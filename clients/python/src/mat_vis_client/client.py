#!/usr/bin/env python3
"""mat-vis reference client — pure Python, zero dependencies.

Fetches PBR textures from mat-vis GitHub Releases via HTTP range reads.
Uses only urllib (stdlib). No pyarrow, no binary deps.

Usage as library:
    from mat_vis_client import MatVisClient
    client = MatVisClient()
    png_bytes = client.fetch_texture("ambientcg", "Rock064", "color", tier="1k")

    # Search by category and scalar ranges
    results = client.search("metal", roughness_range=(0.2, 0.6))

    # Bulk prefetch all materials for offline use
    client.prefetch("ambientcg", tier="1k")

Usage as CLI:
    python mat_vis_client.py list                              # list sources × tiers
    python mat_vis_client.py materials ambientcg 1k            # list materials
    python mat_vis_client.py fetch ambientcg Rock064 color 1k  # fetch PNG → stdout
    python mat_vis_client.py fetch ambientcg Rock064 color 1k -o rock.png
    python mat_vis_client.py search metal --roughness 0.2:0.6  # search materials
    python mat_vis_client.py prefetch ambientcg 1k             # bulk download
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import sys
import time
import urllib.request
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Literal

REPO = "MorePET/mat-vis"
GITHUB_API = f"https://api.github.com/repos/{REPO}"  # update-check only
PYPI_API = "https://pypi.org/pypi/mat-vis-client/json"

# v0.6.0 (ADR-0007): HF Datasets is the canonical substrate. URLs are
# built as ``{HF_BASE}/<tag>/<path>``. There is no "latest" alias on HF
# — callers must pin a revision (tag or branch). `MAT_VIS_HF_BASE`
# overrides the default for tests / private mirrors.
HF_DATASET = "gerchowl/mat-vis"
HF_BASE = os.environ.get(
    "MAT_VIS_HF_BASE",
    f"https://huggingface.co/datasets/{HF_DATASET}/resolve",
)
# Default tag when the caller doesn't pin one (#242). The dataset's
# `main` branch is an empty baseline — every release lives on a
# CalVer branch — so a `tag=None` client must default to a real
# release. Bump this when a new prod release ships and is verified
# under the per-file substrate (#186 / ADR-0012). The explicit
# ``tag=...`` override still wins for callers that need it.
DEFAULT_TAG = "v2026.04.2"
DEFAULT_CACHE_DIR = Path(os.environ.get("MAT_VIS_CACHE", Path.home() / ".cache" / "mat-vis"))

# SSoT for version: clients/python/pyproject.toml. Derived at runtime so
# every User-Agent, __version__ export, and update-check comparison
# agrees with the installed wheel's actual version — no manual bumps
# scattered across the codebase.
try:
    __version__ = _pkg_version("mat-vis-client")
except PackageNotFoundError:
    __version__ = "0.0.0+dev"
USER_AGENT = f"mat-vis-client/{__version__} (Python)"

# Module-local logger. Library consumers configure their own handlers;
# notices emitted via ``log.info(...)`` are silent by default (root
# logger at WARNING), which is the behavior we want for a library.
log = logging.getLogger("mat-vis-client")


def _env_flag(name: str) -> bool:
    """Return True if the named env var is set to a truthy value."""
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


# Update check: cache TTL (24h) + opt-out / force env vars.
# Precedence: MAT_VIS_NO_UPDATE_CHECK (opt-out) wins over
# MAT_VIS_UPDATE_CHECK (force-on). Default behavior is "only warn in
# interactive terminals (TTY stderr)" — see ``_should_check_updates``.
UPDATE_CHECK_TTL_SECONDS = 24 * 3600
UPDATE_CHECK_DISABLED = _env_flag("MAT_VIS_NO_UPDATE_CHECK")
UPDATE_CHECK_FORCED = _env_flag("MAT_VIS_UPDATE_CHECK")


def _parse_size(s: str | int) -> int:
    """Parse '5GB', '500MB', '0' etc. to bytes. 0 disables size checks."""
    if isinstance(s, int):
        return s
    s = str(s).strip().upper()
    if s in ("0", ""):
        return 0
    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    for unit in ("TB", "GB", "MB", "KB", "B"):
        if s.endswith(unit):
            num = s[: -len(unit)].strip()
            try:
                return int(float(num) * units[unit])
            except ValueError:
                break
    try:
        return int(s)
    except ValueError as e:
        raise ValueError(f"Cannot parse size: {s!r} (use e.g. '5GB', '500MB')") from e


def _fmt_size(n: int) -> str:
    """Format bytes to human-readable."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


# Default soft cap: 5 GB (configurable via MAT_VIS_CACHE_MAX_SIZE).
DEFAULT_CACHE_MAX_BYTES = _parse_size(os.environ.get("MAT_VIS_CACHE_MAX_SIZE", "5GB"))

# Previously a hardcoded frozenset of 10 names. The client doesn't need a
# static enum — categories are discoverable at runtime from rowmap filenames
# in the release manifest. See MatVisClient.categories(). Kept as a
# module-level alias for 1.x back-compat and lazy-loaded from the first
# client instance that calls .categories(). Callers doing strict validation
# should prefer `client.categories()` over this constant.
CATEGORIES: frozenset[str] = frozenset()  # populated lazily, see client.categories()


# Rate limit / retry knobs (env-configurable).
MAX_RETRIES = int(os.environ.get("MAT_VIS_MAX_RETRIES", "5"))
BACKOFF_BASE_SECONDS = float(os.environ.get("MAT_VIS_BACKOFF_BASE", "1.0"))
RETRY_MAX_WAIT_SECONDS = int(os.environ.get("MAT_VIS_RETRY_MAX_WAIT", "60"))


class MatVisError(Exception):
    """Base class for mat-vis-client errors.

    Every exception surfaced to callers is a ``MatVisError`` subclass —
    raw ``urllib.error.HTTPError`` / ``URLError`` never leaks out.
    """


class NotFoundError(MatVisError):
    """A key was not found in a mat-vis registry (material / channel / etc).

    Structured fields let callers branch without string-matching messages:

    - ``key``: the missing name (e.g. ``"Rock999"``)
    - ``available``: sorted list of valid names at this level
    - ``context``: optional path qualifier (e.g. ``"ambientcg/1k"``)
    - ``kind``: class-level label ("material", "source", ...)
    """

    kind: str = "item"

    def __init__(
        self,
        key: str,
        available: list[str] | None = None,
        context: str = "",
    ) -> None:
        self.key = key
        self.available = list(available or [])
        self.context = context
        where = f" in {context}" if context else ""
        hint = f". Available: {self.available}" if self.available else ""
        super().__init__(f"{self.kind} {key!r} not found{where}{hint}")


class MaterialNotFoundError(NotFoundError):
    kind = "material"


class UnknownMaterialError(MaterialNotFoundError):
    """``material_id`` is not present in ``client.index(source)``.

    Distinct from :class:`MaterialNotStagedError`: the name/id is wrong or the
    material was never mirrored, not just unbaked. Subclasses
    :class:`MaterialNotFoundError` so existing ``except MaterialNotFoundError``
    guards still fire.
    """


class MaterialNotStagedError(MatVisError):
    """The material exists in the source's index but no release asset was baked.

    Typical when a new index entry has landed upstream but the ``bake`` / ``derive``
    pipeline hasn't re-run yet. Caller should wait for or request a re-bake rather
    than treating this as a lookup failure.
    """

    def __init__(
        self,
        source: str,
        material_id: str,
        tier: str,
        *,
        original_name: str | None = None,
    ) -> None:
        self.source = source
        self.material_id = material_id
        self.tier = tier
        # ``original_name`` is what the caller typed *before* name→id
        # resolution (#280). When the user passed a UUID directly we
        # leave this ``None`` and emit the legacy single-id message;
        # when they passed a human name we surface both so a batch log
        # tells you *which* item in the run broke without grepping.
        self.original_name = original_name
        if original_name is not None and original_name != material_id:
            msg = (
                f"material {original_name!r} (resolved id {material_id!r}) "
                f"exists in {source!r} index but is not staged for "
                f"tier {tier!r}. Needs a re-bake."
            )
        else:
            msg = (
                f"material {material_id!r} exists in {source!r} index "
                f"but is not staged for tier {tier!r}. Needs a re-bake."
            )
        super().__init__(msg)


class AmbiguousMaterialError(MatVisError):
    """A human-readable name resolves to more than one index entry in ``source``.

    Raised by the name-resolution path in :meth:`fetch_all_textures` /
    :meth:`fetch_texture`; pass the canonical ``id`` instead to disambiguate.
    """

    def __init__(self, source: str, name: str, candidates: list[str]) -> None:
        self.source = source
        self.name = name
        self.candidates = sorted(candidates)
        bullets = "\n".join(f"  - {c}" for c in self.candidates)
        super().__init__(
            f"name {name!r} matches {len(self.candidates)} materials "
            f"in source {source!r}:\n{bullets}\nPass the id directly to disambiguate."
        )


class SourceNotFoundError(NotFoundError):
    kind = "source"


class TierNotFoundError(NotFoundError):
    kind = "tier"


class ChannelNotFoundError(NotFoundError):
    kind = "channel"


# Dispatch table: _lookup() picks the right typed subclass from ``kind``.
# Unknown kinds fall back to MatVisError (legacy call sites still work).
_NOT_FOUND_BY_KIND: dict[str, type[NotFoundError]] = {
    "material": MaterialNotFoundError,
    "source": SourceNotFoundError,
    "tier": TierNotFoundError,
    "channel": ChannelNotFoundError,
}


def _lookup(mapping: dict, key: str, *, kind: str, context: str = "") -> object:
    """Dict lookup that raises the typed ``NotFoundError`` subclass for
    ``kind`` (with an ``available=[...]`` hint) instead of ``KeyError``.

    Example: ``_lookup(materials, "Rock999", kind="material", context="ambientcg/1k")``
    raises :class:`MaterialNotFoundError` carrying ``.key`` / ``.available`` /
    ``.context``.
    """
    if key in mapping:
        return mapping[key]
    available = sorted(mapping.keys())
    cls = _NOT_FOUND_BY_KIND.get(kind)
    if cls is not None:
        raise cls(key=key, available=available, context=context)
    # Unknown kind — preserve legacy MatVisError for back-compat.
    where = f" in {context}" if context else ""
    raise MatVisError(f"{kind} {key!r} not found{where}. Available: {available}")


class HTTPFetchError(MatVisError):
    """HTTP fetch failed with a non-rate-limit error (404, 500, ...).

    Wraps ``urllib.error.HTTPError`` so callers never see raw urllib
    exceptions. Carries ``.url``, ``.code``, ``.reason``.
    """

    def __init__(self, url: str, code: int, reason: str = ""):
        self.url = url
        self.code = code
        self.reason = reason
        super().__init__(f"HTTP {code} for {url}{': ' + reason if reason else ''}")


class NetworkError(MatVisError):
    """Network-level failure (DNS / connection / timeout) after retries.

    Wraps ``urllib.error.URLError``. Carries ``.url`` and ``.reason``.
    """

    def __init__(self, url: str, reason: str):
        self.url = url
        self.reason = reason
        super().__init__(f"Network error for {url}: {reason}")


class RateLimitError(MatVisError):
    """GitHub rate limit hit. Carries retry_after in seconds."""

    def __init__(self, url: str, retry_after: int, message: str = ""):
        self.url = url
        self.retry_after = retry_after
        super().__init__(message or f"Rate limited on {url}. Retry after {retry_after}s.")


def _parse_retry_after(headers, default: int) -> int:
    """Extract retry delay from Retry-After / X-RateLimit-Reset headers."""
    retry_after = headers.get("Retry-After") if headers else None
    if retry_after:
        try:
            return min(int(retry_after), RETRY_MAX_WAIT_SECONDS)
        except (ValueError, TypeError):
            pass
    reset = headers.get("X-RateLimit-Reset") if headers else None
    if reset:
        try:
            wait = int(reset) - int(time.time())
            if 0 < wait <= RETRY_MAX_WAIT_SECONDS:
                return wait
        except (ValueError, TypeError):
            pass
    return min(default, RETRY_MAX_WAIT_SECONDS)


def _is_rate_limited(err: urllib.error.HTTPError) -> bool:
    """True if this HTTPError is transient and worth retrying.

    Covers: 429 (rate limit), 502/503/504 (proxy/service transient),
    403 with rate-limit headers or body. Non-transient 4xx/5xx (400, 401,
    404, 500, ...) return False so they propagate immediately.
    """
    if err.code in (429, 502, 503, 504):
        return True
    if err.code == 403:
        remaining = err.headers.get("X-RateLimit-Remaining") if err.headers else None
        if remaining == "0":
            return True
        body_start = ""
        try:
            body_start = str(err.read()[:200]).lower()
        except Exception:
            pass
        if "rate limit" in body_start or "api rate limit" in body_start:
            return True
    return False


def _get(
    url: str,
    headers: dict | None = None,
    return_final_url: bool = False,
) -> bytes | tuple[bytes, str]:
    """HTTP GET with User-Agent, automatic retry on rate limits / transient errors.

    Retries up to MAX_RETRIES on 429 / 503 / rate-limited 403, respecting
    Retry-After and X-RateLimit-Reset headers. Exponential backoff when the
    server doesn't specify. Emits one-line stderr notice per retry so the
    user sees what's happening. Raises ``RateLimitError`` after exhaustion.

    Non-rate-limit HTTP errors (404, 500, etc.) pass through unchanged.

    With ``return_final_url=True``, returns ``(bytes, resolved_url)`` — the
    URL after urllib followed any redirects. Useful for caching the
    resolved CDN URL of a GitHub Release asset (avoids repeated redirect
    hits on the rate-limited github.com side).
    """
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)

    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                if return_final_url:
                    return data, resp.url
                return data
        except urllib.error.HTTPError as e:
            last_err = e
            if not _is_rate_limited(e):
                # Non-transient HTTP error — wrap and surface immediately.
                raise HTTPFetchError(url, e.code, e.reason or "") from e
            if attempt >= MAX_RETRIES:
                # Rate-limit exhaustion → typed RateLimitError.
                wait = _parse_retry_after(e.headers, int(BACKOFF_BASE_SECONDS * (2**attempt)))
                raise RateLimitError(
                    url, wait, f"Rate limited on {url} after {MAX_RETRIES} retries."
                ) from e
            wait = _parse_retry_after(e.headers, int(BACKOFF_BASE_SECONDS * (2**attempt)))
            print(
                f"mat-vis-client: rate limited (HTTP {e.code}), "
                f"retry {attempt + 1}/{MAX_RETRIES} in {wait}s",
                file=sys.stderr,
            )
            time.sleep(wait)
        except urllib.error.URLError as e:
            # Network-level error (DNS / connection reset / timeout). Retry.
            last_err = e
            if attempt >= MAX_RETRIES:
                raise NetworkError(url, str(e.reason)) from e
            wait = min(int(BACKOFF_BASE_SECONDS * (2**attempt)), RETRY_MAX_WAIT_SECONDS)
            print(
                f"mat-vis-client: network error ({e.reason}), "
                f"retry {attempt + 1}/{MAX_RETRIES} in {wait}s",
                file=sys.stderr,
            )
            time.sleep(wait)

    # Should be unreachable (loop always raises or returns), but fail loud
    raise MatVisError(f"exhausted {MAX_RETRIES} retries for {url}") from last_err


def _get_json(url: str) -> dict | list:
    """Fetch and parse JSON."""
    return json.loads(_get(url))


def _get_with_etag(url: str, etag: str | None = None) -> tuple[bytes | None, str | None]:
    """Conditional GET. Returns ``(body, response_etag)``.

    Issue #258: replaces the never-invalidated manifest disk cache with a
    cheap conditional GET per client lifecycle. When ``etag`` is non-empty
    we send ``If-None-Match: <etag>``; HF responds 304 Not Modified when
    the dataset hasn't moved on that revision (which on an immutable
    release tag is always — see ADR-0007 / immutable-tag policy):

    - 304 → ``(None, etag)``, caller serves the cached body.
    - 200 → ``(body, response_etag)``; ``response_etag`` may be None
      when the origin / mirror omits ``ETag`` — body still cached, but
      next lifecycle re-fetches unconditionally (defensive cold-start).

    Mirrors ``_get``'s retry/backoff envelope by reusing the same loop
    structure: 429 / 503 / network failures retry with exponential
    backoff and Retry-After honored; 304 short-circuits before the
    rate-limit handler sees it; other HTTP errors raise typed
    ``HTTPFetchError`` like ``_get``.
    """
    hdrs = {"User-Agent": USER_AGENT}
    if etag:
        hdrs["If-None-Match"] = etag

    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                return data, resp.headers.get("ETag")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                # Not Modified — caller's cached body is authoritative.
                return None, etag
            last_err = e
            if not _is_rate_limited(e):
                raise HTTPFetchError(url, e.code, e.reason or "") from e
            if attempt >= MAX_RETRIES:
                wait = _parse_retry_after(e.headers, int(BACKOFF_BASE_SECONDS * (2**attempt)))
                raise RateLimitError(
                    url, wait, f"Rate limited on {url} after {MAX_RETRIES} retries."
                ) from e
            wait = _parse_retry_after(e.headers, int(BACKOFF_BASE_SECONDS * (2**attempt)))
            print(
                f"mat-vis-client: rate limited (HTTP {e.code}), "
                f"retry {attempt + 1}/{MAX_RETRIES} in {wait}s",
                file=sys.stderr,
            )
            time.sleep(wait)
        except urllib.error.URLError as e:
            last_err = e
            if attempt >= MAX_RETRIES:
                raise NetworkError(url, str(e.reason)) from e
            wait = min(int(BACKOFF_BASE_SECONDS * (2**attempt)), RETRY_MAX_WAIT_SECONDS)
            print(
                f"mat-vis-client: network error ({e.reason}), "
                f"retry {attempt + 1}/{MAX_RETRIES} in {wait}s",
                file=sys.stderr,
            )
            time.sleep(wait)

    raise MatVisError(f"exhausted {MAX_RETRIES} retries for {url}") from last_err


def _in_range(value: float | None, lo: float, hi: float) -> bool:
    """Check if a value falls within [lo, hi]. None values never match."""
    if value is None:
        return False
    return lo <= value <= hi


# Schema versions this client understands. Manifest declares its own
# schema_version field; if the manifest version is outside this set,
# the client refuses to operate rather than silently misreading data.
COMPATIBLE_SCHEMA_VERSIONS = frozenset([2, 3])  # 3 = per-file substrate (#186 / ADR-0012)


class MatVisClient:
    """Lightweight client for mat-vis texture data.

    The client is decoupled from the data: client version is semver
    (API stability), data releases are calver (upstream snapshot).
    Compatibility is negotiated via ``schema_version`` in the manifest.

    Data source selection (in precedence order):

    1. ``manifest_url=...`` — explicit URL (custom mirror, air-gapped setup)
    2. ``tag="v2026.04.0"`` — specific release tag on MorePET/mat-vis
    3. default — latest release (resolves ``releases/latest/download/...``)

    Plus ``cache_dir=Path(...)`` to override ``$MAT_VIS_CACHE`` /
    ``~/.cache/mat-vis``.

    Examples::

        client = MatVisClient()                               # latest release
        client = MatVisClient(tag="v2026.04.0")               # pinned
        client = MatVisClient(manifest_url="https://mirror/manifest.json")
        client = MatVisClient(cache_dir=Path("/scratch/mat-vis"))
    """

    def __init__(
        self,
        *,
        manifest_url: str | None = None,
        cache_dir: Path | None = None,
        tag: str | None = None,
        cache: bool = True,
    ):
        self._cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._cache = cache
        self._manifest: dict | None = None
        # Per-file substrate (#186 / ADR-0012): which (source, tier) pairs
        # we've already verified carry a .tier_complete sentinel. The
        # probe is a single HEAD request, cached process-wide.
        self._tier_complete: dict[tuple[str, str], bool] = {}
        self._indexes: dict[str, list[dict]] = {}
        # Cached alternate clients keyed by tag (populated by .at()).
        # Each shares this instance's cache_dir + cache flag so all tag
        # scopes resolve under one root.
        self._alt_clients: dict[str, MatVisClient] = {}
        self._tag = tag

        if manifest_url:
            self._manifest_url = manifest_url
        else:
            # v0.6.0: HF substrate only. No "latest" alias on HF — the
            # client picks a sensible default release (DEFAULT_TAG) so
            # out-of-the-box use returns real data instead of the empty
            # `main` baseline (#242). Explicit ``tag=...`` overrides.
            rev = tag or DEFAULT_TAG
            self._manifest_url = f"{HF_BASE}/{rev}/release-manifest.json"

    @property
    def _cache_scope(self) -> Path:
        """Tag-scoped cache subdirectory.

        Keeps data for different release tags in separate subtrees so a
        ``tag=v1`` cache never serves bytes for a ``tag=v2`` request.
        When no explicit tag is pinned, the ``"latest"`` sentinel is used
        — invalidation of that bucket is the caller's responsibility
        (or, more typically, handled by the update-check TTL).
        """
        return self._cache_dir / (self._tag or "latest")

    def at(self, tag: str) -> "MatVisClient":
        """Return a client pinned to ``tag``, sharing this one's cache.

        Cheap lazy alternate: reuses the parent's ``cache_dir`` and
        ``cache`` flag so every tag lives under a common root and the
        tag-scoped cache paths stay coherent. Subclients are memoized,
        so ``client.at("v1")`` twice returns the same instance.

        Used internally to implement per-operation ``tag=`` kwargs:
        ``client.fetch_texture(..., tag="v1")`` delegates to
        ``client.at("v1").fetch_texture(...)``.
        """
        if tag == self._tag:
            return self
        if tag not in self._alt_clients:
            self._alt_clients[tag] = MatVisClient(
                cache_dir=self._cache_dir, tag=tag, cache=self._cache
            )
        return self._alt_clients[tag]

    # ── cache-aware I/O helpers (single source of gating) ──────────

    def _cache_read_bytes(self, path: Path) -> bytes | None:
        if not self._cache or not path.exists():
            return None
        return path.read_bytes()

    def _cache_write_bytes(self, path: Path, data: bytes) -> None:
        if not self._cache:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _cache_read_text(self, path: Path) -> str | None:
        if not self._cache or not path.exists():
            return None
        return path.read_text()

    def _cache_write_text(self, path: Path, text: str) -> None:
        if not self._cache:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def _cache_read_manifest_with_etag(self) -> tuple[str | None, str | None]:
        """Return the cached manifest body + ETag, or ``(None, None)``.

        Issue #258: pairs the on-disk manifest cache with its ETag so a
        conditional GET can validate against the remote without
        refetching the body. Bare ``.manifest.json`` without
        ``.manifest.etag`` is treated as etag-less (forces an
        unconditional GET next lifecycle, then we adopt whatever ETag
        the server hands back).
        """
        body = self._cache_read_text(self._cache_scope / ".manifest.json")
        if body is None:
            return None, None
        etag = self._cache_read_text(self._cache_scope / ".manifest.etag")
        return body, (etag or None)

    def _cache_write_manifest(self, body: str | bytes, etag: str | None) -> None:
        """Persist manifest body + ETag side-by-side.

        ``body`` accepts bytes (raw response) or str (already-decoded);
        we always store as text. The ETag file is only written when the
        server provided one — absent ``.manifest.etag`` signals "next
        lifecycle, refetch unconditionally" (defensive cold-start).
        """
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        self._cache_write_text(self._cache_scope / ".manifest.json", body)
        etag_path = self._cache_scope / ".manifest.etag"
        if etag:
            self._cache_write_text(etag_path, etag)
        elif etag_path.exists():
            # Stale etag from a prior lifecycle would falsely 304 us
            # against a manifest we no longer have. Clear it.
            try:
                etag_path.unlink()
            except OSError:
                pass

    @property
    def manifest(self) -> dict:
        """Return the v3 manifest for the pinned revision.

        Read directly from the authoritative ``release-manifest.json``
        file at the dataset root — one HTTP GET, no tree-listing
        reconstruction (the HF tree API caps at 1000 entries per page,
        which prod bakes blow past easily, see #238).

        Cache strategy (#258): one conditional GET per client lifecycle.
        On the first access we send ``If-None-Match: <cached etag>``;
        the server responds 304 if the manifest hasn't moved (which on
        an immutable release tag is always — see README's immutable-tag
        note) and we serve the cached body. On 200 we replace both body
        and ETag. Repeat accesses in the same process hit the in-memory
        cache and never touch HTTP.
        """
        if self._manifest is None:
            cached_body, cached_etag = self._cache_read_manifest_with_etag()
            body, new_etag = _get_with_etag(self._manifest_url, etag=cached_etag)
            if body is None:
                # 304 Not Modified — cached body is still authoritative.
                # cached_body is guaranteed non-None here because
                # _get_with_etag only returns body=None when an
                # If-None-Match was sent, which requires cached_etag,
                # which requires we had a cached body alongside it.
                assert cached_body is not None
                self._manifest = json.loads(cached_body)
            else:
                if isinstance(body, bytes):
                    body_text = body.decode("utf-8")
                else:
                    body_text = body
                self._manifest = json.loads(body_text)
                self._cache_write_manifest(body_text, etag=new_etag)
            self._check_schema_version(self._manifest)
            self._maybe_warn_updates()
        return self._manifest

    # ── update checks ──────────────────────────────────────────

    def check_updates(self, *, force: bool = False) -> dict:
        """Check for newer data release and newer client version.

        Results cached for 24h in the cache dir. Pass ``force=True`` to
        skip cache. Returns a dict with ``data`` and ``client`` keys,
        each with ``current``, ``latest``, ``newer_available`` fields.
        """
        cache_path = self._cache_dir / ".update-check.json"
        if not force and cache_path.exists():
            try:
                age = time.time() - cache_path.stat().st_mtime
                if age < UPDATE_CHECK_TTL_SECONDS:
                    return json.loads(cache_path.read_text())
            except (OSError, json.JSONDecodeError):
                pass

        result = {
            "data": self._check_data_version(),
            "client": self._check_client_version(),
        }
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            cache_path.write_text(json.dumps(result, indent=2))
        except OSError:
            pass
        return result

    def _check_data_version(self) -> dict:
        """Compare current/pinned tag against releases/latest on GitHub."""
        # Prefer the manifest's own release_tag — works for both pinned
        # and latest. Falls back to self._tag if manifest isn't loaded.
        try:
            current = self.manifest.get("release_tag") or self._tag
        except Exception:
            current = self._tag
        try:
            data = _get_json(f"{GITHUB_API}/releases/latest")
            latest = data.get("tag_name")
        except Exception:
            latest = None
        return {
            "current": current,
            "latest": latest,
            "newer_available": bool(latest and current and latest != current),
        }

    @staticmethod
    def _check_client_version() -> dict:
        """Compare installed client version against latest on PyPI."""
        try:
            from importlib.metadata import PackageNotFoundError, version

            try:
                current = version("mat-vis-client")
            except PackageNotFoundError:
                current = None
        except ImportError:
            current = None
        try:
            data = _get_json(PYPI_API)
            latest = data.get("info", {}).get("version")
        except Exception:
            latest = None
        return {
            "current": current,
            "latest": latest,
            "newer_available": bool(latest and current and latest != current),
        }

    @staticmethod
    def _should_check_updates() -> bool:
        """Decide whether to emit an update-available notice.

        Precedence (first match wins):

        * ``MAT_VIS_NO_UPDATE_CHECK=1`` → never check (back-compat opt-out).
        * ``MAT_VIS_UPDATE_CHECK=1`` → always check (force-on for CI debug).
        * Default → check only when stderr is a TTY. This is the
          pip / uv pattern: interactive terminals get notices, scripted
          pipelines and CI don't get noisy output bleeding into their
          stderr stream.
        """
        if UPDATE_CHECK_DISABLED:
            return False
        if UPDATE_CHECK_FORCED:
            return True
        try:
            return bool(sys.stderr.isatty())
        except (AttributeError, ValueError):
            # Unusual stderr (closed, wrapped by a non-file-like object).
            # Be conservative: skip the check.
            return False

    def _maybe_warn_updates(self) -> None:
        """Emit a one-line INFO log record if newer data or client exists.

        Runs once per process. Uses ``logging.getLogger("mat-vis-client")``
        so library consumers who want notices configure their logger, and
        those who don't aren't spammed — matches numpy/requests/polars
        etiquette (no stderr writes on import).

        Default gating: only emit when stderr is a TTY. Override with
        ``MAT_VIS_UPDATE_CHECK=1`` (force-on) or disable entirely with
        ``MAT_VIS_NO_UPDATE_CHECK=1``. Network failures are silent.
        """
        if getattr(self, "_update_warned", False):
            return
        self._update_warned = True
        if not self._should_check_updates():
            return
        try:
            result = self.check_updates()
        except Exception:
            return

        data = result["data"]
        client = result["client"]
        if data["newer_available"]:
            log.info(
                "mat-vis: newer data release available (%s -> %s). "
                "Use MatVisClient() for latest or set tag=%r.",
                data["current"],
                data["latest"],
                data["latest"],
            )
        if client["newer_available"]:
            log.info(
                "mat-vis-client: newer version available (%s -> %s). "
                "Upgrade: pip install -U mat-vis-client",
                client["current"],
                client["latest"],
            )

    @staticmethod
    def _check_schema_version(manifest: dict) -> None:
        """Refuse to operate on manifests with missing or incompatible schema.

        Requires the canonical ``schema_version`` field. A missing field
        almost always means a stale cached manifest from before the
        schema-version contract existed — surface a clear recovery path
        (``mat-vis-client cache clear``) rather than silently guessing.
        """
        if "schema_version" not in manifest:
            raise RuntimeError(
                "Manifest is missing 'schema_version'. This usually means a "
                "stale cached manifest from an older release. Clear the "
                "cache and retry: `mat-vis-client cache clear` (or delete "
                "~/.cache/mat-vis/.manifest.json). If the problem persists, "
                "upgrade the data release or the client: "
                "`pip install -U mat-vis-client`."
            )
        schema = manifest["schema_version"]
        if schema not in COMPATIBLE_SCHEMA_VERSIONS:
            raise RuntimeError(
                f"mat-vis-client does not support manifest schema_version={schema}. "
                f"This client supports: {sorted(COMPATIBLE_SCHEMA_VERSIONS)}. "
                f"Upgrade with: pip install -U mat-vis-client"
            )

    def _revision(self) -> str:
        """Pinned revision for HF resolve URLs.

        v0.6.0 requires a tagged revision — there is no "latest" alias
        on HF the way `releases/latest` worked on GitHub. Falls back to
        ``DEFAULT_TAG`` (#242) for clients constructed with no tag, so
        ``client.fetch_texture(...)`` returns real data out-of-the-box
        even when the manifest lacks a ``release_tag`` field.
        """
        return self._tag or self.manifest.get("release_tag", DEFAULT_TAG)

    def _hf_url(self, path: str) -> str:
        return f"{HF_BASE}/{self._revision()}/{path}"

    def sources(self, tier: str | None = None) -> list[str]:
        """List sources. With ``tier`` set, restricts to sources that
        actually published that tier in this revision."""
        sources = self.manifest.get("sources", {})
        if tier is None:
            return sorted(sources.keys())
        return sorted(name for name, entry in sources.items() if tier in (entry.get("tiers") or {}))

    def tiers(self, source: str | None = None) -> list[str]:
        """List tiers published in this revision. With ``source``, just that source's."""
        sources = self.manifest.get("sources", {})
        if source is not None:
            src_entry = _lookup(sources, source, kind="source")
            return sorted((src_entry.get("tiers") or {}).keys())
        found: set[str] = set()
        for entry in sources.values():
            found.update((entry.get("tiers") or {}).keys())
        return sorted(found)

    def categories(self) -> tuple[str, ...]:
        """Discover material categories from the current release's catalogs.

        v0.6.0 reads `mat_vis.category` from each source's catalog entry
        (ADR-0011 / mat-vis#152) — the old substrate encoded categories
        in parquet filenames; that dimension no longer exists
        (ADR-0007 drops per-category partitioning). Always reflects the
        actual release.
        """
        global CATEGORIES
        found: set[str] = set()
        for source in self.sources():
            for entry in self.index(source):
                cat = (entry.get("mat_vis") or {}).get("category")
                if cat:
                    found.add(cat)
        result = tuple(sorted(found))
        CATEGORIES = frozenset(result)
        return result

    def materials(self, source: str, tier: str | None = None) -> list[str]:
        """List material IDs for a source, optionally filtered by tier.

        v0.6.0+ (#186 / ADR-0012): derived from the v3 catalog —
        entries whose ``available_tiers`` list includes ``tier``.

        ``tier`` omitted (``None``, mat-vis#339): returns ALL materials
        in the source that advertise at least one tier — deduped across
        tiers. Hides the ``"scalar"`` sentinel from consumers of
        scalar-only sources like physicallybased; ``materials("physicallybased")``
        just works without the user having to type
        ``materials("physicallybased", "scalar")``. Bernhard's #281
        instinct (``tier=None``) is the friendlier path for both
        scalar-only and "I just want the catalog" multi-tier cases.

        Explicit ``tier`` still wins for callers that want a hard filter.
        """
        sources = self.manifest.get("sources", {})
        src_entry = _lookup(sources, source, kind="source")

        idx = self._load_index_raw(source)
        out: list[str] = []

        if tier is None:
            # No tier filter — return every material with ≥1 tier.
            # Excludes pre-#331 physicallybased rows that had
            # ``available_tiers=[]`` (those are inert anyway). Future
            # multi-tier sources get the deduped union here.
            for entry in idx:
                if not isinstance(entry, dict):
                    continue
                if (entry.get("available_tiers") or []) and entry.get("id"):
                    out.append(entry["id"])
            return sorted(out)

        # Explicit tier — validate against manifest before catalog scan.
        _lookup(
            src_entry.get("tiers") or {},
            tier,
            kind="tier",
            context=f"source {source!r}",
        )
        for entry in idx:
            if not isinstance(entry, dict):
                continue
            tiers = entry.get("available_tiers") or []
            if tier in tiers and entry.get("id"):
                out.append(entry["id"])
        return sorted(out)

    def channels(self, source: str, material_id: str, tier: str) -> list[str]:
        """List channels available for a material at a tier.

        v0.6.0+ (#186 / ADR-0012): read from the v3 catalog entry's
        ``maps`` list (or ``texture_hashes`` keys as fallback). The
        ``tier`` is required to confirm the material is actually staged
        for that resolution; mismatches raise the existing typed errors
        via ``_resolve_material_id``.
        """
        resolved = self._resolve_material_id(source, material_id, tier)
        idx = self._load_index_raw(source)
        for entry in idx:
            if not isinstance(entry, dict):
                continue
            if entry.get("id") == resolved:
                maps = entry.get("maps") or list((entry.get("texture_hashes") or {}).keys())
                return sorted(m for m in maps if isinstance(m, str))
        return []

    # ── Index & search ──────────────────────────────────────────

    def _index_url(self, source: str) -> str:
        """Build the URL for a source's catalog JSON on HF."""
        # The manifest's per-source entry is the authoritative pointer;
        # fall back to the convention for callers constructing a URL
        # before loading the manifest.
        try:
            catalog = self.manifest["sources"][source]["catalog"]
        except (KeyError, TypeError):
            catalog = f"{source}.json"
        return self._hf_url(catalog)

    def _load_index_raw(self, source: str) -> list[dict]:
        """Fetch + cache the per-source catalog JSON, verbatim (with ``upstream``).

        Internal accessor used by :meth:`upstream`, :meth:`_resolve_material_id`,
        :meth:`_scalars_for`, and :meth:`search`'s filter loop — anywhere the
        server-side shape is needed. Public callers get the stripped view from
        :meth:`index`.

        Guards the v2/v3 boundary: a v3 client pointed at a v2 catalog (e.g.
        a user who pinned ``tag="v2026.04.0"`` before rebaking) would silently
        return empty ``search()`` / ``categories()`` because every ``mat_vis``
        lookup misses. Fail loudly instead (ADR-0011 / mat-vis#152).
        """
        if source not in self._indexes:
            cache_path = self._cache_scope / ".indexes" / f"{source}.json"
            cached = self._cache_read_text(cache_path)
            if cached is not None:
                self._indexes[source] = json.loads(cached)
            else:
                data = _get_json(self._index_url(source))
                self._indexes[source] = data
                self._cache_write_text(cache_path, json.dumps(data, indent=2))
            self._assert_v3_catalog(source, self._indexes[source])
        return self._indexes[source]

    @staticmethod
    def _assert_v3_catalog(source: str, entries: list[dict]) -> None:
        """Raise if ``entries`` is a pre-ADR-0011 (v2) catalog.

        Detection: any entry missing a top-level ``mat_vis`` key but carrying
        one of the v2 semantic keys (``category``, ``name`` directly). A truly
        empty catalog (``entries=[]``) is ambiguous but harmless — no silent
        failure surface, so allow it.
        """
        if not isinstance(entries, list) or not entries:
            return
        sample = entries[0]
        if not isinstance(sample, dict):
            return
        if "mat_vis" in sample:
            return
        # v2-shaped entry: top-level semantic fields instead of mat_vis block.
        if any(k in sample for k in ("category", "color_hex", "roughness")):
            raise MatVisError(
                f"catalog for source {source!r} predates ADR-0011 (v2 shape). "
                f"This client requires v3 catalogs (mat_vis block). "
                f"Pin tag='v2026.04.1' or newer, or downgrade to mat-vis-client 0.5.x."
            )

    @staticmethod
    def _strip_upstream(entry: dict) -> dict:
        """Return a shallow copy of ``entry`` with the ``upstream`` key removed.

        Layer-2 (``upstream.raw``) is explicitly NOT stable — shipping it in
        every ``index()`` / ``search()`` response would drag unstable upstream
        shape into the query surface. Consumers that want it use
        :meth:`upstream` directly.

        Shallow copy only — inner dicts are shared with the cache. Caller
        promises not to mutate; that matches the existing read-only
        contract on search results.
        """
        if "upstream" not in entry:
            return entry
        return {k: v for k, v in entry.items() if k != "upstream"}

    def index(self, source: str) -> list[dict]:
        """Fetch and cache the per-source catalog JSON.

        v0.6.0: resolves to ``<HF_BASE>/<revision>/<source>.json`` via
        the manifest. No GH-Raw fallback — the catalog lives in the
        same dataset revision as everything else (ADR-0007).

        The ``upstream`` block (Layer 2 of ADR-0011) is stripped from
        every entry — it's the verbatim upstream response, intentionally
        NOT part of the stable query surface. Use :meth:`upstream` to
        access it for a specific material.
        """
        return [self._strip_upstream(e) for e in self._load_index_raw(source)]

    def upstream(
        self,
        source: str,
        material_id: str,
        tier: str = "1k",
    ) -> dict:
        """Return the verbatim upstream metadata for a material.

        The shape is source-specific and **unstable** — not covered by
        semver. Use this for advanced queries that need upstream fields
        not exposed via ``mat_vis.*``; for anything that MUST be stable,
        stick to ``client.index()`` / ``client.search()`` and the
        Layer-1 contract.

        ``material_id`` may be the canonical id or a human-readable name;
        resolution goes through the same path as every other per-material
        accessor (:meth:`_resolve_material_id`). Unknown ids / ambiguous
        names raise typed errors just like ``fetch_texture``.

        Returns ``{}`` when the entry exists but carries no ``upstream``
        block — typical for pre-v3 catalogs that haven't been re-baked.
        """
        resolved = self._resolve_material_id(source, material_id, tier)
        for entry in self._load_index_raw(source):
            if entry.get("id") != resolved:
                continue
            upstream = entry.get("upstream") or {}
            raw = upstream.get("raw")
            if not isinstance(raw, dict):
                return {}
            return raw
        # _resolve_material_id should have caught missing ids, but be
        # defensive — the rowmap/index disagreement path is real.
        raise UnknownMaterialError(
            key=material_id,
            available=[],
            context=f"{source}/{tier}",
        )

    _SCALAR_WIDEN = 0.2  # scalar shorthand → range half-width

    def search(
        self,
        category: str | None = None,
        *,
        roughness: float | None = None,
        metalness: float | None = None,
        roughness_range: tuple[float, float] | None = None,
        metalness_range: tuple[float, float] | None = None,
        source: str | None = None,
        tier: str = "1k",
        tag: str | None = None,
        score: bool = False,
        limit: int | None = None,
    ) -> list[dict]:
        """Search materials by category and scalar ranges.

        Fetches index JSON for the given source (or all sources for the
        tier) and filters locally. Returns matching index entries.

        Args:
            category: Filter by material category (e.g. "metal", "wood").
            roughness: Scalar shorthand. Matches within ± ``_SCALAR_WIDEN``.
                Mutually exclusive with ``roughness_range``.
            metalness: Scalar shorthand. Same semantics as ``roughness``.
            roughness_range: (min, max) roughness filter, inclusive.
            metalness_range: (min, max) metalness filter, inclusive.
            source: Limit search to one source. If None, searches all
                    sources available for the given tier.
            tier: Only return materials that have this tier available.
            tag: Optional release tag override (see .at()).
            score: When True and a scalar shorthand is passed, attach a
                ``score`` field (absolute distance) and sort ascending.
            limit: Cap the returned list length.
        """
        if tag is not None and tag != self._tag:
            return self.at(tag).search(
                category,
                roughness=roughness,
                metalness=metalness,
                roughness_range=roughness_range,
                metalness_range=metalness_range,
                source=source,
                tier=tier,
                score=score,
                limit=limit,
            )
        # Scalar + range on the same dimension is ambiguous — reject.
        if roughness is not None and roughness_range is not None:
            raise MatVisError("pass roughness OR roughness_range, not both")
        if metalness is not None and metalness_range is not None:
            raise MatVisError("pass metalness OR metalness_range, not both")
        # Scalar shorthand widens into an inclusive range.
        if roughness is not None:
            roughness_range = (
                max(0.0, roughness - self._SCALAR_WIDEN),
                min(1.0, roughness + self._SCALAR_WIDEN),
            )
        if metalness is not None:
            metalness_range = (
                max(0.0, metalness - self._SCALAR_WIDEN),
                min(1.0, metalness + self._SCALAR_WIDEN),
            )
        if category:
            valid = self.categories()  # discovered from manifest
            if valid and category not in valid:
                # Soft-warn rather than raise — the honest answer to "find
                # materials in a category that has none" is an empty list.
                log.warning(
                    "search: category %r not in manifest %s; returning empty",
                    category,
                    valid,
                )
                return []

        sources = [source] if source else self.sources(tier)
        results: list[dict] = []

        for src in sources:
            for entry in self.index(src):
                mv = entry.get("mat_vis") or {}
                pbr = mv.get("pbr") or {}
                if category and mv.get("category") != category:
                    continue
                if roughness_range and not _in_range(pbr.get("roughness"), *roughness_range):
                    continue
                if metalness_range and not _in_range(pbr.get("metalness"), *metalness_range):
                    continue
                # Scalar-only entries (e.g. physicallybased) advertise no
                # textures — treat missing/empty ``available_tiers`` as
                # tier-independent so they pass any tier filter (#167).
                # Textured entries still get gated to the requested tier.
                entry_tiers = entry.get("available_tiers")
                if entry_tiers and tier not in entry_tiers:
                    continue
                results.append(entry)

        if score and (roughness is not None or metalness is not None):
            for r in results:
                pbr = (r.get("mat_vis") or {}).get("pbr") or {}
                s = 0.0
                if roughness is not None and pbr.get("roughness") is not None:
                    s += abs(pbr["roughness"] - roughness)
                if metalness is not None and pbr.get("metalness") is not None:
                    s += abs(pbr["metalness"] - metalness)
                r["score"] = s
            results.sort(key=lambda r: r["score"])

        if limit is not None:
            results = results[:limit]
        return results

    # ── Bulk operations ─────────────────────────────────────────

    @staticmethod
    def _normalize_name(s: str) -> str:
        """Case/whitespace-fold a name for index lookup. NFKC + casefold."""
        import unicodedata

        return unicodedata.normalize("NFKC", s).strip().casefold()

    def _resolve_material_id(self, source: str, material_id: str, tier: str) -> str:
        """Resolve ``material_id`` to its canonical catalog id.

        Per-file substrate (#186 / ADR-0012): "is this material staged
        for the requested tier?" comes from the v3 catalog entry's
        ``available_tiers`` field, not a separate rowmap.

        Resolution order, for the UX described in mat-vis#143:

        1. Direct id hit + tier in available_tiers → canonical id.
        2. Exact ``id`` match in the catalog but tier not in
           ``available_tiers`` → :class:`MaterialNotStagedError`.
        3. Normalized-name match against ``mat_vis.name``
           (:meth:`_normalize_name`):
           a. >1 match → :class:`AmbiguousMaterialError` (mat-vis#144).
           b. 1 match, tier staged → return id.
           c. 1 match, tier not staged → :class:`MaterialNotStagedError`.
        4. Nothing matches → :class:`UnknownMaterialError`.
        """
        try:
            idx = self.index(source)
        except MatVisError:
            idx = []
        if not isinstance(idx, list):
            idx = []

        norm_query = self._normalize_name(material_id)
        by_id: dict | None = None
        by_name: list[dict] = []

        # Per-entry display name. v3 entries carry it under the
        # ``mat_vis`` envelope; ambientcg/polyhaven flat-v2 entries
        # carry it at the top level (#284). Fall back to the canonical
        # id so the name-list never holds an empty string.
        def _display_name(entry: dict) -> str:
            envelope = entry.get("mat_vis") or {}
            return envelope.get("name") or entry.get("name") or entry.get("id", "")

        for entry in idx:
            if not isinstance(entry, dict):
                continue
            if entry.get("id") == material_id:
                by_id = entry
            entry_name = _display_name(entry)
            if entry_name and self._normalize_name(entry_name) == norm_query:
                by_name.append(entry)

        def _is_staged(entry: dict) -> bool:
            return tier in (entry.get("available_tiers") or [])

        if by_id is not None:
            if _is_staged(by_id):
                return material_id
            # Direct-UUID path: original_name stays None so the legacy
            # single-id message is preserved (#280).
            raise MaterialNotStagedError(source=source, material_id=material_id, tier=tier)

        if len(by_name) > 1:
            # #286: surface human names (or fall back to id) so the
            # disambiguation list is actually useful when names exist.
            raise AmbiguousMaterialError(
                source=source,
                name=material_id,
                candidates=[_display_name(e) for e in by_name if e.get("id")],
            )

        if len(by_name) == 1:
            resolved = by_name[0].get("id", "")
            if _is_staged(by_name[0]):
                return resolved
            # #280: the user passed a name; carry it through so the
            # error message names *which* material in their batch broke.
            raise MaterialNotStagedError(
                source=source,
                material_id=resolved,
                tier=tier,
                original_name=material_id,
            )

        # Build the "available materials at this tier" hint from the
        # catalog. #286: prefer human names over UUIDs and surface
        # close-matches before the full list.
        staged_names = sorted(
            _display_name(e) for e in idx if isinstance(e, dict) and _is_staged(e) and e.get("id")
        )
        close = difflib.get_close_matches(material_id, staged_names, n=5, cutoff=0.6)
        # Cap the full-list to keep messages readable on big catalogs.
        max_full = 50
        if len(staged_names) > max_full:
            shown = staged_names[:max_full]
            shown.append(f"(... {len(staged_names) - max_full} more)")
            available = shown
        else:
            available = staged_names
        # Prepend close-matches when we have any: callers see the
        # likely-typo first, before scrolling the full list.
        if close:
            ordered = list(close) + [a for a in available if a not in close]
        else:
            ordered = available
        raise UnknownMaterialError(
            key=material_id,
            available=ordered,
            context=f"{source}/{tier}",
        )

    def fetch_all_textures(
        self,
        source: str,
        material_id: str,
        tier: str = "1k",
        *,
        tag: str | None = None,
    ) -> dict[str, bytes]:
        """Fetch all texture channels for a material.

        ``material_id`` may be the canonical id (UUID/slug used as the rowmap
        key) or a human-readable name from the source's index; the latter is
        resolved via :meth:`_resolve_material_id` (mat-vis#143). Unknown ids,
        un-staged materials, and ambiguous names raise typed errors rather
        than returning ``{}`` silently (mat-vis#141 / #144).

        Returns a dict mapping channel name to PNG bytes.
        """
        if tag is not None and tag != self._tag:
            return self.at(tag).fetch_all_textures(source, material_id, tier)
        resolved = self._resolve_material_id(source, material_id, tier)
        chs = self.channels(source, resolved, tier)
        return {ch: self.fetch_texture(source, resolved, ch, tier) for ch in chs}

    def prefetch(
        self,
        source: str,
        tier: str = "1k",
        *,
        on_progress: callable | None = None,
        tag: str | None = None,
    ) -> int:
        """Bulk download all materials for a source + tier to cache.

        Args:
            source: Source name (e.g. "ambientcg").
            tier: Resolution tier (default "1k").
            on_progress: Optional callback(material_id, index, total).
            tag: Optional release tag override (see .at()).

        Returns the number of materials fetched.
        """
        if tag is not None and tag != self._tag:
            return self.at(tag).prefetch(source, tier, on_progress=on_progress)
        mat_ids = self.materials(source, tier)
        total = len(mat_ids)

        for i, mid in enumerate(mat_ids):
            self.fetch_all_textures(source, mid, tier)
            if on_progress:
                on_progress(mid, i + 1, total)

        return total

    def materialize(
        self,
        source: str,
        material_id: str,
        tier: str = "1k",
        output_dir: str | Path = ".",
    ) -> Path:
        """Write all texture PNGs for a material to disk.

        Returns the directory containing the PNG files, named by channel
        (e.g. color.png, normal.png, roughness.png).
        """
        out = Path(output_dir) / material_id
        out.mkdir(parents=True, exist_ok=True)

        chs = self.channels(source, material_id, tier)
        for ch in chs:
            png_path = out / f"{ch}.png"
            if not png_path.exists():
                png_bytes = self.fetch_texture(source, material_id, ch, tier)
                png_path.write_bytes(png_bytes)

        return out

    # ── MaterialX API (dotted) ─────────────────────────────────

    def mtlx(
        self,
        source: str,
        material_id: str,
        tier: str = "1k",
        *,
        tag: str | None = None,
    ) -> MtlxSource:
        """Get a lazy :class:`MtlxSource` for a material.

        Use ``.xml`` for the document string, ``.export(path)`` to write
        files, and ``.original`` for the upstream-author variant (None
        if not available for this source).

        Creation is free — no network IO happens until ``.xml`` or
        ``.export(...)`` is called. Pass ``tag=`` to scope the source to
        a specific release (see .at()).
        """
        if tag is not None and tag != self._tag:
            return self.at(tag).mtlx(source, material_id, tier)
        return MtlxSource(self, source, material_id, tier, is_original=False)

    def asset(
        self,
        source: str,
        material_id: str,
        tier: str = "1k",
    ) -> "VisAsset":
        """Return a :class:`VisAsset` ergonomic wrapper for ``(source, material_id, tier)``.

        VisAsset bundles identity, lazy scalars, lazy textures, and adapter
        methods (``.to_threejs() / .to_gltf() / .to_mtlx()``) that delegate
        to the free-function primitive layer in :mod:`mat_vis_client.adapters`.
        Creation is free — no network IO until ``.scalars`` / ``.textures``
        / an adapter method is accessed. Mat-vis#93.
        """
        return VisAsset(self, source, material_id, tier)

    def _scalars_for(self, source: str, material_id: str) -> dict:
        """Look up PBR scalars for a material from the source index.

        Reads ``mat_vis.pbr.*`` (v3 catalog shape, ADR-0011). Returns a
        flat dict keyed by the adapter interface's scalar names
        (``roughness`` / ``metalness`` / ``ior`` / ``color_hex``) —
        ``color_hex`` is synthesized from ``pbr.color_rgb`` for the
        ``to_threejs`` / ``to_gltf`` / ``to_mtlx`` adapters which still
        consume the hex shape.

        Silent on failure — returns ``{}`` if the index is unavailable or
        the material isn't found. Used by :class:`MtlxSource` to fill in
        shader scalar inputs when a texture channel is absent.
        """
        scalars: dict = {}
        try:
            for entry in self.index(source):
                if entry["id"] != material_id:
                    continue
                pbr = (entry.get("mat_vis") or {}).get("pbr") or {}
                for k in ("roughness", "metalness", "ior"):
                    v = pbr.get(k)
                    if v is not None:
                        scalars[k] = v
                rgb = pbr.get("color_rgb")
                if isinstance(rgb, list) and len(rgb) >= 3:
                    r, g, b = rgb[:3]
                    scalars["color_hex"] = "#{:02X}{:02X}{:02X}".format(
                        int(round(r * 255)),
                        int(round(g * 255)),
                        int(round(b * 255)),
                    )
                break
        except Exception:
            pass
        return scalars

    def _fetch_mtlx_original_map(self, source: str) -> dict[str, str]:
        """Fetch and cache the {source}-mtlx.json map. Empty dict on miss.

        First access hits the network and fetches the full JSON map (gpuopen
        is ~22 MB). Subsequent calls return the in-process cache. Any fetch
        error is cached as ``{}`` so we don't retry every call.
        """
        if not hasattr(self, "_mtlx_originals"):
            self._mtlx_originals: dict[str, dict[str, str]] = {}
        if source not in self._mtlx_originals:
            mtlx_path = (
                self.manifest.get("sources", {}).get(source, {}).get("mtlx")
                or f"{source}-mtlx.json"
            )
            try:
                self._mtlx_originals[source] = _get_json(self._hf_url(mtlx_path))
            except Exception:
                self._mtlx_originals[source] = {}
        return self._mtlx_originals[source]

    # ── Per-file texture fetch (#186 / ADR-0012) ───────────────────

    _PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
    _KTX2_MAGIC = b"\xabKTX 20\xbb\r\n\x1a\n"

    def _per_file_url(self, source: str, tier: str, mid: str, channel: str, ext: str) -> str:
        """``<source>/<tier>/<mid>/<channel>.<ext>`` resolve URL on HF."""
        return self._hf_url(f"{source}/{tier}/{mid}/{channel}.{ext}")

    def _assert_tier_complete(self, source: str, tier: str) -> None:
        """Probe ``<source>/<tier>/.tier_complete`` once per process.

        ADR-0012: the sentinel is the final commit per tier, so its
        presence confirms the tier is atomically baked. Probe once and
        cache; reject partial tiers loudly so callers don't silently
        consume half-baked data.
        """
        key = (source, tier)
        if self._tier_complete.get(key):
            return
        url = self._hf_url(f"{source}/{tier}/.tier_complete")
        try:
            _get(url)  # any 2xx response means the sentinel exists
        except Exception as e:  # noqa: BLE001 — wrap in friendly error
            raise MatVisError(
                f"tier {source}/{tier!r} is not atomically complete on this "
                f"release (no .tier_complete sentinel). The bake may still be "
                f"running, or this revision was committed mid-batch. Re-run "
                f"the bake or pin a known-complete tag."
            ) from e
        self._tier_complete[key] = True

    def fetch_texture(
        self,
        source: str,
        material_id: str,
        channel: str,
        tier: str = "1k",
        *,
        tag: str | None = None,
    ) -> bytes:
        """Fetch a single texture via plain HTTPS GET (#186 / ADR-0012).

        URL: ``<HF_BASE>/<tag>/<source>/<tier>/<material_id>/<channel>.{png,ktx2}``.
        PNG is tried first (the common case for textured sources); on
        404 the client falls back to KTX2 for derived ktx2 tiers.

        Per-file substrate replaces the pre-#186 tar+rowmap+Range path.
        Verifies a ``.tier_complete`` sentinel for the requested tier
        on first read so partial bakes never serve half-baked bytes.

        Returns raw bytes. Caches locally under the active tag scope.
        Pass ``tag="v..."`` to delegate to ``self.at(tag)`` for a
        specific release without reinstantiating.
        """
        if tag is not None and tag != self._tag:
            return self.at(tag).fetch_texture(source, material_id, channel, tier)

        # Validate (source, tier) against the manifest first — gives the
        # friendliest possible error before we hit the catalog or HF.
        sources_block = self.manifest.get("sources", {})
        src_entry = _lookup(sources_block, source, kind="source")
        _lookup(
            src_entry.get("tiers") or {},
            tier,
            kind="tier",
            context=f"source {source!r}",
        )

        resolved = self._resolve_material_id(source, material_id, tier)

        # Channel-existence check up front so a friendly error fires
        # before we waste a GET. Mirrors the pre-#186 rowmap-lookup error.
        available = self.channels(source, resolved, tier)
        if channel not in available:
            raise MatVisError(
                f"channel {channel!r} not found "
                f"(context: {source}/{tier}/{resolved}). "
                f"Available: {available}"
            )

        # ext is unknown until we hit one — try .png in cache first
        # (overwhelming common case), fall back to .ktx2 cache key.
        for ext in ("png", "ktx2"):
            cache_path = self._cache_scope / source / tier / resolved / f"{channel}.{ext}"
            cached = self._cache_read_bytes(cache_path)
            if cached is not None:
                return cached

        # Confirm the tier is atomically complete before reading.
        self._assert_tier_complete(source, tier)

        # Try PNG, fall back to KTX2. HF returns 404 for missing files.
        # If the PNG path 404s but a KTX2 lands, that's the derived
        # ktx2 tier shape. If BOTH 404, surface the original error
        # type — callers (and tests) expect HTTPFetchError, not a
        # generic MatVisError, so the network-failure contract is stable.
        # Emit one progress notice per real network fetch (#287). Cache
        # hits returned above stay silent. Library users (build123d,
        # Jupyter) wire this to their own UI; logger is silent by default.
        log.info(
            "Downloading %s/%s/%s @ %s ...",
            source,
            resolved,
            channel,
            tier,
        )

        last_exc: Exception | None = None
        for ext in ("png", "ktx2"):
            url = self._per_file_url(source, tier, resolved, channel, ext)
            try:
                data = _get(url)
            except Exception as e:  # noqa: BLE001
                last_exc = e
                continue
            if not (data.startswith(self._PNG_MAGIC) or data.startswith(self._KTX2_MAGIC)):
                raise ValueError(
                    f"Expected PNG or KTX2 bytes, got {data[:12]!r} "
                    f"({source}/{resolved}/{channel} @ {tier})"
                )
            cache_path = self._cache_scope / source / tier / resolved / f"{channel}.{ext}"
            self._cache_write_bytes(cache_path, data)
            self._maybe_warn_cache_cap()
            return data

        # Both extensions failed. Re-raise the underlying network error
        # so HTTPFetchError (and friends) propagate to callers.
        if last_exc is not None:
            raise last_exc
        raise MatVisError(f"channel {channel!r} not available for {source}/{resolved} @ {tier}")

    # ── Cache management ────────────────────────────────────────

    def cache_size(self) -> int:
        """Total bytes currently in the cache directory (recursive)."""
        if not self._cache_dir.exists():
            return 0
        total = 0
        for p in self._cache_dir.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total

    def cache_status(self) -> dict[str, dict[str, int]]:
        """Breakdown of cache usage by (source, tier) and metadata categories.

        Returns a dict like:
          {
            "ambientcg/1k": {"bytes": 1234, "files": 56},
            "_meta": {"bytes": 100, "files": 2},        # rowmaps + indexes + manifest + mtlx
            "_total": {"bytes": 1334, "files": 58},
          }
        """
        result: dict[str, dict[str, int]] = {}
        total_bytes = 0
        total_files = 0
        if not self._cache_dir.exists():
            result["_total"] = {"bytes": 0, "files": 0}
            return result

        meta_bytes = 0
        meta_files = 0
        for p in self._cache_dir.rglob("*"):
            if not p.is_file():
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            total_bytes += size
            total_files += 1

            rel = p.relative_to(self._cache_dir)
            parts = rel.parts
            if parts[0].startswith("."):
                # .manifest.json, .rowmaps/, .indexes/, .mtlx-original/
                meta_bytes += size
                meta_files += 1
                continue
            # textures: source/tier/material/channel.png
            if len(parts) >= 2:
                key = f"{parts[0]}/{parts[1]}"
                bucket = result.setdefault(key, {"bytes": 0, "files": 0})
                bucket["bytes"] += size
                bucket["files"] += 1

        result["_meta"] = {"bytes": meta_bytes, "files": meta_files}
        result["_total"] = {"bytes": total_bytes, "files": total_files}
        return result

    def cache_clear(self) -> int:
        """Delete all cached data. Returns bytes freed."""
        import shutil

        if not self._cache_dir.exists():
            return 0
        size = self.cache_size()
        shutil.rmtree(self._cache_dir, ignore_errors=True)
        return size

    def cache_prune(
        self,
        *,
        keep_tags: list[str] | None = None,
        tag: str | None = None,
        source: str | None = None,
        tier: str | None = None,
    ) -> int:
        """Delete subsets of the cache. Returns bytes freed.

        Args:
            keep_tags: Keep only these tags' rowmaps/indexes; delete others.
            tag: Delete only this specific tag's rowmap/index files.
            source: Delete only textures for this source.
            tier: Delete only textures for this tier (combined with source if given).
        """
        import shutil

        if not self._cache_dir.exists():
            return 0
        before = self.cache_size()

        # Source/tier-scoped texture pruning
        if source or tier:
            for src_dir in self._cache_dir.iterdir():
                if not src_dir.is_dir() or src_dir.name.startswith("."):
                    continue
                if source and src_dir.name != source:
                    continue
                if tier:
                    tier_dir = src_dir / tier
                    if tier_dir.exists():
                        shutil.rmtree(tier_dir, ignore_errors=True)
                else:
                    shutil.rmtree(src_dir, ignore_errors=True)

        # Tag-scoped pruning of rowmaps/indexes (only meta files have tag prefixes
        # like "<source>-<tier>-<cat>-rowmap.json" — we can't infer tag from these
        # without inspecting JSON content. Match by content's "release_tag" field.)
        if keep_tags or tag:
            keep_set = set(keep_tags) if keep_tags else None
            rowmaps_dir = self._cache_dir / ".rowmaps"
            indexes_dir = self._cache_dir / ".indexes"
            for d in (rowmaps_dir, indexes_dir):
                if not d.exists():
                    continue
                for f in d.iterdir():
                    if not f.is_file() or f.suffix != ".json":
                        continue
                    try:
                        content = json.loads(f.read_text())
                        file_tag = content.get("release_tag")
                    except Exception:
                        continue
                    if tag and file_tag == tag:
                        f.unlink(missing_ok=True)
                    elif keep_set and file_tag and file_tag not in keep_set:
                        f.unlink(missing_ok=True)

            # Manifest is current-tag only — drop if not in keep_tags.
            # The sibling .manifest.etag (#258) goes with it; a stray
            # etag without a body would falsely 304 us against nothing.
            mf = self._cache_dir / ".manifest.json"
            if mf.exists() and (keep_set or tag):
                try:
                    mtag = json.loads(mf.read_text()).get("release_tag")
                except Exception:
                    mtag = None
                if (tag and mtag == tag) or (keep_set and mtag and mtag not in keep_set):
                    mf.unlink(missing_ok=True)
                    (self._cache_dir / ".manifest.etag").unlink(missing_ok=True)

        return before - self.cache_size()

    def _maybe_warn_cache_cap(self) -> None:
        """Warn once per process if cache exceeds the soft cap."""
        if DEFAULT_CACHE_MAX_BYTES <= 0:
            return
        if getattr(self, "_cap_warned", False):
            return
        size = self.cache_size()
        if size > DEFAULT_CACHE_MAX_BYTES:
            print(
                f"mat-vis: cache is {_fmt_size(size)} (soft cap "
                f"{_fmt_size(DEFAULT_CACHE_MAX_BYTES)}).\n"
                f"         Run `mat-vis-client cache prune` to clean up.\n"
                f"         Raise the cap with MAT_VIS_CACHE_MAX_SIZE=20GB.",
                file=sys.stderr,
            )
            self._cap_warned = True


# ── MaterialX façade ────────────────────────────────────────────


class MtlxSource:
    """Lazy façade for a material's MaterialX document.

    Two forms, both reachable from :meth:`MatVisClient.mtlx`:

    * **Synthesized** — ``client.mtlx(src, id, tier)``. Always available
      (UsdPreviewSurface wrapper over our PNG channels).
    * **Original** — ``client.mtlx(src, id, tier).original`` or ``None``.
      The upstream-author MaterialX document. Currently only gpuopen
      ships these; other sources return ``None``.

    Both variants expose the same two accessors:

    * ``.xml`` — the document as a string (no files written)
    * ``.export(output_dir)`` — writes channel PNGs + the ``.mtlx``
      file referencing them by local path; returns the mtlx path

    No network IO happens in ``__init__``. First access to ``.xml``,
    ``.export(...)``, or ``.original`` is what triggers fetching.
    """

    def __init__(
        self,
        client: MatVisClient,
        source: str,
        material_id: str,
        tier: str,
        *,
        is_original: bool = False,
    ):
        self._client = client
        self._source = source
        self._material_id = material_id
        self._tier = tier
        self._is_original = is_original
        self._xml_cache: str | None = None

    @property
    def source(self) -> str:
        """The mat-vis source name (e.g. ``"ambientcg"``, ``"gpuopen"``)."""
        return self._source

    @property
    def material_id(self) -> str:
        """The material identifier within the source."""
        return self._material_id

    @property
    def tier(self) -> str:
        """The resolution tier (e.g. ``"1k"``, ``"2k"``)."""
        return self._tier

    @property
    def is_original(self) -> bool:
        """True if this is the upstream-author document, not synthesized."""
        return self._is_original

    def xml(self) -> str:
        """Return the MaterialX XML as a string.

        Method, not a property: callers make the network cost explicit.
        This also ports straight to JS/Rust reference clients, which
        don't have attribute-triggered IO.

        * Synthesized: generated in-memory from scalars + channel list.
          No PNGs are written and no texture bytes are fetched.
        * Original: pulls the upstream XML from the cached per-source
          ``{source}-mtlx.json`` map on first access. Subsequent calls
          on the same instance return the cached string.

        Raises:
            LookupError: if this is an original variant but the
                material disappeared from the upstream map between
                ``original()`` and ``xml()`` (rare — shouldn't happen
                since ``original()`` checks presence).
        """
        if self._xml_cache is not None:
            return self._xml_cache

        if self._is_original:
            xml_str = self._client._fetch_mtlx_original_map(self._source).get(self._material_id)
            if xml_str is None:
                raise LookupError(f"No original MaterialX for {self._source}/{self._material_id}")
            self._xml_cache = xml_str
            return xml_str

        # Synthesized: build XML from scalars + channel list, referencing
        # PNGs by <material_id>/<channel>.png (relative paths that line up
        # with what .export() writes). No PNG bytes fetched.
        chs = self._client.channels(self._source, self._material_id, self._tier)
        scalars = self._client._scalars_for(self._source, self._material_id)
        # Reference PNGs relative to the mtlx file — matches the layout
        # .export() produces (.mtlx alongside channel PNGs in one dir).
        self._xml_cache = _render_synthesized_mtlx_xml(
            scalars=scalars,
            channels=chs,
            material_name=self._material_id,
        )
        return self._xml_cache

    def export(self, output_dir: str | Path) -> Path:
        """Materialize PNGs + write the ``.mtlx`` file. Returns the mtlx path.

        Layout (same for synthesized and original):
        ``<output_dir>/<material_id>/<channel>.png`` + ``<material_id>.mtlx``.

        * Synthesized: generates the document with local PNG references.
        * Original: fetches upstream XML, then rewrites texture filename
          references to point at the local PNGs using a heuristic
          name-to-channel map (``BaseColor.png`` → ``color.png`` etc.).
        """
        from mat_vis_client.adapters import export_mtlx

        tex_dir = self._client.materialize(self._source, self._material_id, self._tier, output_dir)
        chs = self._client.channels(self._source, self._material_id, self._tier)

        if not self._is_original:
            scalars = self._client._scalars_for(self._source, self._material_id)
            return export_mtlx(
                scalars=scalars,
                output_dir=str(tex_dir),
                material_name=self._material_id,
                texture_dir=str(tex_dir),
                channels=chs,
            )

        # Original: fetch upstream, rewrite filename values to local PNGs.
        xml_str = self.xml()  # raises LookupError if gone from the map
        rewritten = _rewrite_mtlx_texture_paths(xml_str, tex_dir, chs)
        mtlx_path = tex_dir / f"{self._material_id}.mtlx"
        mtlx_path.write_text(rewritten, encoding="utf-8")
        return mtlx_path

    def original(self) -> MtlxSource | None:
        """Return the upstream-author variant if available, else ``None``.

        Method, not a property: first call for a given source fetches a
        JSON map from the network. Only synthesized :class:`MtlxSource`
        instances have an original — calling ``original()`` on an already-
        original instance returns ``None``.

        Fast after first call: the per-source ``{source}-mtlx.json`` map
        is cached at the client level.
        """
        if self._is_original:
            return None
        mtlx_map = self._client._fetch_mtlx_original_map(self._source)
        if self._material_id not in mtlx_map:
            return None
        return MtlxSource(
            self._client,
            self._source,
            self._material_id,
            self._tier,
            is_original=True,
        )


# ── VisAsset ────────────────────────────────────────────────────

_VIS_ASSET_FROZEN = ("_source", "_material_id", "_tier")


class VisAsset:
    """Bundle of (identity + lazy scalars + lazy textures + adapters).

    Two-layer shape (mat-vis#93): VisAsset is the ergonomic class; the
    underlying ``to_threejs`` / ``to_gltf`` / ``export_mtlx`` free functions
    in :mod:`mat_vis_client.adapters` remain the stable primitives. Adapter
    methods on this class call those primitives with identity-bound args.

    Mirrors ``requests.Session`` / ``requests.get()`` and
    ``subprocess.Popen`` / ``subprocess.run()``: class as ergonomic
    surface, free functions as port-friendly primitives.

    Identity is **immutable** — assigning to ``source``/``material_id``/``tier``
    raises ``AttributeError``. Use :meth:`with_tier` to spawn a new
    instance for a different tier. Equality and hashing are identity-only
    (same triple, even across distinct client instances).
    """

    __slots__ = (
        "_client",
        "_source",
        "_material_id",
        "_tier",
        "_scalars_cache",
        "_textures_cache",
        "_initialized",
    )

    def __init__(
        self,
        client: MatVisClient,
        source: str,
        material_id: str,
        tier: str,
    ) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_source", source)
        object.__setattr__(self, "_material_id", material_id)
        object.__setattr__(self, "_tier", tier)
        object.__setattr__(self, "_scalars_cache", None)
        object.__setattr__(self, "_textures_cache", None)
        object.__setattr__(self, "_initialized", True)

    @classmethod
    def from_client(
        cls,
        client: MatVisClient,
        source: str,
        material_id: str,
        tier: str = "1k",
    ) -> "VisAsset":
        return cls(client, source, material_id, tier)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_initialized", False) and name in _VIS_ASSET_FROZEN:
            raise AttributeError(f"{name} is immutable; use with_tier() / new VisAsset")
        object.__setattr__(self, name, value)

    @property
    def source(self) -> str:
        return self._source

    @property
    def material_id(self) -> str:
        return self._material_id

    @property
    def tier(self) -> str:
        return self._tier

    def with_tier(self, tier: str) -> "VisAsset":
        """Return a new :class:`VisAsset` with the same source/material_id but a different tier."""
        return VisAsset(self._client, self._source, self._material_id, tier)

    @property
    def scalars(self) -> dict:
        """Lazy PBR scalars (cached after first access).

        Calls :meth:`MatVisClient._scalars_for` exactly once per instance.
        """
        if self._scalars_cache is None:
            object.__setattr__(
                self,
                "_scalars_cache",
                self._client._scalars_for(self._source, self._material_id),
            )
        return self._scalars_cache

    @property
    def textures(self) -> dict[str, bytes]:
        """Lazy channel -> PNG bytes mapping (cached after first access).

        Calls :meth:`MatVisClient.fetch_all_textures` exactly once per
        instance — except for scalar-only sources (e.g. ``physicallybased``,
        whose v3 index entries advertise ``available_tiers=[]``). For those
        we short-circuit to ``{}`` so that ``to_threejs`` / ``to_gltf``
        produce a valid scalars-only material instead of raising
        :class:`MaterialNotStagedError` from the texture-fetch path
        (mat-vis#288). Texture-bearing sources still go through
        :meth:`_resolve_material_id` and raise on misuse.
        """
        if self._textures_cache is None:
            if self._is_scalar_only_entry():
                fetched: dict[str, bytes] = {}
            else:
                fetched = self._client.fetch_all_textures(
                    self._source, self._material_id, self._tier
                )
            object.__setattr__(self, "_textures_cache", fetched)
        return self._textures_cache

    def _is_scalar_only_entry(self) -> bool:
        """True if this asset's index entry advertises no staged tiers.

        Scalar-only sources (currently just ``physicallybased``, but the
        check is shape-driven so future scalar-only sources will work)
        publish ``available_tiers=[]`` for every entry — there are no
        textures to fetch. Best-effort: a missing index or lookup error
        falls back to ``False``, preserving the existing (loud) error
        path through :meth:`fetch_all_textures`.
        """
        try:
            entries = self._client.index(self._source)
        except Exception:
            return False
        if not isinstance(entries, list):
            return False
        norm = self._client._normalize_name(self._material_id)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if (
                entry.get("id") == self._material_id
                or self._client._normalize_name((entry.get("mat_vis") or {}).get("name") or "")
                == norm
            ):
                tiers = entry.get("available_tiers")
                # Explicitly empty list (or missing) → scalar-only entry.
                return not tiers
        return False

    def to_threejs(self, *, color_format: Literal["hex", "int"] = "hex") -> dict:
        """Return a Three.js ``MeshPhysicalMaterial`` parameter dict.

        Wraps :func:`mat_vis_client.adapters.to_threejs` with this asset's
        identity-bound scalars and textures. ``color_format`` is forwarded;
        default is ``"hex"`` (Pythonic ``"#RRGGBB"`` string) since 0.7.0.
        """
        from mat_vis_client.adapters import to_threejs

        return to_threejs(self.scalars, self.textures, color_format=color_format)

    def to_gltf(self) -> dict:
        """Return a glTF 2.0 material dict.

        Wraps :func:`mat_vis_client.adapters.to_gltf` with this asset's
        identity-bound scalars and textures.
        """
        from mat_vis_client.adapters import to_gltf

        return to_gltf(self.scalars, self.textures)

    def to_mtlx(self) -> "MtlxSource":
        """Return a fresh :class:`MtlxSource` for this asset's identity.

        Composition, not replacement: ``MtlxSource`` remains the public
        MaterialX façade. This is the recommended entry point.
        """
        return MtlxSource(self._client, self._source, self._material_id, self._tier)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, VisAsset) and (
            self._source,
            self._material_id,
            self._tier,
        ) == (other._source, other._material_id, other._tier)

    def __hash__(self) -> int:
        return hash((self._source, self._material_id, self._tier))

    def __repr__(self) -> str:
        return f"VisAsset({self._source!r}, {self._material_id!r}, tier={self._tier!r})"


def _render_synthesized_mtlx_xml(
    *,
    scalars: dict,
    channels: list[str],
    material_name: str,
) -> str:
    """Build the synthesized MaterialX XML string with PNG refs like
    ``<material_name>/<channel>.png`` (relative — matches :meth:`export`).
    """
    # We reference PNGs as "<material_name>/<channel>.png" which matches
    # the layout export() produces (mtlx is written into the material
    # dir, so relative refs would just be "<channel>.png"). But xml
    # without export happens too — keep refs scoped by material for
    # consumers who write files themselves.
    from mat_vis_client.adapters import _build_mtlx_tree, _mtlx_tree_to_string

    tex_filenames = {ch: f"{material_name}/{ch}.png" for ch in channels}
    root = _build_mtlx_tree(scalars, tex_filenames, material_name)
    return _mtlx_tree_to_string(root)


# GPUOpen upstream names → our mat-vis channel names.
# Used by the original-mtlx path-rewriter so a <input file value="BaseColor.png"/>
# is redirected to our local "color.png" after materialization.
# Single source of truth: mat_vis_client.schema.CHANNELS (filename_aliases).
from mat_vis_client.schema import FILENAME_TO_CHANNEL as _FILENAME_TO_CHANNEL  # noqa: E402


def _rewrite_mtlx_texture_paths(xml_str: str, tex_dir: Path, channels: list[str]) -> str:
    """Rewrite texture filename values in upstream MaterialX XML to
    point at the local PNGs in ``tex_dir``.

    Matches ``value="...png|jpg|jpeg|tif|tiff|exr"`` anywhere in the XML
    and rewrites if the stem matches a known channel name (case-insensitive,
    ignoring ``_``/``-``/`` ``).
    """
    import re

    def _rewrite(match: re.Match) -> str:
        orig = match.group(1)
        stem = Path(orig).stem.lower().replace(" ", "").replace("-", "").replace("_", "")
        for pattern, channel in _FILENAME_TO_CHANNEL.items():
            clean_pattern = pattern.replace("_", "")
            if clean_pattern in stem and channel in channels:
                return f'value="{tex_dir / (channel + ".png")}"'
        for pattern, channel in _FILENAME_TO_CHANNEL.items():
            clean_pattern = pattern.replace("_", "")
            if clean_pattern in stem:
                local = tex_dir / f"{channel}.png"
                if local.exists():
                    return f'value="{local}"'
        return match.group(0)

    return re.sub(
        r'value="([^"]*\.(?:png|jpg|jpeg|tif|tiff|exr))"',
        _rewrite,
        xml_str,
        flags=re.IGNORECASE,
    )


# ── CLI ─────────────────────────────────────────────────────────


def _parse_range(s: str) -> tuple[float, float]:
    """Parse 'lo:hi' into a (lo, hi) tuple."""
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected lo:hi, got {s!r}")
    return float(parts[0]), float(parts[1])


def main():
    import argparse

    parser = argparse.ArgumentParser(prog="mat-vis-client", description="mat-vis texture client")
    parser.add_argument("--tag", help="Release tag (default: latest)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List sources x tiers")

    p_mat = sub.add_parser("materials", help="List materials for a source x tier")
    p_mat.add_argument("source")
    p_mat.add_argument("tier", nargs="?", default="1k")

    p_fetch = sub.add_parser("fetch", help="Fetch a texture PNG")
    p_fetch.add_argument("source")
    p_fetch.add_argument("material")
    p_fetch.add_argument("channel")
    p_fetch.add_argument("tier", nargs="?", default="1k")
    p_fetch.add_argument("-o", "--output", help="Output file (default: stdout)")

    p_search = sub.add_parser("search", help="Search materials by category / scalars")
    p_search.add_argument("category", nargs="?", help="Category filter (e.g. metal, wood)")
    p_search.add_argument("--source", help="Limit to one source")
    p_search.add_argument("--tier", default="1k")
    p_search.add_argument("--roughness", help="Roughness range as lo:hi")
    p_search.add_argument("--metalness", help="Metalness range as lo:hi")

    p_prefetch = sub.add_parser("prefetch", help="Bulk download all materials for source x tier")
    p_prefetch.add_argument("source")
    p_prefetch.add_argument("tier", nargs="?", default="1k")

    p_cache = sub.add_parser("cache", help="Manage the local cache")
    p_cache_sub = p_cache.add_subparsers(dest="cache_cmd", required=True)
    p_cache_sub.add_parser("status", help="Show cache size breakdown")
    p_cache_sub.add_parser("clear", help="Delete all cached data")
    p_prune = p_cache_sub.add_parser("prune", help="Delete subsets of the cache")
    p_prune.add_argument("--source", help="Limit to one source")
    p_prune.add_argument("--tier", help="Limit to one tier")
    p_prune.add_argument("--tag", help="Drop a specific release tag's metadata")
    p_prune.add_argument(
        "--keep-tags",
        help="Comma-separated tags to keep (drops everything else's metadata)",
    )

    p_upd = sub.add_parser("check-updates", help="Check for newer data + client")
    p_upd.add_argument("--force", action="store_true", help="Ignore 24h cache")

    args = parser.parse_args()
    client = MatVisClient(tag=args.tag)

    if args.cmd == "list":
        for tier in client.tiers():
            sources = client.sources(tier)
            print(f"{tier}: {', '.join(sources)}")

    elif args.cmd == "materials":
        for mid in client.materials(args.source, args.tier):
            print(mid)

    elif args.cmd == "fetch":
        data = client.fetch_texture(args.source, args.material, args.channel, args.tier)
        if args.output:
            Path(args.output).write_bytes(data)
            print(f"Wrote {args.output} ({len(data):,} bytes)", file=sys.stderr)
        else:
            sys.stdout.buffer.write(data)

    elif args.cmd == "search":
        roughness = _parse_range(args.roughness) if args.roughness else None
        metalness = _parse_range(args.metalness) if args.metalness else None
        results = client.search(
            args.category,
            roughness_range=roughness,
            metalness_range=metalness,
            source=args.source,
            tier=args.tier,
        )
        for entry in results:
            mv = entry.get("mat_vis") or {}
            pbr = mv.get("pbr") or {}
            scalars = []
            if pbr.get("roughness") is not None:
                scalars.append(f"R={pbr['roughness']:.2f}")
            if pbr.get("metalness") is not None:
                scalars.append(f"M={pbr['metalness']:.2f}")
            scalar_str = f" ({', '.join(scalars)})" if scalars else ""
            print(f"{entry['source']}/{entry['id']}  [{mv.get('category', '?')}]{scalar_str}")
        print(f"\n{len(results)} result(s)", file=sys.stderr)

    elif args.cmd == "prefetch":

        def _progress(mid: str, i: int, total: int) -> None:
            print(f"[{i}/{total}] {mid}", file=sys.stderr)

        n = client.prefetch(args.source, args.tier, on_progress=_progress)
        print(f"Prefetched {n} materials", file=sys.stderr)

    elif args.cmd == "cache":
        if args.cache_cmd == "status":
            status = client.cache_status()
            cap = DEFAULT_CACHE_MAX_BYTES
            total = status.get("_total", {}).get("bytes", 0)
            print(f"Cache directory: {client._cache_dir}")
            print(
                f"Total: {_fmt_size(total)} "
                f"({status.get('_total', {}).get('files', 0)} files), "
                f"soft cap: {_fmt_size(cap) if cap > 0 else 'disabled'}"
            )
            print()
            print(f"  {'KEY':30s}  {'SIZE':>10s}  {'FILES':>8s}")
            for key in sorted(status.keys()):
                if key.startswith("_"):
                    continue
                s = status[key]
                print(f"  {key:30s}  {_fmt_size(s['bytes']):>10s}  {s['files']:>8d}")
            meta = status.get("_meta", {"bytes": 0, "files": 0})
            print(f"  {'(metadata)':30s}  {_fmt_size(meta['bytes']):>10s}  {meta['files']:>8d}")
            if cap > 0 and total > cap:
                print(
                    f"\nWARNING: cache exceeds soft cap by {_fmt_size(total - cap)}.",
                    file=sys.stderr,
                )
        elif args.cache_cmd == "clear":
            freed = client.cache_clear()
            print(f"Cleared {_fmt_size(freed)}", file=sys.stderr)
        elif args.cache_cmd == "prune":
            keep_tags = args.keep_tags.split(",") if args.keep_tags else None
            freed = client.cache_prune(
                keep_tags=keep_tags,
                tag=args.tag,
                source=args.source,
                tier=args.tier,
            )
            print(f"Pruned {_fmt_size(freed)}", file=sys.stderr)

    elif args.cmd == "check-updates":
        r = client.check_updates(force=args.force)
        for kind in ("data", "client"):
            entry = r[kind]
            arrow = "→" if entry["newer_available"] else "="
            marker = " (UPDATE AVAILABLE)" if entry["newer_available"] else ""
            print(
                f"  {kind:8s}  {entry['current'] or '?'} {arrow} {entry['latest'] or '?'}{marker}"
            )


if __name__ == "__main__":
    main()
