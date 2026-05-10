"""Layered repo + tag resolution for ``MatVisClient`` (mat-vis#384).

Covers the precedence chain on ``_resolve_repo_and_tag`` plus the
end-to-end client-construction wiring (URL composition + cache
namespacing). Pure unit tests — no network.

Precedence (high → low):

  1. Constructor kwargs ``MatVisClient(repo=..., tag=...)``.
  2. ``MAT_VIS_DATASET=<repo>@<tag>`` (combined env var).
  3. ``MAT_VIS_HF_DATASET=<repo>`` + ``MAT_VIS_TAG=<tag>``.
  4. ``MAT_VIS_HF_BASE=<full-url>`` (legacy).
  5. Default ``gerchowl/mat-vis`` @ ``DEFAULT_TAG``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from mat_vis_client import MatVisClient
from mat_vis_client.client import (
    DEFAULT_TAG,
    HF_DATASET,
    _resolve_repo_and_tag,
)

# Env vars the resolver consults — wiped per-test by the fixture below.
_RESOLVER_ENV_VARS = (
    "MAT_VIS_DATASET",
    "MAT_VIS_HF_DATASET",
    "MAT_VIS_TAG",
    "MAT_VIS_HF_BASE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip resolver-relevant env vars so each test starts from a
    known baseline. Tests that want a var set call ``monkeypatch.setenv``
    after this fixture has run."""
    for name in _RESOLVER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ── _resolve_repo_and_tag table-driven precedence tests ───────────


class TestResolverPrecedence:
    def test_layer1_kwargs_win_over_everything(self, monkeypatch):
        # Set every env var to a poison value — kwargs must still win.
        monkeypatch.setenv("MAT_VIS_DATASET", "poison/dataset@vBAD")
        monkeypatch.setenv("MAT_VIS_HF_DATASET", "poison/hfds")
        monkeypatch.setenv("MAT_VIS_TAG", "vBAD")
        monkeypatch.setenv("MAT_VIS_HF_BASE", "https://poison.test/d/x/y/resolve")
        repo, tag, base = _resolve_repo_and_tag(repo_kwarg="org/wins", tag_kwarg="vWIN")
        assert (repo, tag, base) == ("org/wins", "vWIN", None)

    def test_layer1_partial_kwargs_repo_only_uses_env_tag(self, monkeypatch):
        """``repo=`` kwarg + no ``tag=`` kwarg: tag falls through to env."""
        monkeypatch.setenv("MAT_VIS_TAG", "vENV")
        repo, tag, base = _resolve_repo_and_tag(repo_kwarg="org/explicit", tag_kwarg=None)
        assert (repo, tag, base) == ("org/explicit", "vENV", None)

    def test_layer2_combined_env_var(self):
        import os

        os.environ["MAT_VIS_DATASET"] = "gerchowl/mat-vis-tst@v2026.04.99-tst-full-369"
        try:
            repo, tag, base = _resolve_repo_and_tag(repo_kwarg=None, tag_kwarg=None)
        finally:
            del os.environ["MAT_VIS_DATASET"]
        assert repo == "gerchowl/mat-vis-tst"
        assert tag == "v2026.04.99-tst-full-369"
        assert base is None

    def test_layer2_combined_env_var_overrides_split_form(self, monkeypatch):
        """When ``MAT_VIS_DATASET`` is set, ``MAT_VIS_HF_DATASET`` and
        ``MAT_VIS_TAG`` from the split form must be ignored — the
        combined form is the more-specific layer."""
        monkeypatch.setenv("MAT_VIS_DATASET", "winner/repo@vWIN")
        monkeypatch.setenv("MAT_VIS_HF_DATASET", "loser/repo")
        monkeypatch.setenv("MAT_VIS_TAG", "vLOSE")
        repo, tag, _ = _resolve_repo_and_tag(repo_kwarg=None, tag_kwarg=None)
        assert (repo, tag) == ("winner/repo", "vWIN")

    def test_layer3_split_env_vars(self, monkeypatch):
        monkeypatch.setenv("MAT_VIS_HF_DATASET", "gerchowl/mat-vis-tst")
        monkeypatch.setenv("MAT_VIS_TAG", "v2026.04.99")
        repo, tag, base = _resolve_repo_and_tag(repo_kwarg=None, tag_kwarg=None)
        assert (repo, tag, base) == ("gerchowl/mat-vis-tst", "v2026.04.99", None)

    def test_layer3_dataset_env_with_default_tag(self, monkeypatch):
        """``MAT_VIS_HF_DATASET`` alone → repo overridden, tag falls
        back to ``DEFAULT_TAG``."""
        monkeypatch.setenv("MAT_VIS_HF_DATASET", "gerchowl/mat-vis-tst")
        repo, tag, base = _resolve_repo_and_tag(repo_kwarg=None, tag_kwarg=None)
        assert (repo, tag, base) == ("gerchowl/mat-vis-tst", DEFAULT_TAG, None)

    def test_layer4_legacy_hf_base_only(self, monkeypatch):
        """``MAT_VIS_HF_BASE`` alone → repo parsed from URL,
        ``base_override`` returned for verbatim URL composition."""
        monkeypatch.setenv(
            "MAT_VIS_HF_BASE",
            "https://huggingface.co/datasets/gerchowl/mat-vis-tst/resolve",
        )
        repo, tag, base = _resolve_repo_and_tag(repo_kwarg=None, tag_kwarg=None)
        assert repo == "gerchowl/mat-vis-tst"
        assert tag == DEFAULT_TAG
        assert base == "https://huggingface.co/datasets/gerchowl/mat-vis-tst/resolve"

    def test_layer4_legacy_hf_base_non_hf_url(self, monkeypatch):
        """Private-mirror URL that doesn't match the HF dataset
        pattern: regex misses, repo defaults, base preserved verbatim."""
        monkeypatch.setenv(
            "MAT_VIS_HF_BASE",
            "https://internal.mirror.example/private/textures",
        )
        repo, tag, base = _resolve_repo_and_tag(repo_kwarg=None, tag_kwarg=None)
        # Repo couldn't be parsed → falls back to default.
        assert repo == HF_DATASET
        assert tag == DEFAULT_TAG
        # Base preserved verbatim so URL composition still works.
        assert base == "https://internal.mirror.example/private/textures"

    def test_layer5_pure_default(self):
        """No env, no kwargs → production defaults."""
        repo, tag, base = _resolve_repo_and_tag(repo_kwarg=None, tag_kwarg=None)
        assert repo == HF_DATASET
        assert tag == DEFAULT_TAG
        assert base is None

    def test_higher_layer_wins_when_both_set(self, monkeypatch):
        """When both combined-env (layer 2) AND legacy HF_BASE (layer 4)
        are set, the combined-env value wins; HF_BASE is ignored."""
        monkeypatch.setenv("MAT_VIS_DATASET", "winner/repo@vWIN")
        monkeypatch.setenv(
            "MAT_VIS_HF_BASE",
            "https://huggingface.co/datasets/loser/repo/resolve",
        )
        repo, tag, base = _resolve_repo_and_tag(repo_kwarg=None, tag_kwarg=None)
        assert (repo, tag, base) == ("winner/repo", "vWIN", None)

    def test_combined_env_with_at_in_tag(self, monkeypatch):
        """Defensive: ``rsplit("@", 1)`` so values containing ``@``
        round-trip cleanly when only the FINAL ``@`` is the separator.
        """
        monkeypatch.setenv("MAT_VIS_DATASET", "org/repo@feature@v1")
        repo, tag, _ = _resolve_repo_and_tag(repo_kwarg=None, tag_kwarg=None)
        assert (repo, tag) == ("org/repo@feature", "v1")


# ── End-to-end client wiring (URL composition + cache scope) ──────


class TestClientWithRepoKwarg:
    def test_repo_kwarg_routes_url_composition(self):
        """``MatVisClient(repo=..., tag=...)`` composes manifest_url + hf_url
        against the chosen dataset, not the prod default."""
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(
                repo="gerchowl/mat-vis-tst",
                tag="v2026.04.99-tst-full-369",
                cache_dir=Path(tmp),
            )
            assert (
                client._manifest_url == "https://huggingface.co/datasets/gerchowl/mat-vis-tst"
                "/resolve/v2026.04.99-tst-full-369/release-manifest.json"
            )
            assert client._hf_url("ambientcg.json") == (
                "https://huggingface.co/datasets/gerchowl/mat-vis-tst"
                "/resolve/v2026.04.99-tst-full-369/ambientcg.json"
            )

    def test_repo_kwarg_namespaces_cache(self):
        """Two clients pointed at the SAME tag but DIFFERENT repos
        must not collide on disk — pre-#384 they shared a scope."""
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            c_prod = MatVisClient(repo="gerchowl/mat-vis", tag="v2026.04.2", cache_dir=cache_dir)
            c_tst = MatVisClient(repo="gerchowl/mat-vis-tst", tag="v2026.04.2", cache_dir=cache_dir)
            assert c_prod._cache_scope != c_tst._cache_scope, (
                "cache scopes collided — one repo would serve the other's bytes"
            )
            # Sanity: both share the version segment (parent of the
            # repo segment), only the repo segment differs.
            assert c_prod._cache_scope.parent.parent == c_tst._cache_scope.parent.parent

    def test_combined_env_var_drives_construction(self, monkeypatch):
        """No kwargs, only ``MAT_VIS_DATASET`` env: client routes there."""
        monkeypatch.setenv("MAT_VIS_DATASET", "gerchowl/mat-vis-tst@v2026.04.99")
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(cache_dir=Path(tmp))
            assert client._repo == "gerchowl/mat-vis-tst"
            assert client._tag == "v2026.04.99"
            assert "gerchowl/mat-vis-tst" in client._manifest_url
            assert "v2026.04.99" in client._manifest_url

    def test_at_forwards_repo(self):
        """``client.at("v...")`` keeps routing to the same repo so
        per-tag scopes under env-driven overrides don't silently drift
        back to prod."""
        with tempfile.TemporaryDirectory() as tmp:
            parent = MatVisClient(
                repo="gerchowl/mat-vis-tst",
                tag="v2026.04.99",
                cache_dir=Path(tmp),
            )
            child = parent.at("v2026.04.50")
            assert child._repo == "gerchowl/mat-vis-tst"
            assert child._tag == "v2026.04.50"
            assert "gerchowl/mat-vis-tst" in child._hf_url("x.json")

    def test_legacy_hf_base_url_preserved_verbatim(self, monkeypatch):
        """Private-mirror URL shape (non-HF) stays intact end-to-end —
        important for back-compat with existing CI / tests / mirrors."""
        monkeypatch.setenv("MAT_VIS_HF_BASE", "https://mirror.example/textures")
        with tempfile.TemporaryDirectory() as tmp:
            client = MatVisClient(tag="v2026.04.0", cache_dir=Path(tmp))
            assert client._base == "https://mirror.example/textures"
            assert client._manifest_url.startswith("https://mirror.example/textures/v2026.04.0/")
