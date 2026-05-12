// Load + type the trimmed catalog produced by
// `scripts/fetch_catalog.py`. Used at build time by `getStaticPaths`
// and at runtime by the search island (which fetches the JSON over
// HTTP from the `public/` copy).
import { readFileSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

// PBR scalars we trim to. Keys mirror the upstream `mat_vis.pbr`
// block; only non-null fields are emitted by `fetch_catalog.py`.
export interface TrimmedPbr {
  color_rgb?: [number, number, number];
  roughness?: number;
  metalness?: number;
  ior?: number;
  specular_f0?: number | [number, number, number];
  transmission?: number;
  thickness?: number;
  clearcoat_roughness?: number;
  specular_intensity?: number;
  specular_color?: [number, number, number];
  dispersion?: number;
  is_conductor?: boolean;
  metalness_mean?: number;
  metalness_source?: string;
  complex_ior?: unknown;
}

// One material as carried over the wire and through MiniSearch. Short
// single-letter keys keep `catalog.json` lean — at ~5200 entries even
// a few bytes/field/material matters for first-paint.
export interface TrimmedMaterial {
  id: string;
  s: string; // source: ambientcg | polyhaven | gpuopen | physicallybased
  n: string; // display name
  c: string | null; // category
  t: string[]; // tags
  l: string | null; // license_spdx
  u: string | null; // upstream URL
  a: string[]; // authors
  p: TrimmedPbr; // pbr scalars (only non-null fields)
  d: { p: string | null; u: string | null }; // dates: published / updated
  tiers: string[]; // available texture tiers
  thumb: boolean; // convenience flag — true when `thumb` is in tiers
  // HF coordinates for composing direct download links client-side.
  // Lifted to top-level (not inside `p`) so the detail page doesn't
  // need to know the dataset shape — just the repo + tag + tier list.
}

export interface CatalogMeta {
  release_tag: string;
  repo_id: string;
  generated_at: string;
  sources: string[];
  categories: string[];
  licenses: string[];
  total: number;
}

const HERE = dirname(fileURLToPath(import.meta.url));
const BUILD_DIR = resolve(HERE, "../../build");
const PUBLIC_DIR = resolve(HERE, "../../public");

// Prefer the public-dir copy (the one shipped to the browser) when
// present; that's the source of truth at build time. Fall back to the
// build/ staging dir for the case where `fetch_catalog.py` was run but
// nobody copied the artifacts to public/ yet.
function pickPath(filename: string): string {
  const inPublic = resolve(PUBLIC_DIR, filename);
  if (existsSync(inPublic)) return inPublic;
  return resolve(BUILD_DIR, filename);
}

export function loadCatalog(): TrimmedMaterial[] {
  const path = pickPath("catalog.json");
  if (!existsSync(path)) {
    // Soft-fail: empty catalog so `astro build` doesn't crash in
    // dev environments where nobody fetched data yet. The site will
    // render an empty grid + a "no data — run fetch_catalog.py" hint.
    console.warn(`[load.ts] catalog.json not found at ${path} — using empty catalog`);
    return [];
  }
  const raw = readFileSync(path, "utf-8");
  return JSON.parse(raw) as TrimmedMaterial[];
}

export function loadMeta(): CatalogMeta {
  const path = pickPath("catalog.meta.json");
  if (!existsSync(path)) {
    return {
      release_tag: "unknown",
      repo_id: "gerchowl/mat-vis",
      generated_at: new Date().toISOString(),
      sources: [],
      categories: [],
      licenses: [],
      total: 0,
    };
  }
  const raw = readFileSync(path, "utf-8");
  return JSON.parse(raw) as CatalogMeta;
}

// HF URL composer. The thumb tier ships material thumbnails as
// `<source>/<id>.thumb.png` at the repo root for the given revision.
// Detail pages compose 1k/512/etc. download URLs the same way per
// tier.
export function hfUrl(repo: string, tag: string, path: string): string {
  return `https://huggingface.co/datasets/${repo}/resolve/${tag}/${path}`;
}

// Substrate layout: `<source>/thumb/<id>/thumb.png` per ADR-0012's
// per-file shape. The `thumb` tier flag on the material entry tells us
// the file exists; we compose the resolve URL deterministically here.
export function thumbUrl(meta: CatalogMeta, mat: TrimmedMaterial): string | null {
  if (!mat.thumb) return null;
  return hfUrl(meta.repo_id, meta.release_tag, `${mat.s}/thumb/${mat.id}/thumb.png`);
}

// Tier directory link for the detail page. Substrate layout has
// `<source>/<tier>/<id>/` containing the per-channel files.
export function tierDirUrl(meta: CatalogMeta, mat: TrimmedMaterial, tier: string): string {
  return `https://huggingface.co/datasets/${meta.repo_id}/tree/${meta.release_tag}/${mat.s}/${tier}/${mat.id}`;
}
