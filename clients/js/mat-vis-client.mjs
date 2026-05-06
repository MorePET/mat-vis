/**
 * mat-vis reference client — browser + Node.js compatible.
 * Uses fetch() API (native in modern browsers and Node 18+).
 * Zero dependencies.
 *
 * Usage (browser):
 *   import { MatVisClient } from './mat-vis-client.mjs';
 *   const client = new MatVisClient({ tag: 'v2026.04.1' });
 *   const png = await client.fetchTexture('ambientcg', 'Rock064', 'color', '1k');
 *   // png is an ArrayBuffer of raw PNG (or KTX2) bytes
 *
 * Usage (Node CLI):
 *   node mat-vis-client.mjs list
 *   node mat-vis-client.mjs fetch ambientcg Rock064 color 1k -o rock.png
 *
 * Substrate: per-file HF dataset (#186 / ADR-0012). The client does
 * a plain HTTPS GET on
 *   <HF_BASE>/<tag>/<source>/<tier>/<material>/<channel>.{png,ktx2}
 * and probes a `.tier_complete` sentinel once per (source, tier) so
 * partial bakes can never serve half-baked bytes.
 */

const HF_DATASET = 'gerchowl/mat-vis';
const HF_BASE =
  (typeof process !== 'undefined' && process.env?.MAT_VIS_HF_BASE) ||
  `https://huggingface.co/datasets/${HF_DATASET}/resolve`;

// SSoT: clients/js/package.json. Kept in sync by
// scripts/sync-js-version.py (pre-commit) — a drift test in tests/
// fails CI if these disagree. Do not hand-edit.
export const VERSION = '0.6.4';
const UA = `mat-vis-client/${VERSION} (JavaScript)`;

// Default tag when the caller doesn't pin one (#242). The dataset's
// `main` branch is an empty baseline — every release lives on a
// CalVer branch — so a tag-less client must default to a real
// release. Bump in lockstep with the Python client's DEFAULT_TAG
// when a new prod release ships under the per-file substrate
// (#186 / ADR-0012). Explicit ``tag=...`` still wins.
export const DEFAULT_TAG = 'v2026.04.2';

const PNG_MAGIC = [0x89, 0x50, 0x4e, 0x47];
const KTX2_MAGIC = [0xab, 0x4b, 0x54, 0x58];

function startsWithMagic(buf, magic) {
  if (buf.byteLength < magic.length) return false;
  const v = new Uint8Array(buf, 0, magic.length);
  for (let i = 0; i < magic.length; i++) {
    if (v[i] !== magic[i]) return false;
  }
  return true;
}

// Issue #258 — manifest cache validates against the origin per client
// lifecycle via a conditional GET. Storage is filesystem in Node (so a
// fresh process can short-circuit body refetches on 304) and in-memory
// only in the browser (no persistent cache available without IndexedDB,
// which would pull a dependency the zero-deps client refuses to take).
const IS_NODE = typeof process !== 'undefined' && process.versions?.node;

async function readCachedManifest(cacheDir) {
  if (!IS_NODE || !cacheDir) return [null, null];
  const { readFile } = await import('fs/promises');
  const { join } = await import('path');
  try {
    const body = await readFile(join(cacheDir, '.manifest.json'), 'utf-8');
    let etag = null;
    try {
      etag = (await readFile(join(cacheDir, '.manifest.etag'), 'utf-8')) || null;
    } catch {
      // no etag file — cold-start equivalent, fall through.
    }
    return [body, etag];
  } catch {
    return [null, null];
  }
}

async function writeCachedManifest(cacheDir, body, etag) {
  if (!IS_NODE || !cacheDir) return;
  const { mkdir, writeFile, unlink } = await import('fs/promises');
  const { join } = await import('path');
  await mkdir(cacheDir, { recursive: true });
  await writeFile(join(cacheDir, '.manifest.json'), body);
  const etagPath = join(cacheDir, '.manifest.etag');
  if (etag) {
    await writeFile(etagPath, etag);
  } else {
    // Stale etag from a prior lifecycle would falsely 304 us.
    try {
      await unlink(etagPath);
    } catch {
      // already absent — fine.
    }
  }
}

export class MatVisClient {
  #tag;
  #cacheDir;
  #manifest = null;
  #catalogs = new Map();
  #tierComplete = new Map();

  /**
   * @param {Object} opts
   * @param {string} [opts.tag] - Release tag (default: DEFAULT_TAG, see #242)
   * @param {string} [opts.cacheDir] - Per-tag manifest cache dir (Node only).
   *   Falls back to ``$MAT_VIS_CACHE/<tag>`` then ``~/.cache/mat-vis/<tag>``.
   *   Browser: ignored (no persistent cache without IndexedDB).
   */
  constructor({ tag, cacheDir } = {}) {
    this.#tag = tag || DEFAULT_TAG;
    if (IS_NODE) {
      const root =
        cacheDir ||
        process.env.MAT_VIS_CACHE ||
        (process.env.HOME ? `${process.env.HOME}/.cache/mat-vis` : null);
      this.#cacheDir = root ? `${root}/${this.#tag}` : null;
    } else {
      this.#cacheDir = null;
    }
  }

  #hfUrl(path) {
    return `${HF_BASE}/${this.#tag}/${path}`;
  }

  async manifest() {
    // In-memory short-circuit (#258): repeated calls in the same
    // process never touch HTTP, regardless of substrate.
    if (this.#manifest) return this.#manifest;

    const url = this.#hfUrl('release-manifest.json');
    const [cachedBody, cachedEtag] = await readCachedManifest(this.#cacheDir);
    const headers = { 'User-Agent': UA };
    if (cachedEtag) headers['If-None-Match'] = cachedEtag;

    const resp = await fetch(url, { headers });

    // 304 Not Modified — cached body is authoritative. On an immutable
    // release tag (see README's immutable-tag note) this is the steady
    // state, so we save the body bytes and avoid the JSON parse hop on
    // wire-format payload too.
    if (resp.status === 304 && cachedBody) {
      this.#manifest = JSON.parse(cachedBody);
    } else {
      if (!resp.ok) throw new Error(`Failed to fetch manifest: ${resp.status}`);
      const body = await resp.text();
      this.#manifest = JSON.parse(body);
      const newEtag = resp.headers.get('etag');
      await writeCachedManifest(this.#cacheDir, body, newEtag);
    }

    const sv = this.#manifest.schema_version;
    if (sv !== 3) {
      throw new Error(
        `Unsupported manifest schema_version=${sv}; this client requires v3 (per-file substrate, ADR-0012).`,
      );
    }
    return this.#manifest;
  }

  async tiers() {
    const m = await this.manifest();
    const seen = new Set();
    for (const src of Object.values(m.sources || {})) {
      for (const t of Object.keys(src.tiers || {})) seen.add(t);
    }
    return [...seen].sort();
  }

  async sources(tier = '1k') {
    const m = await this.manifest();
    const out = [];
    for (const [name, entry] of Object.entries(m.sources || {})) {
      if (entry.tiers && tier in entry.tiers) out.push(name);
    }
    return out.sort();
  }

  async #catalog(source) {
    if (this.#catalogs.has(source)) return this.#catalogs.get(source);
    const m = await this.manifest();
    const srcEntry = m.sources?.[source];
    if (!srcEntry) throw new Error(`Source ${source} not found in manifest`);
    const catalogPath = srcEntry.catalog || `${source}.json`;
    const url = this.#hfUrl(catalogPath);
    const resp = await fetch(url, { headers: { 'User-Agent': UA } });
    if (!resp.ok) throw new Error(`Failed to fetch catalog ${catalogPath}: ${resp.status}`);
    const entries = await resp.json();
    if (!Array.isArray(entries)) throw new Error(`Catalog ${catalogPath} is not a list`);
    this.#catalogs.set(source, entries);
    return entries;
  }

  async materials(source, tier = '1k') {
    const cat = await this.#catalog(source);
    return cat
      .filter((e) => Array.isArray(e.available_tiers) && e.available_tiers.includes(tier))
      .map((e) => e.id)
      .filter((id) => typeof id === 'string')
      .sort();
  }

  async channels(source, materialId, tier = '1k') {
    void tier;
    const cat = await this.#catalog(source);
    const entry = cat.find((e) => e.id === materialId);
    if (!entry) return [];
    const maps = Array.isArray(entry.maps)
      ? entry.maps
      : Object.keys(entry.texture_hashes || {});
    return [...maps].filter((m) => typeof m === 'string').sort();
  }

  async #assertTierComplete(source, tier) {
    const key = `${source}/${tier}`;
    if (this.#tierComplete.get(key)) return;
    const url = this.#hfUrl(`${source}/${tier}/.tier_complete`);
    const resp = await fetch(url, { headers: { 'User-Agent': UA } });
    if (!resp.ok) {
      throw new Error(
        `tier ${source}/${tier} is not atomically complete on this release ` +
          `(no .tier_complete sentinel). The bake may still be running, or ` +
          `this revision was committed mid-batch. Re-run the bake or pin a ` +
          `known-complete tag.`,
      );
    }
    this.#tierComplete.set(key, true);
  }

  /**
   * Fetch a single texture via plain HTTPS GET (#186 / ADR-0012).
   *
   * URL: <HF_BASE>/<tag>/<source>/<tier>/<material>/<channel>.{png,ktx2}.
   * PNG is tried first; on 404 falls back to .ktx2 for derived tiers.
   *
   * @returns {Promise<ArrayBuffer>} Raw PNG or KTX2 bytes.
   */
  async fetchTexture(source, materialId, channel, tier = '1k') {
    const cat = await this.#catalog(source);
    const entry = cat.find((e) => e.id === materialId);
    if (!entry) throw new Error(`Material ${materialId} not found in ${source}`);
    if (!(entry.available_tiers || []).includes(tier)) {
      throw new Error(`Material ${materialId} is not staged at tier ${tier}`);
    }
    const available = await this.channels(source, materialId, tier);
    if (!available.includes(channel)) {
      throw new Error(
        `channel ${channel} not found (context: ${source}/${tier}/${materialId}). ` +
          `Available: ${available.join(', ')}`,
      );
    }

    await this.#assertTierComplete(source, tier);

    let lastErr = null;
    for (const ext of ['png', 'ktx2']) {
      const url = this.#hfUrl(`${source}/${tier}/${materialId}/${channel}.${ext}`);
      const resp = await fetch(url, { headers: { 'User-Agent': UA } });
      if (!resp.ok) {
        lastErr = new Error(`HTTP ${resp.status} on ${url}`);
        continue;
      }
      const buf = await resp.arrayBuffer();
      if (!startsWithMagic(buf, PNG_MAGIC) && !startsWithMagic(buf, KTX2_MAGIC)) {
        const head = new Uint8Array(buf, 0, Math.min(12, buf.byteLength));
        throw new Error(
          `Expected PNG or KTX2 bytes, got ${[...head].map((b) => b.toString(16)).join(' ')}`,
        );
      }
      return buf;
    }
    throw lastErr || new Error(`channel ${channel} not available for ${source}/${materialId}`);
  }
}

// ── Node CLI ────────────────────────────────────────────────────

const isNode = typeof process !== 'undefined' && process.argv;
if (isNode && process.argv[1]?.endsWith('mat-vis-client.mjs')) {
  const args = process.argv.slice(2);
  const cmd = args[0];
  const client = new MatVisClient({ tag: process.env.MAT_VIS_TAG });

  (async () => {
    try {
      if (cmd === 'list') {
        const tiers = await client.tiers();
        for (const tier of tiers) {
          const sources = await client.sources(tier);
          console.log(`${tier}: ${sources.join(', ')}`);
        }
      } else if (cmd === 'materials') {
        const mats = await client.materials(args[1], args[2] || '1k');
        mats.forEach((m) => console.log(m));
      } else if (cmd === 'fetch') {
        const [, source, material, channel, tier = '1k'] = args;
        const outIdx = args.indexOf('-o');
        const output = outIdx >= 0 ? args[outIdx + 1] : null;

        const buf = await client.fetchTexture(source, material, channel, tier);

        if (output) {
          const { writeFileSync } = await import('fs');
          writeFileSync(output, Buffer.from(buf));
          console.error(`Wrote ${output} (${buf.byteLength.toLocaleString()} bytes)`);
        } else {
          process.stdout.write(Buffer.from(buf));
        }
      } else {
        console.log('mat-vis client (JavaScript)');
        console.log('');
        console.log('Usage:');
        console.log('  node mat-vis-client.mjs list');
        console.log('  node mat-vis-client.mjs materials <source> [tier]');
        console.log('  node mat-vis-client.mjs fetch <source> <id> <channel> [tier] [-o file]');
      }
    } catch (e) {
      console.error(`error: ${e.message}`);
      process.exit(1);
    }
  })();
}
