//! mat-vis reference client — Rust.
//!
//! Substrate: per-file HF dataset (#186 / ADR-0012). One reqwest::get
//! per texture; no rowmap, no tar, no range read.
//!
//! Usage:
//!   mat-vis list                                 # list sources × tiers
//!   mat-vis materials ambientcg 1k               # list material IDs
//!   mat-vis fetch ambientcg Rock064 color 1k     # fetch PNG → stdout
//!   mat-vis fetch ambientcg Rock064 color 1k -o rock.png

use clap::{Parser, Subcommand};
use serde::Deserialize;
use std::collections::HashMap;
use std::fs;
use std::io::Write;
use std::path::PathBuf;

const HF_DATASET: &str = "gerchowl/mat-vis";
// SSoT: Cargo.toml version. `concat!` + `env!` fold at compile time, so
// bumping `[package].version` is the only edit needed for a release —
// the HTTP User-Agent string follows automatically.
const UA: &str = concat!("mat-vis-client/", env!("CARGO_PKG_VERSION"), " (Rust)");

// Default tag when the caller doesn't pass --tag / set MAT_VIS_TAG (#242).
// The dataset's `main` branch is an empty baseline — every release
// lives on a CalVer branch — so a tag-less invocation must default to
// a real release. Keep in lockstep with the Python/JS clients'
// DEFAULT_TAG; bump when a new prod release ships under the per-file
// substrate (#186 / ADR-0012).
pub const DEFAULT_TAG: &str = "v2026.04.2";

const PNG_MAGIC: &[u8] = &[0x89, 0x50, 0x4e, 0x47];
const KTX2_MAGIC: &[u8] = &[0xab, 0x4b, 0x54, 0x58];

fn hf_base() -> String {
    std::env::var("MAT_VIS_HF_BASE")
        .unwrap_or_else(|_| format!("https://huggingface.co/datasets/{HF_DATASET}/resolve"))
}

fn hf_url(tag: &str, path: &str) -> String {
    format!("{}/{tag}/{path}", hf_base())
}

#[derive(Deserialize)]
struct Manifest {
    schema_version: u32,
    #[serde(default)]
    sources: HashMap<String, SourceEntry>,
}

#[derive(Deserialize)]
struct SourceEntry {
    catalog: Option<String>,
    #[serde(default)]
    tiers: HashMap<String, serde_json::Value>,
}

#[derive(Deserialize)]
struct CatalogEntry {
    id: String,
    #[serde(default)]
    available_tiers: Vec<String>,
    #[serde(default)]
    maps: Vec<String>,
    #[serde(default)]
    texture_hashes: HashMap<String, serde_json::Value>,
}

fn client() -> reqwest::blocking::Client {
    reqwest::blocking::Client::builder()
        .user_agent(UA)
        .build()
        .expect("Failed to build HTTP client")
}

fn validate_schema(m: &Manifest) -> Result<(), String> {
    if m.schema_version != 3 {
        return Err(format!(
            "Unsupported manifest schema_version={}; this client requires v3 (per-file substrate, ADR-0012).",
            m.schema_version
        ));
    }
    Ok(())
}

fn fetch_manifest(tag: &str) -> Manifest {
    let url = hf_url(tag, "release-manifest.json");
    let m: Manifest = client()
        .get(&url)
        .send()
        .expect("Failed to fetch manifest")
        .error_for_status()
        .expect("Failed to fetch manifest")
        .json()
        .expect("Failed to parse manifest");
    if let Err(e) = validate_schema(&m) {
        eprintln!("{e}");
        std::process::exit(1);
    }
    m
}

fn fetch_catalog(tag: &str, source: &str, manifest: &Manifest) -> Vec<CatalogEntry> {
    let src_entry = manifest
        .sources
        .get(source)
        .unwrap_or_else(|| panic!("Source '{source}' not found in manifest"));
    let catalog_path = src_entry
        .catalog
        .clone()
        .unwrap_or_else(|| format!("{source}.json"));
    let url = hf_url(tag, &catalog_path);
    client()
        .get(&url)
        .send()
        .expect("Failed to fetch catalog")
        .error_for_status()
        .expect("Failed to fetch catalog")
        .json()
        .expect("Failed to parse catalog")
}

fn assert_tier_complete(tag: &str, source: &str, tier: &str) -> Result<(), String> {
    let url = hf_url(tag, &format!("{source}/{tier}/.tier_complete"));
    let resp = client()
        .get(&url)
        .send()
        .map_err(|e| format!("network error on sentinel probe: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!(
            "tier {source}/{tier} is not atomically complete on {tag} (no .tier_complete sentinel). \
             The bake may still be running, or this revision was committed mid-batch. \
             Re-run the bake or pin a known-complete tag."
        ));
    }
    Ok(())
}

fn fetch_texture_bytes(
    tag: &str,
    source: &str,
    material: &str,
    channel: &str,
    tier: &str,
) -> Result<Vec<u8>, String> {
    let mut last_status: Option<reqwest::StatusCode> = None;
    for ext in ["png", "ktx2"] {
        let url = hf_url(tag, &format!("{source}/{tier}/{material}/{channel}.{ext}"));
        let resp = client()
            .get(&url)
            .send()
            .map_err(|e| format!("network error on {ext} fetch: {e}"))?;
        if !resp.status().is_success() {
            last_status = Some(resp.status());
            continue;
        }
        let bytes = resp
            .bytes()
            .map_err(|e| format!("Failed to read body: {e}"))?
            .to_vec();
        if !bytes.starts_with(PNG_MAGIC) && !bytes.starts_with(KTX2_MAGIC) {
            return Err(format!(
                "Expected PNG or KTX2 bytes, got {:?}",
                &bytes[..4.min(bytes.len())]
            ));
        }
        return Ok(bytes);
    }
    Err(format!(
        "{material}/{channel} not found at {source}/{tier} (last status={last_status:?})"
    ))
}

fn die<T>(msg: String) -> T {
    eprintln!("{msg}");
    std::process::exit(1)
}

#[derive(Parser)]
#[command(name = "mat-vis", about = "mat-vis PBR texture client (per-file HF substrate)")]
struct Cli {
    #[arg(long, help = "Release tag (default: DEFAULT_TAG, see #242)")]
    tag: Option<String>,

    #[command(subcommand)]
    cmd: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// List sources × tiers
    List,
    /// List materials for a source × tier
    Materials {
        source: String,
        #[arg(default_value = "1k")]
        tier: String,
    },
    /// Fetch a texture (PNG or KTX2)
    Fetch {
        source: String,
        material: String,
        channel: String,
        #[arg(default_value = "1k")]
        tier: String,
        #[arg(short, long, help = "Output file (default: stdout)")]
        output: Option<PathBuf>,
    },
}

fn resolved_tag(cli_tag: &Option<String>) -> String {
    cli_tag
        .clone()
        .or_else(|| std::env::var("MAT_VIS_TAG").ok())
        .unwrap_or_else(|| DEFAULT_TAG.to_string())
}

fn main() {
    let cli = Cli::parse();
    let tag = resolved_tag(&cli.tag);
    let manifest = fetch_manifest(&tag);

    match cli.cmd {
        Commands::List => {
            let mut by_tier: HashMap<&str, Vec<&str>> = HashMap::new();
            for (src, entry) in &manifest.sources {
                for tier in entry.tiers.keys() {
                    by_tier.entry(tier.as_str()).or_default().push(src.as_str());
                }
            }
            let mut tiers: Vec<&&str> = by_tier.keys().collect();
            tiers.sort();
            for tier in tiers {
                let mut srcs = by_tier[tier].clone();
                srcs.sort();
                println!("{tier}: {}", srcs.join(", "));
            }
        }
        Commands::Materials { source, tier } => {
            let cat = fetch_catalog(&tag, &source, &manifest);
            let mut ids: Vec<String> = cat
                .into_iter()
                .filter(|e| e.available_tiers.iter().any(|t| t == &tier))
                .map(|e| e.id)
                .collect();
            ids.sort();
            for id in ids {
                println!("{id}");
            }
        }
        Commands::Fetch {
            source,
            material,
            channel,
            tier,
            output,
        } => {
            let cat = fetch_catalog(&tag, &source, &manifest);
            let entry = cat
                .iter()
                .find(|e| e.id == material)
                .unwrap_or_else(|| panic!("Material '{material}' not found in {source}"));
            if !entry.available_tiers.iter().any(|t| t == &tier) {
                panic!("Material '{material}' is not staged at tier {tier}");
            }
            let maps: Vec<&str> = if !entry.maps.is_empty() {
                entry.maps.iter().map(String::as_str).collect()
            } else {
                entry.texture_hashes.keys().map(String::as_str).collect()
            };
            if !maps.iter().any(|m| *m == channel) {
                panic!(
                    "channel '{channel}' not found (context: {source}/{tier}/{material}). \
                     Available: {:?}",
                    maps
                );
            }

            assert_tier_complete(&tag, &source, &tier).unwrap_or_else(die);
            let bytes = fetch_texture_bytes(&tag, &source, &material, &channel, &tier)
                .unwrap_or_else(die);

            match output {
                Some(path) => {
                    fs::write(&path, &bytes).expect("Failed to write file");
                    eprintln!("Wrote {} ({} bytes)", path.display(), bytes.len());
                }
                None => {
                    std::io::stdout()
                        .write_all(&bytes)
                        .expect("Failed to write to stdout");
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn live_enabled() -> bool {
        std::env::var("MAT_VIS_LIVE_TESTS").as_deref() == Ok("1")
    }

    fn live_tag() -> String {
        std::env::var("MAT_VIS_LIVE_TAG").unwrap_or_else(|_| "v2026.04.1".to_string())
    }

    /// URL contract — does not hit the network.
    #[test]
    fn per_file_url_shape() {
        let url = hf_url("vtest", "ambientcg/1k/Rock064/color.png");
        assert!(
            url.ends_with("/vtest/ambientcg/1k/Rock064/color.png"),
            "unexpected URL: {url}"
        );
    }

    #[test]
    fn sentinel_url_shape() {
        let url = hf_url("vtest", "ambientcg/1k/.tier_complete");
        assert!(url.ends_with("/vtest/ambientcg/1k/.tier_complete"), "{url}");
    }

    #[test]
    fn ua_includes_version_and_lang() {
        assert!(UA.contains("mat-vis-client/"));
        assert!(UA.contains("(Rust)"));
    }

    #[test]
    fn png_magic_matches() {
        let bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00";
        assert!(bytes.starts_with(PNG_MAGIC));
    }

    #[test]
    fn ktx2_magic_matches() {
        let bytes = b"\xabKTX 20\xbb\r\n\x1a\n\x00";
        assert!(bytes.starts_with(KTX2_MAGIC));
    }

    #[test]
    fn random_magic_rejected() {
        let bytes = [0xffu8; 8];
        assert!(!bytes.starts_with(PNG_MAGIC));
        assert!(!bytes.starts_with(KTX2_MAGIC));
    }

    #[test]
    fn validate_schema_rejects_v2() {
        let m = serde_json::from_str::<Manifest>(r#"{"schema_version": 2, "sources": {}}"#)
            .expect("parse");
        let err = validate_schema(&m).expect_err("v2 must be rejected");
        assert!(err.contains("schema_version=2"), "error message: {err}");
    }

    #[test]
    fn validate_schema_accepts_v3() {
        let m = serde_json::from_str::<Manifest>(r#"{"schema_version": 3, "sources": {}}"#)
            .expect("parse");
        assert!(validate_schema(&m).is_ok());
    }

    // ── default tag (#242) ──────────────────────────────────────────

    /// The dataset's `main` branch is empty — `MatVisClient` without
    /// a `--tag` must default to a real release. This is the unit-side
    /// pin: catches accidental regressions to "main" or wrong CalVer.
    #[test]
    fn default_tag_is_real_release() {
        assert_eq!(DEFAULT_TAG, "v2026.04.2");
    }

    #[test]
    fn resolved_tag_falls_back_to_default() {
        // Drop MAT_VIS_TAG for this assertion; restore after.
        let prev = std::env::var("MAT_VIS_TAG").ok();
        // SAFETY: setting/removing env vars is safe in single-threaded
        // test context — Cargo runs tests in parallel by default but
        // this test only reads its own scope and restores.
        unsafe { std::env::remove_var("MAT_VIS_TAG") };
        let tag = resolved_tag(&None);
        if let Some(p) = prev {
            unsafe { std::env::set_var("MAT_VIS_TAG", p) };
        }
        assert_eq!(tag, DEFAULT_TAG);
    }

    #[test]
    fn resolved_tag_explicit_overrides_default() {
        let tag = resolved_tag(&Some("v2026.04.0".to_string()));
        assert_eq!(tag, "v2026.04.0");
    }

    // ── live (opt-in via MAT_VIS_LIVE_TESTS=1) ─────────────────────

    #[test]
    fn live_fetch_manifest_is_v3() {
        if !live_enabled() {
            eprintln!("(skipped: set MAT_VIS_LIVE_TESTS=1)");
            return;
        }
        let m = fetch_manifest(&live_tag());
        assert_eq!(m.schema_version, 3);
        assert!(!m.sources.is_empty(), "manifest sources should be non-empty");
    }

    /// #242 — `DEFAULT_TAG` must resolve a populated v3 manifest on
    /// prod HF. Skipped unless live tests are enabled.
    #[test]
    fn live_default_tag_fetches_v3_manifest() {
        if !live_enabled() {
            eprintln!("(skipped: set MAT_VIS_LIVE_TESTS=1)");
            return;
        }
        let m = fetch_manifest(DEFAULT_TAG);
        assert_eq!(m.schema_version, 3);
        assert!(
            !m.sources.is_empty(),
            "DEFAULT_TAG must point at a populated release"
        );
    }

    #[test]
    fn live_fetch_color_png() {
        if !live_enabled() {
            eprintln!("(skipped: set MAT_VIS_LIVE_TESTS=1)");
            return;
        }
        let tag = live_tag();
        let m = fetch_manifest(&tag);
        let cat = fetch_catalog(&tag, "ambientcg", &m);
        let mid = cat
            .iter()
            .find(|e| e.available_tiers.iter().any(|t| t == "1k"))
            .map(|e| e.id.clone())
            .expect("no 1k-staged ambientcg material");
        assert_tier_complete(&tag, "ambientcg", "1k").expect("sentinel must exist");
        let bytes = fetch_texture_bytes(&tag, "ambientcg", &mid, "color", "1k")
            .expect("texture fetch must succeed");
        assert!(bytes.starts_with(PNG_MAGIC));
        assert!(bytes.len() > 1000);
    }

    // ── httpmock-based offline coverage (#199) ─────────────────────
    //
    // The HTTP-handling code paths (sentinel, fallback, magic-byte
    // verification, 5xx behavior) were previously only exercised by
    // the live tests, which are gated off by default. These four
    // tests close that gap without needing prod HF.
    //
    // env-var serialization: every test sets `MAT_VIS_HF_BASE` to its
    // mock server URL. Cargo runs tests in a single binary in parallel
    // by default, so we serialize through a process-local Mutex.

    use httpmock::prelude::*;
    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn set_hf_base(url: &str) {
        // SAFETY: the mutex above is held by the caller for the
        // duration of the test, so no concurrent reader/writer races.
        unsafe { std::env::set_var("MAT_VIS_HF_BASE", url) };
    }

    #[test]
    fn http_png_ktx2_fallback() {
        let _guard = ENV_LOCK.lock().unwrap();
        let server = MockServer::start();
        set_hf_base(&server.base_url());

        // .png 404, .ktx2 200 with valid magic.
        let png_mock = server.mock(|when, then| {
            when.method(GET).path("/vtest/ambientcg/1k/Rock064/color.png");
            then.status(404);
        });
        let ktx2_payload = {
            let mut v = Vec::from(KTX2_MAGIC);
            v.extend_from_slice(b" 20\xbb\r\n\x1a\n");
            v.extend(std::iter::repeat(0u8).take(64));
            v
        };
        let ktx2_mock = server.mock(|when, then| {
            when.method(GET).path("/vtest/ambientcg/1k/Rock064/color.ktx2");
            then.status(200).body(&ktx2_payload);
        });

        let bytes = fetch_texture_bytes("vtest", "ambientcg", "Rock064", "color", "1k")
            .expect("fetch should succeed via .ktx2 fallback");
        assert!(bytes.starts_with(KTX2_MAGIC), "fallback must return ktx2 bytes");
        png_mock.assert();
        ktx2_mock.assert();
    }

    #[test]
    fn http_magic_byte_rejection() {
        let _guard = ENV_LOCK.lock().unwrap();
        let server = MockServer::start();
        set_hf_base(&server.base_url());

        // .png 200 but bytes are bogus (no PNG/KTX2 magic).
        let _png = server.mock(|when, then| {
            when.method(GET).path("/vtest/ambientcg/1k/Rock064/color.png");
            then.status(200).body(vec![0xffu8; 64]);
        });

        let err = fetch_texture_bytes("vtest", "ambientcg", "Rock064", "color", "1k")
            .expect_err("bogus bytes must be rejected");
        assert!(err.contains("Expected PNG or KTX2"), "error: {err}");
    }

    #[test]
    fn http_5xx_falls_through_to_ktx2() {
        // Documents inherited Python contract: a 5xx on .png is
        // treated identically to a 404 — the loop continues to .ktx2.
        // This is "wrong" in a UX sense (a 503 retry would be ideal)
        // but is consistent across all three v0.6 clients.
        let _guard = ENV_LOCK.lock().unwrap();
        let server = MockServer::start();
        set_hf_base(&server.base_url());

        let _png = server.mock(|when, then| {
            when.method(GET).path("/vtest/ambientcg/1k/Rock064/color.png");
            then.status(503);
        });
        let ktx2_payload = {
            let mut v = Vec::from(KTX2_MAGIC);
            v.extend(std::iter::repeat(0u8).take(64));
            v
        };
        let _ktx2 = server.mock(|when, then| {
            when.method(GET).path("/vtest/ambientcg/1k/Rock064/color.ktx2");
            then.status(200).body(&ktx2_payload);
        });

        let bytes = fetch_texture_bytes("vtest", "ambientcg", "Rock064", "color", "1k")
            .expect("503 falls through to ktx2 by current contract");
        assert!(bytes.starts_with(KTX2_MAGIC));
    }

    #[test]
    fn http_sentinel_probed_then_texture_fetched() {
        let _guard = ENV_LOCK.lock().unwrap();
        let server = MockServer::start();
        set_hf_base(&server.base_url());

        let sentinel = server.mock(|when, then| {
            when.method(GET).path("/vtest/ambientcg/1k/.tier_complete");
            then.status(200);
        });
        let png_payload = {
            let mut v = Vec::from(PNG_MAGIC);
            v.extend_from_slice(b"\r\n\x1a\n");
            v.extend(std::iter::repeat(0u8).take(64));
            v
        };
        let png = server.mock(|when, then| {
            when.method(GET).path("/vtest/ambientcg/1k/Rock064/color.png");
            then.status(200).body(&png_payload);
        });

        // Drive the same call sequence main() uses.
        assert_tier_complete("vtest", "ambientcg", "1k").expect("sentinel must succeed");
        let bytes = fetch_texture_bytes("vtest", "ambientcg", "Rock064", "color", "1k")
            .expect("png fetch must succeed");
        assert!(bytes.starts_with(PNG_MAGIC));

        // Both endpoints were hit. Sentinel ordering is enforced by
        // the call-site contract in main() (line ~272), not by the
        // helpers themselves — we drive the calls in order here, so
        // a regression that flipped them would change the call order
        // visible via httpmock's hits().
        assert_eq!(sentinel.calls(), 1, "sentinel must be probed exactly once");
        assert_eq!(png.calls(), 1, "png must be fetched exactly once");
    }

    #[test]
    fn http_sentinel_missing_rejects() {
        let _guard = ENV_LOCK.lock().unwrap();
        let server = MockServer::start();
        set_hf_base(&server.base_url());

        let _miss = server.mock(|when, then| {
            when.method(GET).path("/vtest/ambientcg/1k/.tier_complete");
            then.status(404);
        });

        let err = assert_tier_complete("vtest", "ambientcg", "1k")
            .expect_err("404 sentinel must produce an error");
        assert!(err.contains("not atomically complete"), "error: {err}");
    }

    // ── e2e (mat-vis-tst per-file substrate, #193) ─────────────────
    //
    // Round-trip against the throwaway scratch dataset
    // `gerchowl/mat-vis-tst`. Gated on MAT_VIS_E2E=1.
    //
    // Ordering contract (#193): the Python E2E suite at
    // tests/e2e/test_per_file_roundtrip.py owns the bake/cleanup
    // lifecycle for the throwaway tag (default
    // `v0.0.0-e2e-184-perfile`). This test presumes that suite has
    // already run (or is running in the same CI job) so the tag
    // exists. Operators run:
    //
    //   MAT_VIS_E2E=1 pytest tests/e2e/      # bakes the tag
    //   MAT_VIS_E2E=1 cargo test --manifest-path clients/rust/Cargo.toml
    //
    // We do NOT bake from Rust — keeping the bake side-effect
    // single-owner avoids racy commits to mat-vis-tst.

    fn e2e_enabled() -> bool {
        std::env::var("MAT_VIS_E2E").as_deref() == Ok("1")
    }

    fn e2e_tag() -> String {
        std::env::var("MAT_VIS_E2E_TAG").unwrap_or_else(|_| "v0.0.0-e2e-184-perfile".to_string())
    }

    #[test]
    fn e2e_fetch_color_png() {
        if !e2e_enabled() {
            eprintln!("(skipped: set MAT_VIS_E2E=1)");
            return;
        }
        let _guard = ENV_LOCK.lock().unwrap();
        // Point the client at the scratch dataset for the duration
        // of this test only — ENV_LOCK serialises with the httpmock
        // tests above so they don't see this URL.
        let prev = std::env::var("MAT_VIS_HF_BASE").ok();
        set_hf_base("https://huggingface.co/datasets/gerchowl/mat-vis-tst/resolve");

        let result = (|| -> Result<(), String> {
            let tag = e2e_tag();
            let m = fetch_manifest(&tag);
            assert_eq!(m.schema_version, 3);
            let cat = fetch_catalog(&tag, "polyhaven", &m);
            let mid = cat
                .iter()
                .find(|e| e.available_tiers.iter().any(|t| t == "1k"))
                .map(|e| e.id.clone())
                .ok_or_else(|| {
                    "no 1k-staged polyhaven material in mat-vis-tst — did the Python E2E suite bake the tag?".to_string()
                })?;
            assert_tier_complete(&tag, "polyhaven", "1k")?;
            let bytes = fetch_texture_bytes(&tag, "polyhaven", &mid, "color", "1k")?;
            assert!(bytes.starts_with(PNG_MAGIC), "must be a real PNG");
            assert!(bytes.len() > 1000, "PNG too small: {}", bytes.len());
            Ok(())
        })();

        // Restore env (best-effort) before propagating any failure.
        // SAFETY: ENV_LOCK is held for the duration of the test.
        unsafe {
            match prev {
                Some(v) => std::env::set_var("MAT_VIS_HF_BASE", v),
                None => std::env::remove_var("MAT_VIS_HF_BASE"),
            }
        }
        result.expect("e2e round-trip");
    }
}
