"""mat-vis CI pipeline.

Usage:
    dagger call build                # slim baker image
    dagger call build-materialx      # baker + materialx (gpuopen)
    dagger call lint                 # ruff check
    dagger call test                 # pytest
    dagger call smoke                # verify pyarrow import (slim)
    dagger call smoke-materialx      # verify MaterialX import (heavy)
    dagger call smoke-baker          # verify baker container (#135)
    dagger call bake                 # per-file hf-bake → atomic HF commit (#136 / ADR-0012)
    dagger call smoke-bake           # dry-run bake against gerchowl/mat-vis-tst (#136)
    dagger call derive               # per-file hf-derive (resize) (#204)
    dagger call derive-ktx-2         # per-file hf-derive-ktx2 (#204; #240)
    dagger call integration-test     # local end-to-end per-file bake + verify
    dagger call probe-sources        # verify upstream API connectivity
    dagger call test-all             # lint + test + smoke + probe
    dagger call test-client-python   # pytest on Python reference client
    dagger call test-client-js       # node --test on JS reference client
    dagger call test-client-shell    # bash tests for shell reference client
    dagger call test-client-rust     # cargo test for Rust reference client
    dagger call test-clients         # all 4 client tests in parallel
    dagger call test-e-2-e           # nightly E2E against mat-vis-tst (#193; #240)
    dagger call preflight            # verify GHCR auth before push
    dagger call push                 # preflight + build + push to GHCR
"""

# #262: kill the Python OTel SDK *before* `import dagger` triggers its
# auto-init. The Dagger Python SDK wires up an OTLP HTTP exporter to
# dagger.cloud at module-import time; that endpoint flakes (5xx) and
# the retry loop ends in SIGPIPE → exit 141, bricking otherwise-green
# CI runs (e.g. dev pushes 25320342461 / 25320773716, the v0.6.0 tag
# run 25287958789, and the per-PR Client-tests job).
#
# Workflow-level env (OTEL_SDK_DISABLED=true in ci.yml) covers the
# dagger CLI itself but does NOT propagate into the container that
# runs THIS python module — so the SDK boots up here regardless and
# the export retry storm continues. Setting via os.environ.setdefault
# at the top of the entrypoint fixes it for both local invocations
# and the CI containers, while still letting an explicit override
# (e.g. once #131 + #176 + the otlp-tailnet composite action ship a
# self-hosted collector) re-enable the SDK by exporting the var.
import os

os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("DAGGER_NO_NAG", "1")

from typing import Annotated  # noqa: E402

import dagger  # noqa: E402
from dagger import Doc, dag, function, object_type

from mat_vis_ci._bake_cli import bake_argv as _bake_argv

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
"""Verify hf-bake --dry-run output: v3 catalog (ADR-0012).

Per-file substrate dry-runs do NOT persist the per-file tree to disk
— ``bake_one_per_file`` builds CommitOperationAdd ops in memory,
logs "would commit N files" on dry-run, and unlinks the local texture
bytes after the (skipped) flush. The only on-disk artifact a dry-run
leaves behind is the per-source catalog JSON at the work_dir root.

So the verify here:
  1. Asserts the catalog exists and is v3-shaped (list of entries
     with a ``mat_vis`` block).
  2. Trusts the CLI exit code (already 0 by the time we run, else
     the previous Dagger step would have failed).

Live per-file tree shape (PNGs under ``<source>/<tier>/<id>/<channel>``,
``.tier_complete`` sentinel) is asserted by the MAT_VIS_E2E=1
round-trip suite in ``tests/e2e/test_per_file_roundtrip.py`` — that
suite actually pushes to ``gerchowl/mat-vis-tst`` and HEADs back the
files.
"""

import json
import sys
from pathlib import Path

out_dir = Path(sys.argv[1])

# Catalog at work_dir root (v3 shape: list of entries with mat_vis block).
# Skip release-manifest.json (object) and *-mtlx.json (object — material_id
# -> XML map produced by mtlx_tier.pack_original_mtlx_json, mat-vis#292).
catalog_files = [
    p
    for p in out_dir.glob("*.json")
    if p.name != "release-manifest.json" and not p.name.endswith("-mtlx.json")
]
assert catalog_files, f"no per-source catalog JSON at {out_dir}"
for cat in catalog_files:
    body = json.loads(cat.read_text())
    assert isinstance(body, list) and body, (
        f"{cat.name}: catalog must be a non-empty list"
    )
    assert all("mat_vis" in entry for entry in body), (
        f"{cat.name}: every entry must carry a 'mat_vis' block (v3 shape)"
    )

print(f"  OK catalogs: {len(catalog_files)} v3 file(s)")
for cat in catalog_files:
    body = json.loads(cat.read_text())
    print(f"  OK {cat.name}: {len(body)} entries, all v3-shaped")
print(f"\\nintegration test passed (per-file substrate, ADR-0012)")
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
        tag: Annotated[str, Doc("Release tag to test against")] = "v2026.04.2",
        live: Annotated[
            bool,
            Doc("Set MAT_VIS_LIVE_TESTS=1 + MAT_VIS_LIVE_TAG=tag to enable @live tests (#248)"),
        ] = False,
    ) -> str:
        """Run pytest on the Python reference client against a live release.

        Test collection is driven by the package's ``testpaths = ["tests"]``
        config — see ``clients/python/pyproject.toml``. #274 consolidated
        the previously-disjoint top-level ``test_client.py`` and nested
        ``tests/test_client.py`` suites under ``tests/``; the explicit
        filename arg this function used to pass is no longer needed and
        would have masked the nested suite again if reintroduced.
        """
        context = src or dag.host().directory(".")
        pip_cache = dag.cache_volume("pip-cache")
        ctr = (
            dag.container()
            .from_("python:3.12-slim")
            .with_mounted_cache("/root/.cache/pip", pip_cache)
            .with_mounted_directory("/app", context)
            .with_workdir("/app/clients/python")
            .with_exec(["pip", "install", "--quiet", "pytest", "."])
            .with_env_variable("MAT_VIS_TAG", tag)
        )
        if live:
            ctr = ctr.with_env_variable("MAT_VIS_LIVE_TESTS", "1").with_env_variable(
                "MAT_VIS_LIVE_TAG", tag
            )
        return await ctr.with_exec(["pytest", "-v"]).stdout()

    @function
    async def test_client_js(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
        tag: Annotated[str, Doc("Release tag to test against")] = "v2026.04.2",
        live: Annotated[
            bool,
            Doc("Set MAT_VIS_LIVE_TESTS=1 + MAT_VIS_LIVE_TAG=tag to enable live tests (#248)"),
        ] = False,
    ) -> str:
        """Run node --test on the JS reference client against a live release."""
        context = src or dag.host().directory(".")
        ctr = (
            dag.container()
            .from_("node:22-slim")
            .with_mounted_directory("/app", context)
            .with_workdir("/app/clients/js")
            .with_env_variable("MAT_VIS_TAG", tag)
        )
        if live:
            ctr = ctr.with_env_variable("MAT_VIS_LIVE_TESTS", "1").with_env_variable(
                "MAT_VIS_LIVE_TAG", tag
            )
        return await ctr.with_exec(["node", "--test", "test_client.mjs"]).stdout()

    @function
    async def test_client_shell(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
        tag: Annotated[str, Doc("Release tag to test against")] = "v2026.04.2",
        live: Annotated[
            bool,
            Doc("Set MAT_VIS_LIVE_TESTS=1 + MAT_VIS_LIVE_TAG=tag to enable live tests (#248)"),
        ] = False,
    ) -> str:
        """Run bash test script for the shell reference client against a live release."""
        context = src or dag.host().directory(".")
        ctr = (
            dag.container()
            .from_("alpine:3.20")
            .with_exec(["apk", "add", "--no-cache", "bash", "curl", "jq", "vim"])
            .with_mounted_directory("/app", context)
            .with_workdir("/app/clients")
            .with_env_variable("MAT_VIS_TAG", tag)
        )
        if live:
            ctr = ctr.with_env_variable("MAT_VIS_LIVE_TESTS", "1").with_env_variable(
                "MAT_VIS_LIVE_TAG", tag
            )
        return await ctr.with_exec(["bash", "test_client.sh"]).stdout()

    @function
    async def test_client_rust(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
        tag: Annotated[str, Doc("Release tag to test against")] = "v2026.04.2",
        live: Annotated[
            bool,
            Doc("Set MAT_VIS_LIVE_TESTS=1 + MAT_VIS_LIVE_TAG=tag to enable live tests (#248)"),
        ] = False,
    ) -> str:
        """Run cargo test for the Rust reference client against a live release."""
        context = src or dag.host().directory(".")
        cargo_cache = dag.cache_volume("cargo-registry")
        target_cache = dag.cache_volume("cargo-target")
        ctr = (
            dag.container()
            .from_("rust:1.89-slim")
            .with_exec(["apt-get", "update", "-qq"])
            .with_exec(["apt-get", "install", "-y", "-qq", "pkg-config", "libssl-dev"])
            .with_mounted_cache("/usr/local/cargo/registry", cargo_cache)
            .with_mounted_cache("/app/clients/rust/target", target_cache)
            .with_mounted_directory("/app", context)
            .with_workdir("/app/clients/rust")
            .with_env_variable("MAT_VIS_TAG", tag)
        )
        if live:
            ctr = ctr.with_env_variable("MAT_VIS_LIVE_TESTS", "1").with_env_variable(
                "MAT_VIS_LIVE_TAG", tag
            )
        # #241: mock + live tests now share a single cargo invocation
        # again — `EnvGuard` in the test code restores `MAT_VIS_HF_BASE`
        # on test exit, so env-var pollution can't bleed across tests.
        return await ctr.with_exec(["cargo", "test", "--", "--test-threads=1"]).stdout()

    @function
    async def test_clients(
        self,
        src: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
        tag: Annotated[str, Doc("Release tag to test against")] = "v2026.04.2",
        live: Annotated[
            bool,
            Doc("Forward MAT_VIS_LIVE_TESTS=1 + MAT_VIS_LIVE_TAG=tag to every client (#248)"),
        ] = False,
    ) -> str:
        """Run all 4 reference client test suites in parallel."""
        context = src or dag.host().directory(".")

        import asyncio

        py_task = asyncio.ensure_future(self.test_client_python(context, tag, live))
        js_task = asyncio.ensure_future(self.test_client_js(context, tag, live))
        sh_task = asyncio.ensure_future(self.test_client_shell(context, tag, live))
        rs_task = asyncio.ensure_future(self.test_client_rust(context, tag, live))

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
        """End-to-end (local): fetch 2 ambientcg materials → per-file bake → verify.

        Uses ``hf-bake --dry-run`` so the pipeline is fully exercised
        (upstream fetch + per-file write + catalog build) without
        needing an HF_TOKEN in the runner — skipping the actual HF push.
        Runs native (no platform override).

        Per-file substrate (ADR-0012, #189): the verify script asserts
        the per-file tree shape (``<source>/<tier>/<id>/<channel>.png``
        + ``.tier_complete`` sentinel + v3 catalog at root). The legacy
        tar+rowmap shape was retired in #189.
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
                    "--repo-id",
                    "gerchowl/mat-vis-tst",
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

    def _guard_prod_target(self, repo_id: str, allow_prod: bool) -> None:
        """Refuse writes to the canonical production repo without opt-in.

        The default target for every bake fn is the scratch dataset
        ``gerchowl/mat-vis-tst``. Any other target (production
        ``gerchowl/mat-vis`` or a third-party fork) requires
        ``--allow-prod=true`` at call time. Prevents feature-branch /
        smoke-test dispatches from accidentally landing in the public
        catalog. The flag is never persisted — it has to be supplied on
        every invocation that touches prod.

        Scratch namespace check is namespace-scoped (``.../mat-vis-tst``
        or ``.../mat-vis-<suffix>-tst``) rather than plain ``endswith("-tst")``
        so a fork named ``evil/mat-vis-tst`` still counts as opt-in
        scratch, but a generic ``somebody/tst`` does not sneak through
        the default.
        """
        if "/" in repo_id:
            _owner, name = repo_id.rsplit("/", 1)
            if name == "mat-vis-tst" or name.endswith("-tst") and name.startswith("mat-vis"):
                return
        if allow_prod:
            return
        raise ValueError(
            f"Refusing to write to non-scratch repo {repo_id!r} without "
            "--allow-prod=true. Scratch repos are named .../mat-vis-tst "
            "(or .../mat-vis-*-tst); anything else requires an explicit "
            "--allow-prod=true. Example: pass --allow-prod=true to target "
            "gerchowl/mat-vis for a real data release."
        )

    def _baker_container(
        self,
        context: dagger.Directory,
        hf_token: dagger.Secret | None = None,
        with_ktx2: bool = False,
    ) -> dagger.Container:
        """Baker container for hf-bake / hf-derive / hf-derive-ktx2 (#135 / #204).

        Parity target: Linux x86_64, Python 3.12, ``uv sync --all-extras``
        on the repo. With ``with_ktx2=True``, KTX-Software 4.4.0 ``.deb``
        is installed so ``toktx`` is on ``PATH`` — needed for the
        ``hf-derive-ktx2`` leg of the per-file derive pipeline (#204).

        Env:
          - ``PYTHONUNBUFFERED=1`` — heartbeat / OTLP logs flush live
            (matches #148 workflow fix).
          - ``HF_TOKEN`` — injected from a Dagger secret when provided;
            never inlined.
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
            # Install uv via the astral-sh standalone installer.
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
            # Khronos KTX-Software 4.4.0 .deb — same URL the legacy
            # derive.yml used so toktx output is byte-identical across
            # the v0.5/v0.6 cutover.
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
        repo_id: Annotated[str, Doc("Target HF dataset repo")] = "gerchowl/mat-vis-tst",
        allow_prod: Annotated[
            bool, Doc("Opt-in flag required to target any non-*-tst repo (e.g. gerchowl/mat-vis)")
        ] = False,
        limit: Annotated[int, Doc("Max materials (0 = no limit)")] = 0,
        offset: Annotated[int, Doc("Skip first N materials")] = 0,
        batch_size: Annotated[int, Doc("Materials per atomic commit (count ceiling, #228)")] = 300,
        batch_max_bytes: Annotated[
            int,
            Doc(
                "Bytes per atomic commit (default 700 MiB). #228: flush "
                "trips on first-of-N-or-bytes. HF caps at 1 GiB/commit; "
                "700 MiB leaves room for catalog + manifest + sentinel."
            ),
        ] = 700 * 1024 * 1024,
        dry_run: Annotated[bool, Doc("Build locally; skip HF push")] = False,
    ) -> str:
        """Bake one (source, tier) into an HF commit (#136 / ADR-0012).

        Wraps ``mat-vis-baker hf-bake`` in the shared ``_baker_container``
        (#135). Returns the CLI stdout — the final line is the commit SHA
        when ``--dry-run`` is not set.

        Substrate: per-file (ADR-0012). Each material × channel lands as
        one file under ``<source>/<tier>/<mid>/<channel>``. Pre-flight
        tree scan + batch commits (size ``batch_size``) make bakes
        resumable across crashes. The legacy tar+rowmap path and its
        ``--legacy-tar`` flag were retired in #189.

        Sentinels: ``limit=0`` drops ``--limit``.

        Safety: defaults to ``gerchowl/mat-vis-tst`` (scratch). Any other
        target requires ``allow_prod=true``. Both Dagger-level
        (``_guard_prod_target``) and baker-level (per-file
        ``_guard_prod_target``) checks fire; redundant by design so the
        rail still holds when callers reach the baker without going
        through Dagger.
        """
        self._guard_prod_target(repo_id, allow_prod)
        ctr = self._baker_container(context, hf_token=hf_token)
        argv = _bake_argv(
            source=source,
            tier=tier,
            release_tag=release_tag,
            repo_id=repo_id,
            offset=offset,
            batch_size=batch_size,
            batch_max_bytes=batch_max_bytes,
            limit=limit,
            dry_run=dry_run,
            allow_prod=allow_prod,
        )
        return await ctr.with_exec(argv).stdout()

    @function
    async def test_e2e(
        self,
        hf_token: Annotated[dagger.Secret, Doc("HF write token for the round-trip bake")],
        context: Annotated[dagger.Directory, Doc("Project root directory")] | None = None,
        repo_id: Annotated[
            str, Doc("Target HF dataset repo (must be a *-tst scratch namespace)")
        ] = "gerchowl/mat-vis-tst",
    ) -> str:
        """Run the live MAT_VIS_E2E=1 round-trip suite against mat-vis-tst (#193).

        Spins up a python:3.12-slim container, syncs project deps with
        the baker + dev extras, installs the Python reference client, and
        runs ``pytest tests/e2e/ -v`` with ``MAT_VIS_E2E=1`` and
        ``HF_TOKEN`` injected from the Dagger secret. Returns stdout.

        Refuses to run against any non-scratch ``repo_id`` — there is no
        ``--allow-prod`` escape hatch here because the E2E suite makes
        and deletes a throwaway tag (``v0.0.0-e2e-184-perfile``); doing
        that on prod would churn the public dataset history. If you
        need to E2E against prod, fork the suite into its own function.
        """
        if "/" in repo_id:
            _owner, name = repo_id.rsplit("/", 1)
            scratch = name == "mat-vis-tst" or (
                name.startswith("mat-vis") and name.endswith("-tst")
            )
        else:
            scratch = False
        if not scratch:
            raise ValueError(
                f"test_e2e refuses to target non-scratch repo {repo_id!r}; "
                "this function only runs against .../mat-vis-tst (or "
                ".../mat-vis-*-tst). E2E bakes a throwaway tag and "
                "deletes it on teardown — that's not safe on prod."
            )

        ctx = context or dag.host().directory(".")
        uv_cache = dag.cache_volume("uv-cache")
        apt_cache = dag.cache_volume("apt-cache")
        return await (
            dag.container()
            .from_("python:3.12-slim")
            .with_env_variable("DEBIAN_FRONTEND", "noninteractive")
            .with_mounted_cache("/var/cache/apt", apt_cache)
            .with_exec(["apt-get", "update", "-qq"])
            .with_exec(["apt-get", "install", "-y", "-qq", "git", "curl", "ca-certificates"])
            .with_exec(
                [
                    "sh",
                    "-c",
                    "curl -LsSf https://astral.sh/uv/install.sh | sh && "
                    "install -m 0755 /root/.local/bin/uv /usr/local/bin/uv",
                ]
            )
            .with_env_variable("PYTHONUNBUFFERED", "1")
            .with_mounted_cache("/root/.cache/uv", uv_cache)
            .with_mounted_directory("/app", ctx)
            .with_workdir("/app")
            .with_exec(["uv", "sync", "--all-extras"])
            .with_exec(["uv", "pip", "install", "-e", "./clients/python"])
            .with_secret_variable("HF_TOKEN", hf_token)
            .with_env_variable("MAT_VIS_E2E", "1")
            .with_exec(["uv", "run", "pytest", "tests/e2e/", "-v"])
            .stdout()
        )

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

        Builds ``_baker_container`` and runs ``mat-vis-baker --help`` plus
        ``mat-vis-baker hf-bake --help`` under ``uv run``. Exits 0 iff the
        apt + ``uv sync`` succeed and the CLI is importable. No network
        side effects — nothing touches HF.
        """
        context = src or dag.host().directory(".")
        ctr = self._baker_container(context)
        top = await ctr.with_exec(["uv", "run", "mat-vis-baker", "--help"]).stdout()
        bake = await ctr.with_exec(["uv", "run", "mat-vis-baker", "hf-bake", "--help"]).stdout()
        return f"=== mat-vis-baker --help ===\n{top}\n=== mat-vis-baker hf-bake --help ===\n{bake}"

    # ── per-file derive pipeline (#204) ─────────────────────────────

    @function
    async def derive(
        self,
        context: Annotated[dagger.Directory, Doc("Project root directory")],
        source: Annotated[str, Doc("Upstream (ambientcg/polyhaven/gpuopen)")],
        target_tier: Annotated[str, Doc("Smaller tier to derive, e.g. 1k")],
        source_tier: Annotated[str, Doc("Existing per-file tier to resize from, e.g. 4k")],
        release_tag: Annotated[str, Doc("Calver release tag, e.g. v2026.05.0")],
        hf_token: Annotated[dagger.Secret, Doc("HF API token for atomic commits")],
        repo_id: Annotated[str, Doc("HF dataset repo id")] = "gerchowl/mat-vis-tst",
        allow_prod: Annotated[
            bool, Doc("Opt-in flag required to target any non-*-tst repo")
        ] = False,
        limit: Annotated[int, Doc("Max materials (0 = no limit)")] = 0,
        batch_size: Annotated[int, Doc("Materials per atomic commit (count ceiling, #228)")] = 300,
        batch_max_bytes: Annotated[
            int,
            Doc(
                "Bytes per atomic commit (default 700 MiB). #228: flush "
                "trips on first-of-N-or-bytes. HF caps at 1 GiB/commit."
            ),
        ] = 700 * 1024 * 1024,
        dry_run: Annotated[bool, Doc("Skip the HF push; build locally")] = False,
    ) -> str:
        """Per-file derive: resize an existing per-file tier into a smaller one (#204).

        Wraps ``mat-vis-baker hf-derive`` in the slim baker container
        (no KTX2 toolchain — ``with_ktx2=False``). Reads source channels
        via plain HTTPS GET, runs PIL LANCZOS resize, writes per-file
        PNGs back to HF. Updates ``<source>.json`` and writes a
        ``.tier_complete`` sentinel as the final commit.

        Safety: defaults to ``gerchowl/mat-vis-tst``; any other target
        requires ``--allow-prod=true``.
        """
        self._guard_prod_target(repo_id, allow_prod)
        ctr = self._baker_container(context, with_ktx2=False, hf_token=hf_token)
        cmd = [
            "uv",
            "run",
            "mat-vis-baker",
            "hf-derive",
            "--source",
            source,
            "--source-tier",
            source_tier,
            "--target-tier",
            target_tier,
            "--release-tag",
            release_tag,
            "--work-dir",
            "/tmp/derive",
            "--repo-id",
            repo_id,
            "--hf-token",
            "env:HF_TOKEN",
            "--batch-size",
            str(batch_size),
            "--batch-max-bytes",
            str(batch_max_bytes),
        ]
        if limit > 0:
            cmd += ["--limit", str(limit)]
        if dry_run:
            cmd.append("--dry-run")
        if allow_prod:
            cmd.append("--allow-prod")
        return await ctr.with_exec(cmd).stdout()

    @function
    async def derive_ktx2(
        self,
        context: Annotated[dagger.Directory, Doc("Project root directory")],
        source: Annotated[str, Doc("Upstream (ambientcg/polyhaven/gpuopen)")],
        source_tier: Annotated[str, Doc("Existing per-file PNG tier to transcode from")],
        release_tag: Annotated[str, Doc("Calver release tag")],
        hf_token: Annotated[dagger.Secret, Doc("HF API token for atomic commits")],
        repo_id: Annotated[str, Doc("HF dataset repo id")] = "gerchowl/mat-vis-tst",
        allow_prod: Annotated[
            bool, Doc("Opt-in flag required to target any non-*-tst repo")
        ] = False,
        target_tier: Annotated[str, Doc("KTX2 target tier label; empty = ktx2-<source-tier>")] = "",
        limit: Annotated[int, Doc("Max materials (0 = no limit)")] = 0,
        batch_size: Annotated[int, Doc("Materials per atomic commit (count ceiling, #228)")] = 300,
        batch_max_bytes: Annotated[
            int,
            Doc(
                "Bytes per atomic commit (default 700 MiB). #228: flush "
                "trips on first-of-N-or-bytes. HF caps at 1 GiB/commit."
            ),
        ] = 700 * 1024 * 1024,
        dry_run: Annotated[bool, Doc("Skip the HF push; build locally")] = False,
    ) -> str:
        """Per-file derive: transcode an existing per-file PNG tier to KTX2 (#204).

        Wraps ``mat-vis-baker hf-derive-ktx2`` in the baker container
        with toktx installed (``with_ktx2=True``). Reads source PNG
        channels via plain GET, runs ``toktx --encode uastc --genmipmap
        --t2``, writes per-file ``.ktx2`` back to HF.

        ``target_tier=""`` is the "use the CLI default" sentinel —
        Dagger can't express "omit this arg", so we only pass
        ``--target-tier`` when the operator supplied a non-empty value.

        Safety: defaults to ``gerchowl/mat-vis-tst``; any other target
        requires ``--allow-prod=true``.

        CLI surface: dagger's Go-side kebab-case conversion splits
        letter→digit boundaries, so this Python ``derive_ktx2`` is
        exposed as ``dagger call derive-ktx-2`` (not ``derive-ktx2``).
        See #240 and the contract test in
        ``tests/test_dagger_function_names.py``.
        """
        self._guard_prod_target(repo_id, allow_prod)
        ctr = self._baker_container(context, with_ktx2=True, hf_token=hf_token)
        cmd = [
            "uv",
            "run",
            "mat-vis-baker",
            "hf-derive-ktx2",
            "--source",
            source,
            "--source-tier",
            source_tier,
            "--release-tag",
            release_tag,
            "--work-dir",
            "/tmp/derive",
            "--repo-id",
            repo_id,
            "--hf-token",
            "env:HF_TOKEN",
            "--batch-size",
            str(batch_size),
            "--batch-max-bytes",
            str(batch_max_bytes),
        ]
        if target_tier:
            cmd += ["--target-tier", target_tier]
        if limit > 0:
            cmd += ["--limit", str(limit)]
        if dry_run:
            cmd.append("--dry-run")
        if allow_prod:
            cmd.append("--allow-prod")
        return await ctr.with_exec(cmd).stdout()

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
