/**
 * Tests for the JavaScript reference client against live release data.
 *
 * Uses Node's built-in test runner (node:test) — zero dependencies.
 * Run with: node --test test_client.mjs
 */

import { describe, it } from 'node:test';
import assert from 'node:assert';
import { MatVisClient } from './mat-vis-client.mjs';

const TAG = process.env.MAT_VIS_TAG || 'v2026.04.0';
const client = new MatVisClient({ tag: TAG });

describe('manifest', () => {
  it('fetches manifest with version field', async () => {
    const m = await client.manifest();
    assert.strictEqual(m.version, 1);
    assert.ok(m.tiers, 'manifest should have tiers');
  });

  it('lists tiers including 1k', async () => {
    const tiers = await client.tiers();
    assert.ok(tiers.includes('1k'), 'tiers should include 1k');
  });

  it('lists sources including ambientcg', async () => {
    const sources = await client.sources('1k');
    assert.ok(sources.includes('ambientcg'), 'sources should include ambientcg');
  });
});

describe('rowmap', () => {
  it('fetches rowmap with materials', async () => {
    const rm = await client.rowmap('ambientcg', '1k');
    assert.ok(rm.materials, 'rowmap should have materials');
    assert.ok(Object.keys(rm.materials).length > 0, 'materials should be non-empty');
  });

  it('lists material IDs', async () => {
    const mats = await client.materials('ambientcg', '1k');
    assert.ok(mats.length > 0, 'should have materials');
    assert.ok(mats.every((m) => typeof m === 'string'), 'all IDs should be strings');
  });

  it('lists channels for first material', async () => {
    const mats = await client.materials('ambientcg', '1k');
    const channels = await client.channels('ambientcg', mats[0], '1k');
    assert.ok(channels.includes('color'), 'channels should include color');
  });
});

describe('fetch texture', () => {
  it('returns valid PNG bytes via range read', async () => {
    const mats = await client.materials('ambientcg', '1k');
    const buf = await client.fetchTexture('ambientcg', mats[0], 'color', '1k');
    const magic = new Uint8Array(buf, 0, 4);
    assert.strictEqual(magic[0], 0x89);
    assert.strictEqual(magic[1], 0x50); // P
    assert.strictEqual(magic[2], 0x4e); // N
    assert.strictEqual(magic[3], 0x47); // G
    assert.ok(buf.byteLength > 1000, 'PNG should not be trivially small');
  });

  it('throws on nonexistent material', async () => {
    await assert.rejects(
      () => client.fetchTexture('ambientcg', 'NONEXISTENT_XYZ', 'color', '1k'),
      /not found/i,
    );
  });
});
