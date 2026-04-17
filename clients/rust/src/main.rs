//! mat-vis reference client — Rust.
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

const REPO: &str = "MorePET/mat-vis";
const UA: &str = "mat-vis-client/0.1 (Rust)";

#[derive(Deserialize)]
struct Manifest {
    tiers: HashMap<String, TierEntry>,
}

#[derive(Deserialize)]
struct TierEntry {
    base_url: String,
    sources: HashMap<String, SourceEntry>,
}

#[derive(Deserialize)]
struct SourceEntry {
    parquet_files: Vec<String>,
    rowmap_files: Option<Vec<String>>,
    rowmap_file: Option<String>,
}

#[derive(Deserialize)]
struct Rowmap {
    parquet_file: String,
    materials: HashMap<String, HashMap<String, ChannelRange>>,
}

#[derive(Deserialize)]
struct ChannelRange {
    offset: u64,
    length: u64,
}

fn client() -> reqwest::blocking::Client {
    reqwest::blocking::Client::builder()
        .user_agent(UA)
        .build()
        .expect("Failed to build HTTP client")
}

fn fetch_manifest(tag: &Option<String>) -> Manifest {
    let url = match tag {
        Some(t) => format!("https://github.com/{REPO}/releases/download/{t}/release-manifest.json"),
        None => format!("https://github.com/{REPO}/releases/latest/download/release-manifest.json"),
    };
    client()
        .get(&url)
        .send()
        .expect("Failed to fetch manifest")
        .json()
        .expect("Failed to parse manifest")
}

fn fetch_rowmap(base_url: &str, src: &SourceEntry) -> Rowmap {
    let files = src
        .rowmap_files
        .as_deref()
        .or(src.rowmap_file.as_deref().map(std::slice::from_ref))
        .expect("No rowmap file");
    let url = format!("{}{}", base_url, files[0]);
    client()
        .get(&url)
        .send()
        .expect("Failed to fetch rowmap")
        .json()
        .expect("Failed to parse rowmap")
}

fn range_read(url: &str, offset: u64, length: u64) -> Vec<u8> {
    let range = format!("bytes={}-{}", offset, offset + length - 1);
    let resp = client()
        .get(url)
        .header("Range", &range)
        .send()
        .expect("Range read failed");
    resp.bytes().expect("Failed to read body").to_vec()
}

#[derive(Parser)]
#[command(name = "mat-vis", about = "mat-vis PBR texture client")]
struct Cli {
    #[arg(long, help = "Release tag (default: latest)")]
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
    /// Fetch a texture PNG
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

fn main() {
    let cli = Cli::parse();
    let manifest = fetch_manifest(&cli.tag);

    match cli.cmd {
        Commands::List => {
            for (tier, entry) in &manifest.tiers {
                let sources: Vec<&String> = entry.sources.keys().collect();
                println!("{tier}: {}", sources.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", "));
            }
        }
        Commands::Materials { source, tier } => {
            let tier_data = manifest.tiers.get(&tier).expect("Tier not found");
            let src_data = tier_data.sources.get(&source).expect("Source not found");
            let rowmap = fetch_rowmap(&tier_data.base_url, src_data);
            let mut ids: Vec<&String> = rowmap.materials.keys().collect();
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
            let tier_data = manifest.tiers.get(&tier).expect("Tier not found");
            let src_data = tier_data.sources.get(&source).expect("Source not found");
            let rowmap = fetch_rowmap(&tier_data.base_url, src_data);
            let mat = rowmap.materials.get(&material).expect("Material not found");
            let rng = mat.get(&channel).expect("Channel not found");

            let url = format!("{}{}", tier_data.base_url, rowmap.parquet_file);
            let data = range_read(&url, rng.offset, rng.length);

            // Verify PNG
            assert!(
                data.len() >= 4 && data[..4] == [0x89, 0x50, 0x4E, 0x47],
                "Expected PNG, got {:?}",
                &data[..4.min(data.len())]
            );

            match output {
                Some(path) => {
                    fs::write(&path, &data).expect("Failed to write file");
                    eprintln!("Wrote {} ({} bytes)", path.display(), data.len());
                }
                None => {
                    std::io::stdout().write_all(&data).expect("Failed to write to stdout");
                }
            }
        }
    }
}
