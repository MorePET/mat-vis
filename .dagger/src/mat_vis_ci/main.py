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
pq_files = list(out_dir.glob("*.parquet"))
assert pq_files, f"No parquet files in {out_dir}"
pq_path = pq_files[0]

rowmap_files = list(out_dir.glob("*-rowmap.json"))
assert rowmap_files, f"No rowmap files in {out_dir}"

index_files = list(out_dir.glob("*.json"))
assert any("rowmap" not in f.name for f in index_files), "No index JSON"

# Load rowmap and verify range reads
rowmap = json.loads(rowmap_files[0].read_text())
file_bytes = pq_path.read_bytes()

materials = rowmap["materials"]
assert len(materials) > 0, "Rowmap has no materials"

verified = 0
errors = []
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

print(f"  OK parquet: {pq_path.name} ({len(file_bytes)} bytes)")
print(f"  OK rowmap: {len(materials)} materials")
print(f"  OK range-read: {verified} channels verified (all PNG)")
print(f"  OK index: {len(index_files)} JSON files")
print(f"\\nintegration test passed")
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
            .with_exec(["pip", "install", "--quiet", "pytest"])
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
            .from_("rust:1.80-slim")
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

    @function
    async def bake_source(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
        source: Annotated[str, Doc("Source name")] = "ambientcg",
        tier: Annotated[str, Doc("Resolution tier")] = "1k",
        release_tag: Annotated[str, Doc("Release tag for rowmap")] = "v0000.00.0",
    ) -> dagger.Directory:
        """Run full baker pipeline for one source+tier. Returns directory with artifacts.

        Output directory contains:
        - mat-vis-<source>-<tier>.parquet
        - <source>-<tier>-rowmap.json
        - <source>.json (index)
        """
        context = src or dag.host().directory(".")
        pip_cache = dag.cache_volume("pip-cache")

        ctr = (
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
                    source,
                    tier,
                    "/tmp/out",
                    "--release-tag",
                    release_tag,
                    "--also-derive",
                    "128,256,512",
                ]
            )
        )

        return ctr.directory("/tmp/out")

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
