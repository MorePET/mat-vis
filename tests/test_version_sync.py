"""Guard SSoT: each client's manifest version must flow to every place that carries it.

- Python package: ``clients/python/pyproject.toml`` is the source. The HTTP
  User-Agent and ``__version__`` export derive via ``importlib.metadata``.
  The zero-install standalone mirror gets its literal synced by
  ``scripts/sync-standalone-version.py`` and checked here.
- JS client: ``clients/js/package.json`` is the source. The ``VERSION``
  constant in ``mat-vis-client.mjs`` is synced by
  ``scripts/sync-js-version.py`` and checked here.
- Rust client: ``clients/rust/Cargo.toml`` is the source. The
  User-Agent string uses ``concat!(..., env!("CARGO_PKG_VERSION"), ...)``
  at compile time, so no sync script is needed — verified by a smoke
  test that the literal ``env!("CARGO_PKG_VERSION")`` is present.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLIENT_PYPROJECT = REPO / "clients" / "python" / "pyproject.toml"
STANDALONE = REPO / "clients" / "python" / "mat_vis_client_standalone.py"
JS_PACKAGE_JSON = REPO / "clients" / "js" / "package.json"
JS_CLIENT_MJS = REPO / "clients" / "js" / "mat-vis-client.mjs"
RUST_CARGO_TOML = REPO / "clients" / "rust" / "Cargo.toml"
RUST_MAIN_RS = REPO / "clients" / "rust" / "src" / "main.rs"


def _client_version() -> str:
    with open(CLIENT_PYPROJECT, "rb") as f:
        return tomllib.load(f)["project"]["version"]


def test_client_pyproject_has_version():
    v = _client_version()
    assert re.match(r"^\d+\.\d+\.\d+", v), f"non-semver client version: {v!r}"


def test_standalone_version_matches_pyproject():
    want = _client_version()
    text = STANDALONE.read_text()
    m = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    assert m, "standalone missing __version__ literal — run scripts/sync-standalone-version.py"
    assert m.group(1) == want, (
        f"standalone __version__={m.group(1)!r} drifted from pyproject {want!r}. "
        "Run `python3 scripts/sync-standalone-version.py` to re-sync."
    )


def test_standalone_user_agent_matches_packaged():
    """Regression for issue #70: the standalone's User-Agent literal used
    to carry a ``-standalone`` suffix, giving servers two distinct UA
    populations for what is one client. Unified in 0.4.1; pin it here
    so the prefix can't silently diverge again — neither the AST drift
    test nor the runtime version check catches string-literal drift."""
    text = STANDALONE.read_text()
    m = re.search(r'^USER_AGENT = f"([^"]+)"$', text, re.MULTILINE)
    assert m, "standalone missing USER_AGENT fstring literal"
    # The literal contains {__version__}; what we check is the prefix.
    assert m.group(1).startswith("mat-vis-client/"), (
        f"standalone USER_AGENT={m.group(1)!r} must start with 'mat-vis-client/' "
        "to match the packaged client (see issue #70)."
    )
    assert "standalone" not in m.group(1), (
        f"standalone USER_AGENT={m.group(1)!r} contains 'standalone' — "
        "server-side observability should see a single UA population."
    )


def test_js_client_version_matches_package_json():
    want = json.loads(JS_PACKAGE_JSON.read_text())["version"]
    m = re.search(r"^export const VERSION = '([^']+)';$", JS_CLIENT_MJS.read_text(), re.MULTILINE)
    assert m, "JS client missing VERSION literal — run scripts/sync-js-version.py"
    assert m.group(1) == want, (
        f"JS client VERSION={m.group(1)!r} drifted from package.json {want!r}. "
        "Run `python3 scripts/sync-js-version.py` to re-sync."
    )


def test_baker_runtime_version_matches_pyproject():
    """Item C regression: BAKER_VERSION must come from installed package
    metadata — no more hardcoded ``0.1.0`` literal diverging from the
    baker's pyproject version."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as pkg_version

    from mat_vis_baker.common import BAKER_VERSION, USER_AGENT

    # Read authoritative baker pyproject version
    with open(REPO / "pyproject.toml", "rb") as f:
        want = tomllib.load(f)["project"]["version"]

    try:
        installed = pkg_version("mat-vis")
    except PackageNotFoundError:
        installed = None

    # If the baker is installed (editable or wheel), BAKER_VERSION must
    # equal the installed metadata version, which must equal pyproject.
    if installed is not None:
        assert BAKER_VERSION == installed == want, (
            f"baker version drift: BAKER_VERSION={BAKER_VERSION!r} "
            f"installed={installed!r} pyproject={want!r}"
        )
    # And User-Agent must carry it verbatim.
    assert USER_AGENT == f"mat-vis-baker/{BAKER_VERSION}"


def test_client_runtime_version_matches_pyproject():
    """Items B + L regression: USER_AGENT + __version__ must come from
    installed metadata, not a hand-maintained literal."""
    from mat_vis_client import __version__ as pkg_level
    from mat_vis_client.client import USER_AGENT
    from mat_vis_client.client import __version__ as mod_level

    want = _client_version()
    assert pkg_level == mod_level == want, (
        f"client version drift: pkg={pkg_level!r} mod={mod_level!r} pyproject={want!r}"
    )
    assert USER_AGENT == f"mat-vis-client/{want} (Python)"


def test_rust_client_uses_compile_time_version_macro():
    """Rust SSoT: the User-Agent must come from CARGO_PKG_VERSION at compile
    time, not a hand-maintained literal."""
    text = RUST_MAIN_RS.read_text()
    assert 'env!("CARGO_PKG_VERSION")' in text, (
        "Rust client dropped the CARGO_PKG_VERSION macro — version would "
        'drift from Cargo.toml. Restore `concat!(..., env!("CARGO_PKG_VERSION"), ...)`.'
    )
    # And make sure Cargo.toml actually has a version to source from.
    with open(RUST_CARGO_TOML, "rb") as f:
        assert tomllib.load(f)["package"]["version"]
