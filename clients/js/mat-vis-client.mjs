/**
 * mat-vis reference client — browser + Node.js compatible.
 * Uses fetch() API (native in modern browsers and Node 18+).
 * Zero dependencies.
 *
 * Usage (browser):
 *   import { MatVisClient } from './mat-vis-client.mjs';
 *   const client = new MatVisClient();
 *   const png = await client.fetchTexture('ambientcg', 'Rock064', 'color', '1k');
 *   // png is an ArrayBuffer of raw PNG bytes
 *
 * Usage (Node CLI):
 *   node mat-vis-client.mjs list
 *   node mat-vis-client.mjs fetch ambientcg Rock064 color 1k -o rock.png
 */

const REPO = 'MorePET/mat-vis';
const RELEASES = `https://github.com/${REPO}/releases`;
const UA = 'mat-vis-client/0.1 (JavaScript)';

export class MatVisClient {
  #manifestUrl;
  #manifest = null;
  #rowmaps = new Map();

  /**
   * @param {Object} opts
   * @param {string} [opts.tag] - Release tag (default: latest)
   * @param {string} [opts.manifestUrl] - Override manifest URL
   */
  constructor({ tag, manifestUrl } = {}) {
    if (manifestUrl) {
      this.#manifestUrl = manifestUrl;
    } else if (tag) {
      this.#manifestUrl = `${RELEASES}/download/${tag}/release-manifest.json`;
    } else {
      this.#manifestUrl = `${RELEASES}/latest/download/release-manifest.json`;
    }
  }

  async manifest() {
    if (!this.#manifest) {
      const resp = await fetch(this.#manifestUrl, { headers: { 'User-Agent': UA } });
      if (!resp.ok) throw new Error(`Failed to fetch manifest: ${resp.status}`);
      this.#manifest = await resp.json();
    }
    return this.#manifest;
  }

  async tiers() {
    const m = await this.manifest();
    return Object.keys(m.tiers);
  }

  async sources(tier = '1k') {
    const m = await this.manifest();
    return Object.keys(m.tiers[tier]?.sources || {});
  }

  async rowmap(source, tier) {
    const key = `${source}-${tier}`;
    if (!this.#rowmaps.has(key)) {
      const m = await this.manifest();
      const tierData = m.tiers[tier];
      if (!tierData) throw new Error(`Tier ${tier} not found`);
      const srcData = tierData.sources[source];
      if (!srcData) throw new Error(`Source ${source} not found for tier ${tier}`);

      const rowmapFiles = srcData.rowmap_files || [srcData.rowmap_file];
      const url = tierData.base_url + rowmapFiles[0];
      const resp = await fetch(url, { headers: { 'User-Agent': UA } });
      if (!resp.ok) throw new Error(`Failed to fetch rowmap: ${resp.status}`);
      this.#rowmaps.set(key, await resp.json());
    }
    return this.#rowmaps.get(key);
  }

  async materials(source, tier = '1k') {
    const rm = await this.rowmap(source, tier);
    return Object.keys(rm.materials).sort();
  }

  async channels(source, materialId, tier = '1k') {
    const rm = await this.rowmap(source, tier);
    return Object.keys(rm.materials[materialId] || {}).sort();
  }

  /**
   * Fetch a single texture PNG via HTTP range read.
   * @returns {Promise<ArrayBuffer>} Raw PNG bytes
   */
  async fetchTexture(source, materialId, channel, tier = '1k') {
    const rm = await this.rowmap(source, tier);
    const mat = rm.materials[materialId];
    if (!mat) throw new Error(`Material ${materialId} not found`);
    const rng = mat[channel];
    if (!rng) throw new Error(`Channel ${channel} not found for ${materialId}`);

    const m = await this.manifest();
    const url = m.tiers[tier].base_url + rm.parquet_file;

    const resp = await fetch(url, {
      headers: {
        'User-Agent': UA,
        Range: `bytes=${rng.offset}-${rng.offset + rng.length - 1}`,
      },
    });

    if (!resp.ok && resp.status !== 206) {
      throw new Error(`Range read failed: ${resp.status}`);
    }

    const buf = await resp.arrayBuffer();
    const magic = new Uint8Array(buf, 0, 4);
    if (magic[0] !== 0x89 || magic[1] !== 0x50 || magic[2] !== 0x4e || magic[3] !== 0x47) {
      throw new Error(`Expected PNG, got ${Array.from(magic).map((b) => b.toString(16)).join(' ')}`);
    }

    return buf;
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
