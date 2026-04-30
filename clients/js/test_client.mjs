/**
 * Tests for the JavaScript reference client.
 *
 * Two test modes:
 *   1. Structural tests — always run. Verify URL contract, magic-byte
 *      parsing, and v3 manifest shape using a stubbed `fetch()`.
 *   2. Live tests — gated on MAT_VIS_LIVE_TESTS=1. Hit the prod HF
 *      dataset; default-skipped because v0.6 dropped tar support and
 *      prod isn't rebaked under the per-file substrate yet (see #179).
 *
 * Uses Node's built-in test runner (node:test) — zero dependencies.
 * Run with: node --test test_client.mjs
 */

import { describe, it, before, after } from 'node:test';
import assert from 'node:assert';
import { MatVisClient, DEFAULT_TAG } from './mat-vis-client.mjs';

// ── stubbed-fetch structural tests ─────────────────────────────────

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0]);

function stubFetch(routes) {
  return async (url, _opts) => {
    for (const [pattern, handler] of routes) {
      if (typeof pattern === 'string' && url.includes(pattern)) return handler(url);
      if (pattern instanceof RegExp && pattern.test(url)) return handler(url);
    }
    return {
      ok: false,
      status: 404,
      async json() { return {}; },
      async arrayBuffer() { return new ArrayBuffer(0); },
    };
  };
}

function jsonResp(obj) {
  return {
    ok: true,
    status: 200,
    async json() { return obj; },
    async arrayBuffer() { return new ArrayBuffer(0); },
  };
}

function bytesResp(u8) {
  return {
    ok: true,
    status: 200,
    async json() { throw new Error('not json'); },
    async arrayBuffer() {
      return u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength);
    },
  };
}

const MOCK_MANIFEST = {
  schema_version: 3,
  release_tag: 'vtest',
  sources: {
    ambientcg: { catalog: 'ambientcg.json', tiers: { '1k': { complete: true } } },
  },
};
const MOCK_CATALOG = [
  {
    id: 'Rock064',
    source: 'ambientcg',
    mat_vis: { name: 'Rock064', category: 'stone' },
    available_tiers: ['1k'],
    maps: ['color', 'normal'],
  },
];

// ── default tag (#242) ────────────────────────────────────────────

describe('default tag (#242)', () => {
  it('exports DEFAULT_TAG = "v2026.04.2"', () => {
    assert.strictEqual(DEFAULT_TAG, 'v2026.04.2');
  });

  it('client without tag uses DEFAULT_TAG in HF URLs', async () => {
    const captured = [];
    const originalFetch = globalThis.fetch;
    globalThis.fetch = stubFetch([
      [
        '/release-manifest.json',
        (url) => {
          captured.push(url);
          return jsonResp(MOCK_MANIFEST);
        },
      ],
    ]);
    try {
      const client = new MatVisClient();
      await client.manifest();
      assert.ok(
        captured.some((u) => u.includes(`/${DEFAULT_TAG}/release-manifest.json`)),
        `expected URL containing /${DEFAULT_TAG}/release-manifest.json, got ${captured}`,
      );
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('explicit tag overrides DEFAULT_TAG', async () => {
    const captured = [];
    const originalFetch = globalThis.fetch;
    globalThis.fetch = stubFetch([
      [
        '/release-manifest.json',
        (url) => {
          captured.push(url);
          return jsonResp(MOCK_MANIFEST);
        },
      ],
    ]);
    try {
      const client = new MatVisClient({ tag: 'v2026.04.0' });
      await client.manifest();
      assert.ok(
        captured.some((u) => u.includes('/v2026.04.0/release-manifest.json')),
        `expected URL containing /v2026.04.0/, got ${captured}`,
      );
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});

describe('structural', () => {
  let originalFetch;

  before(() => {
    originalFetch = globalThis.fetch;
  });
  after(() => {
    globalThis.fetch = originalFetch;
  });

  it('fetchTexture probes sentinel BEFORE the texture GET', async () => {
    // Order matters — a regression that fetched the .png first would
    // surface mid-batch bytes. Record call order and assert it.
    const calledUrls = [];
    globalThis.fetch = stubFetch([
      ['/release-manifest.json', () => jsonResp(MOCK_MANIFEST)],
      ['/ambientcg.json', () => jsonResp(MOCK_CATALOG)],
      [
        '/.tier_complete',
        (url) => {
          calledUrls.push(url);
          return jsonResp({});
        },
      ],
      [
        '/Rock064/color.png',
        (url) => {
          calledUrls.push(url);
          return bytesResp(PNG);
        },
      ],
    ]);
    const client = new MatVisClient({ tag: 'vtest' });
    const buf = await client.fetchTexture('ambientcg', 'Rock064', 'color', '1k');
    assert.ok(buf.byteLength > 0, 'should return non-empty bytes');
    const head = new Uint8Array(buf, 0, 4);
    assert.deepStrictEqual([...head], [0x89, 0x50, 0x4e, 0x47]);
    const sentinelIdx = calledUrls.findIndex((u) => u.endsWith('/ambientcg/1k/.tier_complete'));
    const pngIdx = calledUrls.findIndex((u) => u.endsWith('/ambientcg/1k/Rock064/color.png'));
    assert.ok(sentinelIdx >= 0, 'sentinel must be probed');
    assert.ok(pngIdx >= 0, 'per-file URL must be hit');
    assert.ok(
      sentinelIdx < pngIdx,
      `sentinel must run before texture (got sentinel@${sentinelIdx}, png@${pngIdx})`,
    );
  });

  it('fetchTexture rejects bytes whose magic is neither PNG nor KTX2', async () => {
    globalThis.fetch = stubFetch([
      ['/release-manifest.json', () => jsonResp(MOCK_MANIFEST)],
      ['/ambientcg.json', () => jsonResp(MOCK_CATALOG)],
      ['/.tier_complete', () => jsonResp({})],
      [
        '/color.png',
        () => bytesResp(new Uint8Array([0xff, 0xff, 0xff, 0xff, 0, 0, 0, 0])),
      ],
    ]);
    const client = new MatVisClient({ tag: 'vtest' });
    await assert.rejects(
      () => client.fetchTexture('ambientcg', 'Rock064', 'color', '1k'),
      /Expected PNG or KTX2 bytes/,
    );
  });

  it('fetchTexture falls back to .ktx2 when .png 404s', async () => {
    globalThis.fetch = stubFetch([
      ['/release-manifest.json', () => jsonResp(MOCK_MANIFEST)],
      ['/ambientcg.json', () => jsonResp(MOCK_CATALOG)],
      ['/.tier_complete', () => jsonResp({})],
      [
        '/color.png',
        () => ({
          ok: false,
          status: 404,
          async json() { return {}; },
          async arrayBuffer() { return new ArrayBuffer(0); },
        }),
      ],
      [
        '/color.ktx2',
        () =>
          bytesResp(
            new Uint8Array([
              0xab, 0x4b, 0x54, 0x58, 0x20, 0x32, 0x30, 0xbb, 0x0d, 0x0a, 0x1a, 0x0a, 0,
            ]),
          ),
      ],
    ]);
    const client = new MatVisClient({ tag: 'vtest' });
    const buf = await client.fetchTexture('ambientcg', 'Rock064', 'color', '1k');
    const head = new Uint8Array(buf, 0, 4);
    assert.deepStrictEqual([...head], [0xab, 0x4b, 0x54, 0x58]);
  });

  it('fetchTexture rejects when sentinel is missing', async () => {
    globalThis.fetch = stubFetch([
      ['/release-manifest.json', () => jsonResp(MOCK_MANIFEST)],
      ['/ambientcg.json', () => jsonResp(MOCK_CATALOG)],
      // intentionally NO route for .tier_complete → 404
    ]);
    const client = new MatVisClient({ tag: 'vtest' });
    await assert.rejects(
      () => client.fetchTexture('ambientcg', 'Rock064', 'color', '1k'),
      /not atomically complete/,
    );
  });

  it('manifest rejects pre-v3 schema', async () => {
    globalThis.fetch = stubFetch([
      ['/release-manifest.json', () => jsonResp({ schema_version: 2, sources: {} })],
    ]);
    const client = new MatVisClient({ tag: 'vtest' });
    await assert.rejects(() => client.manifest(), /schema_version=2/);
  });

  it('materials filters by available_tiers', async () => {
    globalThis.fetch = stubFetch([
      ['/release-manifest.json', () => jsonResp(MOCK_MANIFEST)],
      [
        '/ambientcg.json',
        () =>
          jsonResp([
            ...MOCK_CATALOG,
            { id: 'Wood001', available_tiers: ['2k'], maps: ['color'] },
          ]),
      ],
    ]);
    const client = new MatVisClient({ tag: 'vtest' });
    const mats = await client.materials('ambientcg', '1k');
    assert.deepStrictEqual(mats, ['Rock064']);
  });

  it('channels reads from catalog maps array', async () => {
    globalThis.fetch = stubFetch([
      ['/release-manifest.json', () => jsonResp(MOCK_MANIFEST)],
      ['/ambientcg.json', () => jsonResp(MOCK_CATALOG)],
    ]);
    const client = new MatVisClient({ tag: 'vtest' });
    const chs = await client.channels('ambientcg', 'Rock064', '1k');
    assert.deepStrictEqual(chs, ['color', 'normal']);
  });
});

// ── live tests ──────────────────────────────────────────────────────

const LIVE_ENABLED = process.env.MAT_VIS_LIVE_TESTS === '1';
const LIVE_TAG = process.env.MAT_VIS_LIVE_TAG || 'v2026.04.1';

const liveDescribe = LIVE_ENABLED ? describe : describe.skip;

liveDescribe('live default tag (#242; set MAT_VIS_LIVE_TESTS=1)', () => {
  // No tag override — exercises DEFAULT_TAG against prod HF.
  const client = new MatVisClient();

  it('default-tag client fetches a v3 manifest with sources', async () => {
    const m = await client.manifest();
    assert.strictEqual(m.schema_version, 3);
    assert.ok(m.sources && Object.keys(m.sources).length > 0);
  });

  it('default-tag client lists 1k sources', async () => {
    const sources = await client.sources('1k');
    assert.ok(sources.includes('ambientcg'), `expected ambientcg in ${sources}`);
  });
});

liveDescribe('live (set MAT_VIS_LIVE_TESTS=1; needs per-file prod tag)', () => {
  const client = new MatVisClient({ tag: LIVE_TAG });

  it('manifest is v3', async () => {
    const m = await client.manifest();
    assert.strictEqual(m.schema_version, 3);
    assert.ok(m.sources, 'manifest should have sources block');
  });

  it('lists 1k tier', async () => {
    const tiers = await client.tiers();
    assert.ok(tiers.includes('1k'));
  });

  it('returns valid PNG bytes via per-file URL', async () => {
    const mats = await client.materials('ambientcg', '1k');
    assert.ok(mats.length > 0);
    const buf = await client.fetchTexture('ambientcg', mats[0], 'color', '1k');
    const magic = new Uint8Array(buf, 0, 4);
    assert.deepStrictEqual([...magic], [0x89, 0x50, 0x4e, 0x47]);
    assert.ok(buf.byteLength > 1000);
  });
});

// ── e2e (mat-vis-tst per-file substrate, #193) ─────────────────────
//
// Round-trip against the throwaway scratch dataset
// `gerchowl/mat-vis-tst`. Gated on MAT_VIS_E2E=1.
//
// Ordering contract (see issue #193): the Python E2E suite at
// tests/e2e/test_per_file_roundtrip.py owns the bake/cleanup
// lifecycle for the throwaway tag (default `v0.0.0-e2e-184-perfile`).
// This block presumes that suite has already run (or is running in
// the same CI job) so the tag exists. Operators run:
//
//   MAT_VIS_E2E=1 pytest tests/e2e/      # bakes the tag
//   MAT_VIS_E2E=1 node --test clients/js/test_client.mjs  # rides on it
//
// We do NOT bake from JS — keeping the bake side-effect single-owner
// avoids racy commits to mat-vis-tst.

const E2E_ENABLED = process.env.MAT_VIS_E2E === '1';
const E2E_TAG = process.env.MAT_VIS_E2E_TAG || 'v0.0.0-e2e-184-perfile';
const E2E_REPO = 'gerchowl/mat-vis-tst';
const E2E_HF_BASE = `https://huggingface.co/datasets/${E2E_REPO}/resolve`;

const e2eDescribe = E2E_ENABLED ? describe : describe.skip;

e2eDescribe('e2e (set MAT_VIS_E2E=1; needs per-file mat-vis-tst tag)', () => {
  // The exported MatVisClient module captures HF_BASE at import time.
  // Re-import it dynamically with MAT_VIS_HF_BASE set so the URL
  // prefix points at mat-vis-tst for this block only.
  let TstClient;

  before(async () => {
    process.env.MAT_VIS_HF_BASE = E2E_HF_BASE;
    // Bust ESM cache by appending a query so we get a fresh module
    // with the env var read at module init.
    const mod = await import(`./mat-vis-client.mjs?e2e=${Date.now()}`);
    TstClient = mod.MatVisClient;
  });

  it('round-trips a polyhaven 1k color PNG via per-file URL', async () => {
    const client = new TstClient({ tag: E2E_TAG });
    const mats = await client.materials('polyhaven', '1k');
    assert.ok(mats.length >= 1, `expected baked polyhaven materials, got ${mats}`);
    const buf = await client.fetchTexture('polyhaven', mats[0], 'color', '1k');
    const magic = new Uint8Array(buf, 0, 4);
    assert.deepStrictEqual(
      [...magic],
      [0x89, 0x50, 0x4e, 0x47],
      'must be a real PNG',
    );
    assert.ok(buf.byteLength > 1000, `PNG too small: ${buf.byteLength}`);
  });
});
