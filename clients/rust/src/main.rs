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
    if m.schema_version != 3 {
        panic!(
            "Unsupported manifest schema_version={}; this client requires v3 (per-file substrate, ADR-0012).",
            m.schema_version
        );
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

fn assert_tier_complete(tag: &str, source: &str, tier: &str) {
    let url = hf_url(tag, &format!("{source}/{tier}/.tier_complete"));
    let resp = client().get(&url).send().expect("network error on sentinel probe");
    if !resp.status().is_success() {
        panic!(
            "tier {source}/{tier} is not atomically complete on {tag} (no .tier_complete sentinel). \
             The bake may still be running, or this revision was committed mid-batch. \
             Re-run the bake or pin a known-complete tag."
        );
    }
}

fn fetch_texture_bytes(tag: &str, source: &str, material: &str, channel: &str, tier: &str) -> Vec<u8> {
    let mut last_status: Option<reqwest::StatusCode> = None;
    for ext in ["png", "ktx2"] {
        let url = hf_url(tag, &format!("{source}/{tier}/{material}/{channel}.{ext}"));
        let resp = client().get(&url).send().expect("network error");
        if !resp.status().is_success() {
            last_status = Some(resp.status());
            continue;
        }
        let bytes = resp.bytes().expect("Failed to read body").to_vec();
        if !bytes.starts_with(PNG_MAGIC) && !bytes.starts_with(KTX2_MAGIC) {
            panic!(
                "Expected PNG or KTX2 bytes, got {:?}",
                &bytes[..4.min(bytes.len())]
            );
        }
        return bytes;
    }
    panic!(
        "{material}/{channel} not found at {source}/{tier} (last status={:?})",
        last_status
    );
}

#[derive(Parser)]
#[command(name = "mat-vis", about = "mat-vis PBR texture client (per-file HF substrate)")]
struct Cli {
    #[arg(long, help = "Release tag (default: main)")]
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
        .unwrap_or_else(|| "main".to_string())
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

            assert_tier_complete(&tag, &source, &tier);
            let bytes = fetch_texture_bytes(&tag, &source, &material, &channel, &tier);

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
        assert_tier_complete(&tag, "ambientcg", "1k");
        let bytes = fetch_texture_bytes(&tag, "ambientcg", &mid, "color", "1k");
        assert!(bytes.starts_with(PNG_MAGIC));
        assert!(bytes.len() > 1000);
    }
}
