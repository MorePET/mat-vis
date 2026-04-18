"""MaterialX API cleanup (#85 items 6 + 7).

- Single path: ``client.mtlx(src, id, tier)`` returns an MtlxSource with
  ``.xml()``, ``.export(path)``, and ``.original()``.
- Deprecated shims are removed: ``to_mtlx``, ``fetch_mtlx_original``,
  ``materialize_mtlx`` no longer exist on MatVisClient.
- ``.xml`` and ``.original`` are network-triggering methods, not
  attribute-access properties (the latter surprises non-Python readers).
"""

from __future__ import annotations

from unittest.mock import patch


from mat_vis_client import MatVisClient, MtlxSource


def test_mtlx_returns_mtlx_source():
    c = MatVisClient()
    m = c.mtlx("ambientcg", "Rock064", "1k")
    assert isinstance(m, MtlxSource)


# ── Deprecated shims removed ───────────────────────────────────


def test_to_mtlx_removed():
    """client.to_mtlx no longer exists (use client.mtlx(...).export(dir))."""
    c = MatVisClient()
    assert not hasattr(c, "to_mtlx")


def test_fetch_mtlx_original_removed():
    """client.fetch_mtlx_original no longer exists (use .mtlx(...).original())."""
    c = MatVisClient()
    assert not hasattr(c, "fetch_mtlx_original")


def test_materialize_mtlx_removed():
    c = MatVisClient()
    assert not hasattr(c, "materialize_mtlx")


# ── xml is a method, not a property ────────────────────────────


def test_xml_is_callable_method():
    """MtlxSource.xml must be a method — attribute access shouldn't
    trigger IO (footgun; doesn't translate to Rust/Go/JS)."""
    c = MatVisClient()
    m = c.mtlx("ambientcg", "Rock064", "1k")
    # Attribute access should yield the method, NOT raise / fetch.
    assert callable(m.xml), "MtlxSource.xml must be a method"


def test_xml_method_returns_string():
    """Calling .xml() returns the MaterialX document string."""
    c = MatVisClient()
    m = c.mtlx("ambientcg", "Rock064", "1k")
    with (
        patch.object(c, "channels", return_value=["color", "normal"]),
        patch.object(c, "_scalars_for", return_value={"roughness": 0.5}),
    ):
        result = m.xml()
    assert isinstance(result, str)
    assert "materialx" in result


# ── original is a method, not a property ───────────────────────


def test_original_is_callable_method():
    c = MatVisClient()
    m = c.mtlx("ambientcg", "Rock064", "1k")
    assert callable(m.original), "MtlxSource.original must be a method"


def test_original_returns_none_when_no_upstream():
    c = MatVisClient()
    m = c.mtlx("ambientcg", "Rock064", "1k")
    with patch.object(c, "_fetch_mtlx_original_map", return_value={}):
        assert m.original() is None


def test_original_returns_alternate_mtlx_source():
    c = MatVisClient()
    m = c.mtlx("gpuopen", "Metal032", "1k")
    with patch.object(c, "_fetch_mtlx_original_map", return_value={"Metal032": "<mtlx/>"}):
        alt = m.original()
    assert isinstance(alt, MtlxSource)
    assert alt.is_original is True


def test_original_on_original_returns_none():
    """Calling .original() on an already-original source yields None."""
    c = MatVisClient()
    orig = MtlxSource(c, "gpuopen", "Metal032", "1k", is_original=True)
    assert orig.original() is None
