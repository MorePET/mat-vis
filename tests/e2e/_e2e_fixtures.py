"""Preserved E2E fixture set on Hugging Face (mat-vis#358 follow-up).

The cache observability + ETag tests need a stable, deterministic
substrate to assert against. Pinning to ``gerchowl/mat-vis@v2026.04.X``
makes the tests fragile (substrate evolves; tests break when the
fixture material gets re-baked or pruned upstream). Solution: a
dedicated, frozen tag on ``gerchowl/mat-vis-tst`` containing a tiny
deterministic subset.

This module declares the fixture set + a preflight check. Tests in
``test_cache_lifecycle_e2e.py`` use the helpers here; the rebake
script ``scripts/rebake_e2e_fixtures.py`` produces the substrate.

Why a separate tag from prod / dev tst:

- **Stable across substrate evolution**: when v2026.05/06/... cuts
  ship, the E2E tag stays unchanged. Tests don't break on every
  data-side rebake.
- **Tiny payload**: 2 materials × ~5 channels × 1k = ~10 MB total.
  Fast to fetch + cheap on HF bandwidth budget.
- **Self-recovering**: preflight check + ``rebake_e2e_fixtures.py``
  means a missing fixture is recoverable in a single dispatch, not a
  cross-team coordination cost.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

# Pinned to gerchowl/mat-vis-tst; the tag is deliberately reserved
# (never used for normal cuts) so it's always exactly this fixture
# set unless someone explicitly re-bakes it.
E2E_FIXTURES_REPO = "gerchowl/mat-vis-tst"
E2E_FIXTURES_TAG = "v0.0.0-e2e-fixtures"

# Curated fixture: deterministic ids that have stayed stable across
# every ambientcg substrate cut so far (acoustic-foam materials —
# they predate every other in the catalog and ambientCG hasn't
# renamed them since 2018). Two materials × small tier keep total
# bytes under 5 MB.
E2E_FIXTURE_SOURCE = "ambientcg"
E2E_FIXTURE_TIER = "1k"
E2E_FIXTURE_MATERIALS = ("AcousticFoam001", "AcousticFoam002")

# Channels we expect every fixture material to ship — the manifest
# claims these; the preflight check asserts each lands on HF.
E2E_FIXTURE_CHANNELS = ("color", "normal", "roughness")

E2E_FIXTURES_BASE = (
    f"https://huggingface.co/datasets/{E2E_FIXTURES_REPO}/resolve/{E2E_FIXTURES_TAG}"
)


def fixture_urls() -> list[tuple[str, str]]:
    """Return ``(label, url)`` pairs for every fixture artifact the
    preflight check verifies. Stable order so failure messages
    point at the same artifact name across runs.
    """
    out: list[tuple[str, str]] = [
        ("manifest", f"{E2E_FIXTURES_BASE}/release-manifest.json"),
        ("catalog", f"{E2E_FIXTURES_BASE}/{E2E_FIXTURE_SOURCE}.json"),
    ]
    for mat in E2E_FIXTURE_MATERIALS:
        for ch in E2E_FIXTURE_CHANNELS:
            url = f"{E2E_FIXTURES_BASE}/{E2E_FIXTURE_SOURCE}/{E2E_FIXTURE_TIER}/{mat}/{ch}.png"
            out.append((f"{mat}/{ch}.png", url))
    return out


def preflight_fixtures_present() -> tuple[bool, list[str]]:
    """HEAD-check every fixture URL. Returns ``(all_present, missing)``.

    Used by tests to skip-with-warning rather than error-out when
    the fixture set isn't on HF — operator runs
    ``scripts/rebake_e2e_fixtures.py`` to restore.
    """
    missing: list[str] = []
    for label, url in fixture_urls():
        req = urllib.request.Request(url, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=15):
                continue
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                missing.append(label)
                continue
            # Non-404 (5xx, auth) is a transient infra problem, not a
            # missing fixture. Treat as "present" so tests don't get
            # falsely re-baked.
        except (urllib.error.URLError, TimeoutError):
            # Network blip — same handling as 5xx.
            pass
    return (not missing, missing)


def fixture_skip_reason() -> str | None:
    """Return a skip reason string if E2E shouldn't run, else None."""
    if os.environ.get("MAT_VIS_E2E") != "1":
        return "set MAT_VIS_E2E=1 to run E2E cache-lifecycle tests"
    ok, missing = preflight_fixtures_present()
    if not ok:
        return (
            f"E2E fixtures missing on {E2E_FIXTURES_REPO}@{E2E_FIXTURES_TAG}: "
            f"{', '.join(missing[:3])}"
            f"{'...' if len(missing) > 3 else ''}. "
            "Run `python scripts/rebake_e2e_fixtures.py` to restore."
        )
    return None
