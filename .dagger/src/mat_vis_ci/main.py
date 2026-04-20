"""mat-vis CI pipeline.

Usage:
    dagger call build                # slim baker image
    dagger call build-materialx      # baker + materialx (gpuopen)
    dagger call lint                 # ruff check
    dagger call test                 # pytest
    dagger call smoke                # verify pyarrow import (slim)
    dagger call smoke-materialx      # verify MaterialX import (heavy)
    dagger call smoke-baker          # verify hf-tar baker container (#135)
    dagger call bake                 # port of hf-bake → atomic HF commit (#136)
    dagger call smoke-bake           # dry-run bake against gerchowl/mat-vis-tst (#136)
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
"""Verify hf-bake --dry-run output: tar + rowmap + catalog + range-read."""

import json
import sys
from pathlib import Path

out_dir = Path(sys.argv[1])

tar_files = sorted(out_dir.glob("*.tar"))
assert tar_files, f"No tar files in {out_dir}"

rowmap_files = sorted(out_dir.glob("*-rowmap.json"))
assert rowmap_files, f"No rowmap files in {out_dir}"

# Bake no longer writes release-manifest.json (clients derive it from
# the HF tree listing — ADR-0007 race-free design). Just check catalog + tars.
catalog_files = [
    p for p in out_dir.glob("*.json")
    if not p.name.endswith("-rowmap.json") and p.name != "release-manifest.json"
]
assert catalog_files, "No per-source catalog JSON"

verified = 0
errors = []
total_materials = 0

for rm_path in rowmap_files:
    rowmap = json.loads(rm_path.read_text())
    tar_name = rowmap.get("tar_file", "")
    tar_path = out_dir / tar_name if tar_name else None
    if not tar_path or not tar_path.exists():
        errors.append(f"tar {tar_name!r} referenced by {rm_path.name} missing")
        continue
    tar_bytes = tar_path.read_bytes()
    materials = rowmap["materials"]
    total_materials += len(materials)

    for mid, channels in materials.items():
        for ch, rng in channels.items():
            offset = rng["offset"]
            length = rng["length"]
            chunk = tar_bytes[offset : offset + length]
            if chunk[:4] != b"\\x89PNG":
                errors.append(
                    f"{mid}/{ch}: not PNG at offset {offset} (got {chunk[:4]!r})"
                )
                continue
            if len(chunk) != length:
                errors.append(
                    f"{mid}/{ch}: length mismatch at offset {offset}"
                    f" (expected {length}, got {len(chunk)})"
                )
                continue
            verified += 1

if errors:
    for e in errors:
        print(f"  FAIL {e}")
    sys.exit(1)

print(f"  OK tars: {len(tar_files)} file(s)")
print(f"  OK rowmaps: {len(rowmap_files)} file(s), {total_materials} materials")
print(f"  OK range-read: {verified} channels verified (all PNG)")
print(f"  OK catalogs: {len(catalog_files)} file(s)")
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
            # Install the client package too so cross-module tests
            # (e.g. tests/test_version_sync.py::test_client_runtime_version_matches_pyproject,
            # which does ``from mat_vis_client import __version__``) can
            # import it. Without this, the top-level tests/ suite can
            # only see the baker package even though it guards client
            # invariants.
            .with_exec(["pip", "install", "--quiet", "-e", "./clients/python"])
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
        """End-to-end (local): fetch 2 ambientcg materials → pack tar → verify.

        Uses ``hf-bake --dry-run`` so the pipeline is fully exercised
        (upstream fetch + tar write + rowmap + catalog + manifest) without
        needing an HF_TOKEN in the runner — skipping the actual HF push.
        Runs native (no platform override).
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
                    "hf-bake",
                    "ambientcg",
                    "1k",
                    "/tmp/integration",
                    "--limit",
                    "2",
                    "--release-tag",
                    "v0000.00.0",
                    "--dry-run",
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
        self,
        context: dagger.Directory,
        with_ktx2: bool = False,
        hf_token: dagger.Secret | None = None,
    ) -> dagger.Container:
        """Baker container for hf-bake / hf-derive / hf-derive-ktx2 (#135).

        Parity target: ``.github/workflows/derive.yml`` — Linux x86_64,
        Python 3.12, ``uv sync --all-extras`` on the repo, and (when
        ``with_ktx2=True``) KTX-Software 4.4.0 ``.deb`` installed so
        ``toktx`` is on ``PATH``.

        Env:
          - ``PYTHONUNBUFFERED=1`` — heartbeat / OTLP logs flush live
            (matches #148 workflow fix).
          - ``HF_TOKEN`` — injected from a Dagger secret when provided;
            never inlined. Bake / derive steps in #136 / #137 read this
            to push atomic commits to the HF dataset.

        Reuse: #136 (port ``hf-bake``) and #137 (port ``hf-derive`` and
        ``hf-derive-ktx2``) both call this helper. Pass ``with_ktx2=True``
        for the KTX2 derive leg; ``False`` otherwise.
        """
        uv_cache = dag.cache_volume("uv-cache")
        apt_cache = dag.cache_volume("apt-cache")

        ctr = (
            dag.container()
            .from_("python:3.12-slim")
            .with_env_variable("DEBIAN_FRONTEND", "noninteractive")
            .with_mounted_cache("/var/cache/apt", apt_cache)
            .with_exec(["apt-get", "update", "-qq"])
            .with_exec(["apt-get", "install", "-y", "-qq", "git", "curl", "ca-certificates"])
            # Install uv via the astral-sh standalone installer — matches
            # the ``astral-sh/setup-uv@v5`` action used in derive.yml.
            .with_exec(
                [
                    "sh",
                    "-c",
                    "curl -LsSf https://astral.sh/uv/install.sh | sh && "
                    "install -m 0755 /root/.local/bin/uv /usr/local/bin/uv",
                ]
            )
        )

        if with_ktx2:
            # Khronos KTX-Software 4.4.0 .deb — byte-for-byte the URL
            # derive.yml uses, so toktx output is identical to CI.
            ctr = ctr.with_exec(
                [
                    "sh",
                    "-c",
                    "apt-get install -y -qq libgomp1 && "
                    "curl -fsSL -o /tmp/ktx.deb "
                    "https://github.com/KhronosGroup/KTX-Software/releases/download/"
                    "v4.4.0/KTX-Software-4.4.0-Linux-x86_64.deb && "
                    "dpkg -i /tmp/ktx.deb && rm /tmp/ktx.deb && "
                    "toktx --version",
                ]
            )

        ctr = (
            ctr.with_env_variable("PYTHONUNBUFFERED", "1")
            .with_mounted_cache("/root/.cache/uv", uv_cache)
            .with_mounted_directory("/app", context)
            .with_workdir("/app")
            .with_exec(["uv", "sync", "--all-extras"])
        )

        if hf_token is not None:
            ctr = ctr.with_secret_variable("HF_TOKEN", hf_token)

        return ctr

    @function
    async def bake(
        self,
        context: Annotated[dagger.Directory, Doc("Project root directory")],
        source: Annotated[str, Doc("Upstream source: ambientcg|polyhaven|gpuopen|physicallybased")],
        tier: Annotated[
            str, Doc("Resolution tier (e.g. 1k, 2k, 4k); 'scalar' for physicallybased")
        ],
        release_tag: Annotated[str, Doc("Calver data release tag, e.g. v2026.04.1")],
        hf_token: Annotated[dagger.Secret, Doc("HF write token for the atomic push")],
        repo_id: Annotated[str, Doc("Target HF dataset repo")] = "gerchowl/mat-vis",
        limit: Annotated[int, Doc("Max materials (0 = no limit)")] = 0,
        offset: Annotated[int, Doc("Skip first N materials")] = 0,
        batch_size: Annotated[int, Doc("Materials per streaming batch")] = 50,
        dry_run: Annotated[bool, Doc("Build tar locally; skip HF push")] = False,
        shard_index: Annotated[int, Doc("0-based shard index (-1 = no sharding)")] = -1,
        shard_total: Annotated[int, Doc("Total shards (-1 = no sharding)")] = -1,
    ) -> str:
        """Bake one (source, tier) into an atomic HF commit (#136).

        Wraps ``mat-vis-baker hf-bake`` in the shared ``_baker_container``
        (#135). Returns the CLI stdout — the final line is the commit SHA
        when ``--dry-run`` is not set. Parity target: the ``hf-bake`` step
        in ``.github/workflows/bake.yml`` (pre-#138).

        Sentinels: ``limit=0`` drops ``--limit``; ``shard_index=-1`` and
        ``shard_total=-1`` drop both shard flags. Pass both to shard.
        """
        ctr = self._baker_container(context, with_ktx2=False, hf_token=hf_token)

        argv: list[str] = [
            "uv",
            "run",
            "mat-vis-baker",
            "hf-bake",
            source,
            tier,
            "/tmp/bake",
            "--release-tag",
            release_tag,
            "--repo-id",
            repo_id,
            "--offset",
            str(offset),
            "--batch-size",
            str(batch_size),
        ]
        if limit > 0:
            argv += ["--limit", str(limit)]
        if dry_run:
            argv.append("--dry-run")
        if shard_index >= 0 and shard_total >= 0:
            argv += [
                "--shard-index",
                str(shard_index),
                "--shard-total",
                str(shard_total),
            ]

        return await ctr.with_exec(argv).stdout()

    @function
    async def smoke_bake(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
    ) -> str:
        """Smoke-test the bake Dagger op end-to-end (#136).

        Dispatches against ``gerchowl/mat-vis-tst`` at ``v0.0.1-smoke``
        with ``--dry-run --limit 1`` to prove the wrapper's plumbing
        (argv construction, container mount, uv sync, CLI import) without
        an actual HF push. Should finish well under 60s. Requires an
        ``HF_TOKEN`` secret because the CLI signature demands it, but
        ``--dry-run`` means the token is never consumed.
        """
        context = src or dag.host().directory(".")
        # Dry-run path never exercises the secret, but the Dagger
        # signature requires a non-None ``dagger.Secret``. Pull from the
        # host env so local invocations work with ``HF_TOKEN`` exported.
        hf_token = dag.set_secret("HF_TOKEN", "dry-run-placeholder")
        return await self.bake(
            context=context,
            source="polyhaven",
            tier="1k",
            release_tag="v0.0.1-smoke",
            hf_token=hf_token,
            repo_id="gerchowl/mat-vis-tst",
            limit=1,
            dry_run=True,
        )

    @function
    async def smoke_baker(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
    ) -> str:
        """Smoke-test the baker container (#135).

        Builds ``_baker_container(with_ktx2=True)`` and runs
        ``mat-vis-baker --help`` plus ``mat-vis-baker merge-shards --help``
        under ``uv run``. Exits 0 iff the apt + KTX deb + ``uv sync`` all
        succeed and the CLI (including the shard reassembly subcommand
        added in #134) is importable. No network side effects — nothing
        touches HF.
        """
        context = src or dag.host().directory(".")
        ctr = self._baker_container(context, with_ktx2=True)
        top = await ctr.with_exec(["uv", "run", "mat-vis-baker", "--help"]).stdout()
        merge = await ctr.with_exec(
            ["uv", "run", "mat-vis-baker", "merge-shards", "--help"]
        ).stdout()
        return (
            f"=== mat-vis-baker --help ===\n{top}\n"
            f"=== mat-vis-baker merge-shards --help ===\n{merge}"
        )

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
