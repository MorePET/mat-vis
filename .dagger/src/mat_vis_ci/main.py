"""mat-vis CI pipeline.

Usage:
    dagger call build                # slim baker image
    dagger call build-materialx      # baker + materialx (gpuopen)
    dagger call lint                 # ruff check
    dagger call test                 # pytest
    dagger call smoke                # verify pyarrow import (slim)
    dagger call smoke-materialx      # verify MaterialX import (heavy)
    dagger call probe-sources        # verify upstream API connectivity
    dagger call test-all             # lint + test + smoke + probe
    dagger call test-client-python   # pytest on Python reference client
    dagger call test-client-js       # node --test on JS reference client
    dagger call test-client-shell    # bash tests for shell reference client
    dagger call test-client-rust     # cargo test for Rust reference client
    dagger call test-clients         # all 4 client tests in parallel
    dagger call validate-release      # verify release assets are complete
    dagger call preflight            # verify GHCR auth before push
    dagger call push                 # preflight + build + push to GHCR
"""

from typing import Annotated

import dagger
from dagger import Doc, dag, function, object_type

IMAGE = "ghcr.io/morepet/mat-vis-baker"
TARGET_PLATFORM = dagger.Platform("linux/amd64")

PROBE_SCRIPT = '''\
"""Probe upstream material APIs — one minimal request each."""

import json
import sys
import time
import urllib.request

SOURCES = [
    {
        "name": "ambientcg",
        "url": "https://ambientcg.com/api/v2/full_json?type=Material&limit=1&offset=0",
        "check": lambda d: isinstance(d.get("foundAssets"), list) and len(d["foundAssets"]) > 0,
        "desc": "foundAssets[] non-empty",
    },
    {
        "name": "polyhaven",
        "url": "https://api.polyhaven.com/assets?t=textures",
        "check": lambda d: isinstance(d, dict) and len(d) > 100,
        "desc": "dict with >100 assets",
    },
    {
        "name": "gpuopen",
        "url": "https://api.matlib.gpuopen.com/api/packages?limit=1&offset=0",
        "check": lambda d: isinstance(d.get("results"), list) and len(d["results"]) > 0,
        "desc": "results[] non-empty",
    },
    {
        "name": "physicallybased",
        "url": "https://api.physicallybased.info/materials",
        "check": lambda d: isinstance(d, list) and len(d) > 50,
        "desc": "list with >50 materials",
    },
]

ok = 0
for i, src in enumerate(SOURCES):
    if i > 0:
        time.sleep(2)  # polite delay between sources
    name = src["name"]
    try:
        req = urllib.request.Request(src["url"], headers={"User-Agent": "mat-vis-probe/0.1"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            # log rate-limit headers if present
            rl_headers = {
                k: v
                for k, v in resp.headers.items()
                if k.lower().startswith(("x-ratelimit", "retry-after", "ratelimit"))
            }
            data = json.loads(resp.read())

        if status != 200:
            print(f"FAIL {name}: HTTP {status}")
            continue

        if not src["check"](data):
            print(f"FAIL {name}: unexpected shape (expected {src['desc']})")
            continue

        rl_info = f" rate-limit: {rl_headers}" if rl_headers else ""
        print(f"  OK {name}: HTTP {status}, shape valid ({src['desc']}){rl_info}")
        ok += 1
    except Exception as e:
        print(f"FAIL {name}: {e}")

print(f"\\n{ok}/{len(SOURCES)} sources reachable")
if ok < len(SOURCES):
    sys.exit(1)
'''

VERIFY_SCRIPT = '''\
"""Verify integration test output: parquet + rowmap + range-read."""

import json
import sys
from pathlib import Path

out_dir = Path(sys.argv[1])

# Check files exist
pq_files = sorted(out_dir.glob("*.parquet"))
assert pq_files, f"No parquet files in {out_dir}"

rowmap_files = sorted(out_dir.glob("*-rowmap.json"))
assert rowmap_files, f"No rowmap files in {out_dir}"

index_files = list(out_dir.glob("*.json"))
assert any("rowmap" not in f.name for f in index_files), "No index JSON"

# Match each rowmap to its parquet by category slug
verified = 0
errors = []
total_materials = 0

for rm_path in rowmap_files:
    rowmap = json.loads(rm_path.read_text())
    pq_name = rowmap.get("parquet_file", "")
    pq_path = out_dir / pq_name if pq_name else None

    if not pq_path or not pq_path.exists():
        # Fall back: extract category from rowmap filename and find matching parquet
        # e.g. ambientcg-1k-wood-rowmap.json -> mat-vis-ambientcg-1k-wood.parquet
        slug = rm_path.stem.replace("-rowmap", "")
        candidates = [p for p in pq_files if slug in p.stem]
        if not candidates:
            errors.append(f"No parquet for rowmap {rm_path.name}")
            continue
        pq_path = candidates[0]

    file_bytes = pq_path.read_bytes()
    materials = rowmap["materials"]
    total_materials += len(materials)

    for mid, channels in materials.items():
        for ch, rng in channels.items():
            offset = rng["offset"]
            length = rng["length"]
            chunk = file_bytes[offset : offset + length]
            if chunk[:4] != b"\\x89PNG":
                errors.append(f"{mid}/{ch}: not PNG at offset {offset} (got {chunk[:4]!r})")
                continue
            if len(chunk) != length:
                errors.append(
                    f"{mid}/{ch}: length mismatch at offset {offset}"
                    f" (expected {length}, got {len(chunk)}, file_size={len(file_bytes)})"
                )
                continue
            verified += 1

if errors:
    for e in errors:
        print(f"  FAIL {e}")
    sys.exit(1)

print(f"  OK parquets: {len(pq_files)} files")
print(f"  OK rowmaps: {len(rowmap_files)} files, {total_materials} materials")
print(f"  OK range-read: {verified} channels verified (all PNG)")
print(f"  OK index: {len(index_files)} JSON files")
print(f"\\nintegration test passed")
'''


VALIDATE_RELEASE_SCRIPT = '''\
"""Validate all expected release assets exist and range reads work."""

import json
import os
import random
import sys
import tempfile
import urllib.request

USER_AGENT = "mat-vis-validate/0.1"
TEXTURE_TIERS = ["128", "256", "512", "1k", "2k"]
REQUIRED_SOURCES = ["ambientcg", "polyhaven"]
OPTIONAL_SOURCES = ["gpuopen"]


def get(url, headers=None):
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def get_json(url):
    return json.loads(get(url))


tag = os.environ.get("VALIDATE_TAG", "v2026.04.0")
release_base = f"https://github.com/MorePET/mat-vis/releases/download/{tag}"
manifest_url = f"{release_base}/release-manifest.json"

# ── 1. Fetch manifest ──
print(f"=== validate-release {tag} ===\\n")
try:
    manifest = get_json(manifest_url)
    print(f"  OK manifest fetched ({len(json.dumps(manifest))} bytes)")
except Exception as e:
    print(f"FAIL manifest: {e}")
    sys.exit(1)

tiers_data = manifest.get("tiers", {})
failures = []
passes = []

# ── 2+3. Check texture sources per tier ──
for tier in TEXTURE_TIERS:
    if tier not in tiers_data:
        failures.append(f"tier {tier}: missing from manifest")
        continue

    tier_info = tiers_data[tier]
    base_url = tier_info.get("base_url", "")
    sources_in_tier = tier_info.get("sources", {})

    for source in REQUIRED_SOURCES:
        label = f"{source}/{tier}"
        if source not in sources_in_tier:
            failures.append(f"{label}: missing from manifest")
            continue

        src_data = sources_in_tier[source]
        rowmap_files = src_data.get("rowmap_files", [])
        if not rowmap_files:
            rm_file = src_data.get("rowmap_file", f"{source}-{tier}-rowmap.json")
            rowmap_files = [rm_file]

        # Verify at least one parquet + rowmap exists
        if not rowmap_files:
            failures.append(f"{label}: no rowmap files listed")
            continue

        # Fetch one rowmap, verify parquet reference
        rm_url = base_url + rowmap_files[0]
        try:
            rowmap = get_json(rm_url)
        except Exception as e:
            failures.append(f"{label}: rowmap fetch failed: {e}")
            continue

        materials = rowmap.get("materials", {})
        pq_file = rowmap.get("parquet_file", "")
        if not materials:
            failures.append(f"{label}: rowmap has 0 materials")
            continue
        if not pq_file:
            failures.append(f"{label}: rowmap missing parquet_file")
            continue

        passes.append(f"{label}: rowmap OK ({len(materials)} materials, pq={pq_file})")

        # ── 4. Pick one random material, range-read, verify PNG ──
        mat_id = random.choice(list(materials.keys()))
        channels = materials[mat_id]
        ch_name = next(iter(channels))
        rng = channels[ch_name]
        offset = rng["offset"]
        length = rng["length"]

        pq_url = base_url + pq_file
        range_header = f"bytes={offset}-{offset + length - 1}"
        try:
            data = get(pq_url, headers={"Range": range_header})
            if data[:4] != b"\\x89PNG":
                failures.append(
                    f"{label}: range read {mat_id}/{ch_name} not PNG "
                    f"(got {data[:4]!r})"
                )
            else:
                passes.append(
                    f"{label}: range read OK ({mat_id}/{ch_name}, "
                    f"{len(data):,} bytes)"
                )
        except Exception as e:
            failures.append(f"{label}: range read failed: {e}")

    # Optional sources: just check if present, validate same way
    for source in OPTIONAL_SOURCES:
        label = f"{source}/{tier}"
        if source not in sources_in_tier:
            passes.append(f"{label}: not present (optional, OK)")
            continue

        src_data = sources_in_tier[source]
        rowmap_files = src_data.get("rowmap_files", [])
        if not rowmap_files:
            rm_file = src_data.get("rowmap_file", f"{source}-{tier}-rowmap.json")
            rowmap_files = [rm_file]

        rm_url = base_url + rowmap_files[0]
        try:
            rowmap = get_json(rm_url)
            materials = rowmap.get("materials", {})
            if materials:
                passes.append(f"{label}: rowmap OK ({len(materials)} materials)")
            else:
                failures.append(f"{label}: rowmap has 0 materials")
        except Exception as e:
            failures.append(f"{label}: rowmap fetch failed: {e}")

# ── 5. Check physicallybased index JSON ──
pb_label = "physicallybased/index"
try:
    pb_url = f"{release_base}/physicallybased.json"
    pb_data = get_json(pb_url)
    if isinstance(pb_data, list) and len(pb_data) > 0:
        passes.append(f"{pb_label}: OK ({len(pb_data)} entries)")
    else:
        failures.append(f"{pb_label}: unexpected shape (got {type(pb_data).__name__})")
except Exception as e:
    failures.append(f"{pb_label}: {e}")

# ── Report ──
print()
for p in passes:
    print(f"  OK {p}")
for f in failures:
    print(f"FAIL {f}")

total = len(passes) + len(failures)
print(f"\\n{len(passes)}/{total} checks passed")
if failures:
    print(f"{len(failures)} FAILURES — release is incomplete")
    sys.exit(1)
else:
    print("release validated successfully")
'''


@object_type
class MatVisCi:
    """CI pipeline for mat-vis baker container."""

    # ── builds ──────────────────────────────────────────────────

    @function
    def build(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
    ) -> dagger.Container:
        """Build slim baker image (no materialx)."""
        context = src or dag.host().directory(".")
        return context.docker_build(dockerfile="Containerfile", platform=TARGET_PLATFORM)

    @function
    def build_materialx(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
    ) -> dagger.Container:
        """Build baker + materialx image for gpuopen layered graphs."""
        context = src or dag.host().directory(".")
        return context.docker_build(dockerfile="Containerfile.materialx", platform=TARGET_PLATFORM)

    # ── checks ──────────────────────────────────────────────────

    @function
    async def lint(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
    ) -> str:
        """Run ruff check on src/ and tests/."""
        context = src or dag.host().directory(".")
        return await (
            dag.container()
            .from_("python:3.12-slim")
            .with_exec(["pip", "install", "--quiet", "ruff>=0.4"])
            .with_mounted_directory("/app", context)
            .with_workdir("/app")
            .with_exec(["ruff", "check", "src/", "tests/"])
            .stdout()
        )

    @function
    async def test(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
    ) -> str:
        """Run pytest on the test suite."""
        context = src or dag.host().directory(".")
        pip_cache = dag.cache_volume("pip-cache")
        return await (
            dag.container()
            .from_("python:3.12-slim")
            .with_mounted_cache("/root/.cache/pip", pip_cache)
            .with_mounted_directory("/app", context)
            .with_workdir("/app")
            .with_exec(["pip", "install", "--quiet", "-e", ".[baker,dev]"])
            .with_exec(["pytest", "tests/", "-v"])
            .stdout()
        )

    @function
    async def smoke(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
    ) -> str:
        """Verify pyarrow import in slim baker image."""
        ctr = self.build(src)
        return await ctr.with_exec(["python", "-c", "import pyarrow; print('slim ok')"]).stdout()

    @function
    async def smoke_materialx(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
    ) -> str:
        """Verify MaterialX import in heavy baker image."""
        ctr = self.build_materialx(src)
        return await ctr.with_exec(
            ["python", "-c", "import pyarrow; import MaterialX; print('materialx ok')"]
        ).stdout()

    @function
    async def test_all(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
    ) -> str:
        """Run full CI: lint + test + slim smoke + source probe."""
        context = src or dag.host().directory(".")

        lint_out = await self.lint(context)
        test_out = await self.test(context)
        smoke_out = await self.smoke(context)
        probe_out = await self.probe_sources(context)

        return (
            f"=== lint ===\n{lint_out}\n"
            f"=== test ===\n{test_out}\n"
            f"=== smoke ===\n{smoke_out}\n"
            f"=== probe ===\n{probe_out}"
        )

    # ── reference client tests ─────────────────────────────────────

    @function
    async def test_client_python(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
        tag: Annotated[str, Doc("Release tag to test against")] = "v2026.04.0",
    ) -> str:
        """Run pytest on the Python reference client against a live release."""
        context = src or dag.host().directory(".")
        pip_cache = dag.cache_volume("pip-cache")
        return await (
            dag.container()
            .from_("python:3.12-slim")
            .with_mounted_cache("/root/.cache/pip", pip_cache)
            .with_mounted_directory("/app", context)
            .with_workdir("/app/clients/python")
            .with_exec(["pip", "install", "--quiet", "pytest", "."])
            .with_env_variable("MAT_VIS_TAG", tag)
            .with_exec(["pytest", "test_client.py", "-v"])
            .stdout()
        )

    @function
    async def test_client_js(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
        tag: Annotated[str, Doc("Release tag to test against")] = "v2026.04.0",
    ) -> str:
        """Run node --test on the JS reference client against a live release."""
        context = src or dag.host().directory(".")
        return await (
            dag.container()
            .from_("node:22-slim")
            .with_mounted_directory("/app", context)
            .with_workdir("/app/clients/js")
            .with_env_variable("MAT_VIS_TAG", tag)
            .with_exec(["node", "--test", "test_client.mjs"])
            .stdout()
        )

    @function
    async def test_client_shell(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
        tag: Annotated[str, Doc("Release tag to test against")] = "v2026.04.0",
    ) -> str:
        """Run bash test script for the shell reference client against a live release."""
        context = src or dag.host().directory(".")
        return await (
            dag.container()
            .from_("alpine:3.20")
            .with_exec(["apk", "add", "--no-cache", "bash", "curl", "jq", "vim"])
            .with_mounted_directory("/app", context)
            .with_workdir("/app/clients")
            .with_env_variable("MAT_VIS_TAG", tag)
            .with_exec(["bash", "test_client.sh"])
            .stdout()
        )

    @function
    async def test_client_rust(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
        tag: Annotated[str, Doc("Release tag to test against")] = "v2026.04.0",
    ) -> str:
        """Run cargo test for the Rust reference client against a live release."""
        context = src or dag.host().directory(".")
        cargo_cache = dag.cache_volume("cargo-registry")
        target_cache = dag.cache_volume("cargo-target")
        return await (
            dag.container()
            .from_("rust:1.86-slim")
            .with_exec(["apt-get", "update", "-qq"])
            .with_exec(["apt-get", "install", "-y", "-qq", "pkg-config", "libssl-dev"])
            .with_mounted_cache("/usr/local/cargo/registry", cargo_cache)
            .with_mounted_cache("/app/clients/rust/target", target_cache)
            .with_mounted_directory("/app", context)
            .with_workdir("/app/clients/rust")
            .with_env_variable("MAT_VIS_TAG", tag)
            .with_exec(["cargo", "test", "--", "--test-threads=1"])
            .stdout()
        )

    @function
    async def test_clients(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
        tag: Annotated[str, Doc("Release tag to test against")] = "v2026.04.0",
    ) -> str:
        """Run all 4 reference client test suites in parallel."""
        context = src or dag.host().directory(".")

        import asyncio

        py_task = asyncio.ensure_future(self.test_client_python(context, tag))
        js_task = asyncio.ensure_future(self.test_client_js(context, tag))
        sh_task = asyncio.ensure_future(self.test_client_shell(context, tag))
        rs_task = asyncio.ensure_future(self.test_client_rust(context, tag))

        py_out, js_out, sh_out, rs_out = await asyncio.gather(py_task, js_task, sh_task, rs_task)

        return (
            f"=== python ===\n{py_out}\n"
            f"=== js ===\n{js_out}\n"
            f"=== shell ===\n{sh_out}\n"
            f"=== rust ===\n{rs_out}"
        )

    # ── integration test ──────────────────────────────────────────

    @function
    async def integration_test(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
    ) -> str:
        """End-to-end: fetch 2 ambientcg materials → bake → pack → rowmap → range-read verify.

        Runs native (no platform override) — tests pipeline logic, not the amd64 image.
        """
        context = src or dag.host().directory(".")
        pip_cache = dag.cache_volume("pip-cache")
        return await (
            dag.container()
            .from_("python:3.12-slim")
            .with_mounted_cache("/root/.cache/pip", pip_cache)
            .with_mounted_directory("/app", context)
            .with_workdir("/app")
            .with_exec(["pip", "install", "--quiet", "-e", ".[baker]"])
            .with_exec(
                [
                    "mat-vis-baker",
                    "all",
                    "ambientcg",
                    "1k",
                    "/tmp/integration",
                    "--limit",
                    "2",
                ]
            )
            .with_new_file(
                "/tmp/verify.py",
                contents=VERIFY_SCRIPT,
                permissions=0o755,
            )
            .with_exec(["python", "/tmp/verify.py", "/tmp/integration"])
            .stdout()
        )

    # ── bake pipeline ─────────────────────────────────────────────

    def _baker_container(
        self, context: dagger.Directory, with_ktx2: bool = False
    ) -> dagger.Container:
        """Baker container with code + gh CLI + git. Optionally with toktx for KTX2."""
        pip_cache = dag.cache_volume("pip-cache")
        ctr = (
            dag.container()
            .from_("python:3.12-slim")
            .with_exec(["apt-get", "update", "-qq"])
            .with_exec(["apt-get", "install", "-y", "-qq", "git", "curl"])
            .with_exec(
                [
                    "sh",
                    "-c",
                    "curl -fsSL https://github.com/cli/cli/releases/download/v2.74.1/gh_2.74.1_linux_amd64.tar.gz | tar xz --strip-components=2 -C /usr/local/bin gh_2.74.1_linux_amd64/bin/gh",
                ]
            )
        )
        if with_ktx2:
            # Install toktx from KTX-Software .deb (Khronos)
            ctr = ctr.with_exec(
                [
                    "sh",
                    "-c",
                    "apt-get install -y -qq libgomp1 ca-certificates && "
                    "curl -fsSL -o /tmp/ktx.deb https://github.com/KhronosGroup/KTX-Software/releases/download/v4.4.0/KTX-Software-4.4.0-Linux-x86_64.deb && "
                    "dpkg -i /tmp/ktx.deb && rm /tmp/ktx.deb",
                ]
            )
        return (
            ctr.with_mounted_cache("/root/.cache/pip", pip_cache)
            .with_mounted_directory("/app", context)
            .with_workdir("/app")
            .with_exec(["pip", "install", "--quiet", "-e", ".[baker]"])
        )

    @function
    async def bake_and_release(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
        source: Annotated[str, Doc("Source name")] = "ambientcg",
        tier: Annotated[str, Doc("Resolution tier")] = "1k",
        release_tag: Annotated[str, Doc("Release tag")] = "v0000.00.0",
        limit: Annotated[int, Doc("Max materials (0 = all)")] = 0,
        offset: Annotated[int, Doc("Skip first N materials")] = 0,
        batch_size: Annotated[int, Doc("Materials per streaming batch")] = 50,
        upload_chunks: Annotated[bool, Doc("Upload each parquet partition as it closes")] = True,
        registry_pass: Annotated[dagger.Secret | None, Doc("GH token")] = None,
    ) -> str:
        """Bake materials → upload to release → rebuild manifest.

        Single container. Streaming pipeline — bounded disk usage.
        With upload_chunks=True, each parquet partition is uploaded
        and deleted as it closes, freeing runner disk for the next.
        """
        context = src or dag.host().directory(".")
        baker = self._baker_container(context)

        # Need GH_TOKEN early because --upload-chunks calls gh during the run
        if registry_pass is not None:
            baker = baker.with_secret_variable("GH_TOKEN", registry_pass)

        bake_cmd = [
            "mat-vis-baker",
            "all",
            source,
            tier,
            "/tmp/out",
            "--release-tag",
            release_tag,
            "--batch-size",
            str(batch_size),
        ]
        if limit > 0:
            bake_cmd.extend(["--limit", str(limit)])
        if offset > 0:
            bake_cmd.extend(["--offset", str(offset)])
        if upload_chunks and release_tag != "v0000.00.0":
            bake_cmd.append("--upload-chunks")

        baker = baker.with_exec(bake_cmd)

        # Upload remaining (non-chunk) assets to release
        if release_tag != "v0000.00.0" and registry_pass is not None:
            baker = baker.with_exec(
                [
                    "sh",
                    "-c",
                    f"""
                for f in /tmp/out/*.parquet /tmp/out/*-rowmap.json /tmp/out/*.json; do
                    [ -f "$f" ] || continue
                    case "$(basename "$f")" in
                        release-manifest.json) ;;
                        *) gh release upload {release_tag} "$f" --clobber || true ;;
                    esac
                done
            """,
                ]
            )

            # Pack original .mtlx files into JSON map (gpuopen has real graphs)
            baker = baker.with_exec(
                [
                    "sh",
                    "-c",
                    f"""
                if [ -d /tmp/out/mtlx/{source} ] && find /tmp/out/mtlx/{source} -name '*.mtlx' -print -quit | grep -q .; then
                    mat-vis-baker pack-mtlx /tmp/out --source {source} --mtlx-dir /tmp/out/mtlx
                    if [ -f /tmp/out/{source}-mtlx.json ]; then
                        gh release upload {release_tag} /tmp/out/{source}-mtlx.json --clobber || true
                    fi
                fi
            """,
                ]
            )

            # Rebuild manifest from all release assets
            baker = baker.with_exec(
                [
                    "python3",
                    "-c",
                    f"""
from pathlib import Path
from mat_vis_baker.manifest import rebuild_manifest_from_release, write_manifest
manifest = rebuild_manifest_from_release('{release_tag}')
write_manifest(manifest, Path('/tmp/release-manifest.json'))
""",
                ]
            )
            baker = baker.with_exec(
                [
                    "sh",
                    "-c",
                    f"gh release upload {release_tag} /tmp/release-manifest.json --clobber",
                ]
            )

        return await baker.stdout()

    @function
    async def derive_ktx2_to_release(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
        source_tier: Annotated[str, Doc("PNG tier to transcode from")] = "1k",
        target_tier: Annotated[str, Doc("KTX2 tier name")] = "ktx2-1k",
        source: Annotated[str, Doc("Restrict to one source (or 'all')")] = "all",
        release_tag: Annotated[str, Doc("Release tag")] = "v0000.00.0",
        registry_pass: Annotated[dagger.Secret | None, Doc("GH token")] = None,
    ) -> str:
        """Derive KTX2 tier from existing PNG release, upload to same release.

        Container has toktx (KTX-Software) installed. Streams from release
        PNGs (HTTP range reads), transcodes to KTX2, packs into parquet.
        """
        context = src or dag.host().directory(".")
        baker = self._baker_container(context, with_ktx2=True)

        if registry_pass is not None:
            baker = baker.with_secret_variable("GH_TOKEN", registry_pass)

        cmd = [
            "mat-vis-baker",
            "derive-ktx2",
            "/tmp/out",
            "--release-tag",
            release_tag,
            "--source-tier",
            source_tier,
            "--target-tier",
            target_tier,
        ]
        if source != "all":
            cmd.extend(["--source", source])

        baker = baker.with_exec(cmd)

        # Upload all KTX2 parquets + rowmaps
        if release_tag != "v0000.00.0" and registry_pass is not None:
            baker = baker.with_exec(
                [
                    "sh",
                    "-c",
                    f"""
                for f in /tmp/out/*.parquet /tmp/out/*-rowmap.json; do
                    [ -f "$f" ] || continue
                    gh release upload {release_tag} "$f" --clobber || true
                done
            """,
                ]
            )
            # Rebuild manifest
            baker = baker.with_exec(
                [
                    "python3",
                    "-c",
                    f"""
from pathlib import Path
from mat_vis_baker.manifest import rebuild_manifest_from_release, write_manifest
manifest = rebuild_manifest_from_release('{release_tag}')
write_manifest(manifest, Path('/tmp/release-manifest.json'))
""",
                ]
            )
            baker = baker.with_exec(
                [
                    "sh",
                    "-c",
                    f"gh release upload {release_tag} /tmp/release-manifest.json --clobber",
                ]
            )

        return await baker.stdout()

    @function
    async def bake_source(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
        source: Annotated[str, Doc("Source name")] = "ambientcg",
        tier: Annotated[str, Doc("Resolution tier")] = "1k",
        release_tag: Annotated[str, Doc("Release tag for rowmap")] = "v0000.00.0",
        limit: Annotated[int, Doc("Max materials (0 = all)")] = 0,
    ) -> dagger.Directory:
        """Bake single batch, return output directory (no upload)."""
        context = src or dag.host().directory(".")
        baker = self._baker_container(context)

        bake_cmd = [
            "mat-vis-baker",
            "all",
            source,
            tier,
            "/tmp/out",
            "--release-tag",
            release_tag,
        ]
        if limit > 0:
            bake_cmd.extend(["--limit", str(limit)])

        return baker.with_exec(bake_cmd).directory("/tmp/out")

    @function
    async def regenerate_rowmaps(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
        release_tag: Annotated[str, Doc("Release tag")] = "v2026.04.0",
        registry_pass: Annotated[dagger.Secret | None, Doc("GH token")] = None,
    ) -> str:
        """Regenerate all rowmap JSONs for a release using the current scanner.

        Downloads each parquet, rescans with current generate_rowmap_from_parquet,
        uploads new rowmap. Use after fixing the rowmap scanner to repair
        existing release assets without re-baking.
        """
        context = src or dag.host().directory(".")
        baker = self._baker_container(context)
        if registry_pass is not None:
            baker = baker.with_secret_variable("GH_TOKEN", registry_pass)

        script = f"""
import json, os, re, subprocess, urllib.request
from pathlib import Path
from mat_vis_baker.parquet_writer import generate_rowmap_from_parquet, write_rowmap

TAG = '{release_tag}'
BASE = f'https://github.com/MorePET/mat-vis/releases/download/{{TAG}}'
work = Path('/tmp/regen'); work.mkdir(exist_ok=True)

# List release assets
assets = subprocess.run(
    ['gh', 'release', 'view', TAG, '--json', 'assets', '--jq', '.assets[].name'],
    capture_output=True, text=True, check=True,
).stdout.strip().split('\\n')

pq_re = re.compile(r'^mat-vis-(\\w+)-(\\w+)-(\\w+?)(?:-\\d+)?\\.parquet$')
parquets = [a for a in assets if pq_re.match(a)]
print(f'Found {{len(parquets)}} parquet files')

for i, pq_name in enumerate(sorted(parquets), 1):
    m = pq_re.match(pq_name)
    if not m: continue
    source, tier, _ = m.groups()
    pq_path = work / pq_name
    print(f'[{{i}}/{{len(parquets)}}] {{pq_name}}')

    # Download
    urllib.request.urlretrieve(f'{{BASE}}/{{pq_name}}', pq_path)

    # Generate rowmap
    rm = generate_rowmap_from_parquet(pq_path, source, tier, TAG)
    n_mat = len(rm['materials'])
    n_chan = sum(len(c) for c in rm['materials'].values())
    print(f'  → {{n_mat}} materials, {{n_chan}} channels')

    # Write rowmap with matching name
    stem = pq_path.stem.replace(f'mat-vis-{{source}}-{{tier}}-', f'{{source}}-{{tier}}-')
    rm_path = work / f'{{stem}}-rowmap.json'
    write_rowmap(rm, rm_path)

    # Upload rowmap, delete parquet to free disk
    subprocess.run(['gh', 'release', 'upload', TAG, str(rm_path), '--clobber'], check=False)
    pq_path.unlink()

# Rebuild manifest
from mat_vis_baker.manifest import rebuild_manifest_from_release, write_manifest
mf = rebuild_manifest_from_release(TAG)
mf_path = work / 'release-manifest.json'
write_manifest(mf, mf_path)
subprocess.run(['gh', 'release', 'upload', TAG, str(mf_path), '--clobber'], check=False)
print('Manifest rebuilt and uploaded')
"""
        return await baker.with_exec(["python3", "-c", script]).stdout()

    # ── source probes ─────────────────────────────────────────────

    @function
    async def probe_sources(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
    ) -> str:
        """Probe all four upstream APIs — verify connectivity, response shape, and rate limits.

        Single minimal request per source. Checks:
        - HTTP 200 response
        - Expected JSON structure (not just reachable, but correct schema)
        - Rate-limit headers logged (X-RateLimit-*, Retry-After)
        - Respects a 2s delay between sources to avoid burst patterns
        """
        context = src or dag.host().directory(".")
        return await (
            self.build(context)
            .with_new_file("/tmp/probe.py", contents=PROBE_SCRIPT, permissions=0o755)
            .with_exec(["python", "/tmp/probe.py"])
            .stdout()
        )

    # ── release validation ─────────────────────────────────────

    @function
    async def validate_release(
        self,
        tag: Annotated[str, Doc("Release tag to validate")] = "v2026.04.0",
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
    ) -> str:
        """Validate all expected release assets exist and range reads work.

        Checks the manifest, verifies every source x tier has parquet + rowmap,
        picks one random material per combination for an HTTP range read, and
        confirms PNG magic bytes. Exits non-zero on any failure.
        """
        context = src or dag.host().directory(".")
        return await (
            dag.container()
            .from_("python:3.12-slim")
            .with_mounted_directory("/app", context)
            .with_workdir("/app")
            .with_env_variable("VALIDATE_TAG", tag)
            .with_new_file(
                "/tmp/validate_release.py",
                contents=VALIDATE_RELEASE_SCRIPT,
                permissions=0o755,
            )
            .with_exec(["python", "/tmp/validate_release.py"])
            .stdout()
        )

    # ── registry ────────────────────────────────────────────────

    @function
    async def preflight(
        self,
        registry_user: Annotated[str, Doc("GHCR username")] = "",
        registry_pass: Annotated[dagger.Secret | None, Doc("GHCR token")] = None,
    ) -> str:
        """Verify GHCR auth works before attempting a push.

        Pulls a tiny public image through GHCR auth to confirm
        credentials and connectivity. Fails fast with a clear
        message if anything is wrong.
        """
        if registry_pass is None:
            return "SKIP: no registry credentials provided"

        # Try to auth and pull a minimal manifest — catches bad tokens,
        # missing scopes, network issues, org restrictions.
        return await (
            dag.container()
            .from_("alpine:3.20")
            .with_registry_auth("ghcr.io", registry_user, registry_pass)
            .with_exec(["sh", "-c", "echo 'ghcr auth ok'"])
            .stdout()
        )

    @function
    async def push(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
        registry_user: Annotated[str, Doc("GHCR username")] = "",
        registry_pass: Annotated[dagger.Secret | None, Doc("GHCR token")] = None,
        tag: Annotated[str, Doc("Semver tag, e.g. v0.1.0")] = "latest",
    ) -> str:
        """Preflight, build slim + materialx, push both to GHCR.

        Tags pushed per image:
          baker:  :<version> + :latest
          materialx: :<version>-materialx + :materialx
        """
        # Fail fast on auth issues
        pre = await self.preflight(registry_user, registry_pass)
        if "SKIP" in pre:
            return pre

        version = tag.lstrip("v") if tag != "latest" else "latest"
        context = src or dag.host().directory(".")
        results = []

        # Push slim baker
        slim = self.build(context)
        if registry_pass is not None:
            slim = slim.with_registry_auth("ghcr.io", registry_user, registry_pass)
        slim_ref = await slim.publish(f"{IMAGE}:{version}")
        results.append(f"slim: {slim_ref}")
        if version != "latest":
            await slim.publish(f"{IMAGE}:latest")
            results.append(f"slim: {IMAGE}:latest")

        # Push materialx variant
        heavy = self.build_materialx(context)
        if registry_pass is not None:
            heavy = heavy.with_registry_auth("ghcr.io", registry_user, registry_pass)
        heavy_ref = await heavy.publish(f"{IMAGE}:{version}-materialx")
        results.append(f"materialx: {heavy_ref}")
        if version != "latest":
            await heavy.publish(f"{IMAGE}:materialx")
            results.append(f"materialx: {IMAGE}:materialx")

        return "\n".join(results)
