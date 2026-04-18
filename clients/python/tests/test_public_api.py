"""Public surface tests (#84, #85).

Covers promotion of ``_get_client`` to ``get_client`` while keeping the
private name around for one release as a deprecated alias.
"""

from __future__ import annotations

import warnings


def test_get_client_is_public():
    """``from mat_vis_client import get_client`` must work."""
    from mat_vis_client import get_client, MatVisClient

    c = get_client()
    assert isinstance(c, MatVisClient)


def test_get_client_returns_singleton():
    """Repeated calls return the same instance (process-wide cache share)."""
    from mat_vis_client import get_client

    assert get_client() is get_client()


def test_get_client_in_public_all():
    """__all__ advertises get_client — not just the private alias."""
    import mat_vis_client

    assert "get_client" in mat_vis_client.__all__


def test_private_get_client_still_works():
    """Back-compat: the old ``_get_client`` import path still returns a client."""
    from mat_vis_client import _get_client, MatVisClient

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        c = _get_client()
    assert isinstance(c, MatVisClient)


def test_private_get_client_warns():
    """Using ``_get_client`` emits DeprecationWarning pointing at get_client."""
    from mat_vis_client import _get_client

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _get_client()
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert deprecations, "expected DeprecationWarning on _get_client()"
    assert "get_client" in str(deprecations[0].message)


def test_private_and_public_return_same_singleton():
    from mat_vis_client import _get_client, get_client

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert _get_client() is get_client()
