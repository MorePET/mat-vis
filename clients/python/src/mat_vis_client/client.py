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

import json
import logging
import os
import sys
import time
import urllib.request
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

REPO = "MorePET/mat-vis"
GITHUB_RELEASES = f"https://github.com/{REPO}/releases"
GITHUB_API = f"https://api.github.com/repos/{REPO}"
GITHUB_RAW = f"https://raw.githubusercontent.com/{REPO}"
LATEST_MANIFEST_URL = f"{GITHUB_RELEASES}/latest/download/release-manifest.json"
PYPI_API = "https://pypi.org/pypi/mat-vis-client/json"
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

# Hard cap per range-read: the rowmap tells us how many bytes to pull for
# one texture, but a compromised/corrupt rowmap could claim (e.g.) 10 GB
# for a single PNG and drive the client OOM. Textures are capped in the
# baker well below this — 500 MB is ~10× the largest legitimate 8k PNG.
# Override with MAT_VIS_MAX_FETCH_SIZE if you really need more.
DEFAULT_MAX_FETCH_BYTES = _parse_size(os.environ.get("MAT_VIS_MAX_FETCH_SIZE", "500MB"))

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


def _in_range(value: float | None, lo: float, hi: float) -> bool:
    """Check if a value falls within [lo, hi]. None values never match."""
    if value is None:
        return False
    return lo <= value <= hi


# Schema versions this client understands. Manifest declares its own
# schema_version field; if the manifest version is outside this set,
# the client refuses to operate rather than silently misreading data.
COMPATIBLE_SCHEMA_VERSIONS = frozenset([1])


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
        self._rowmaps: dict[str, dict] = {}
        self._indexes: dict[str, list[dict]] = {}
        # Cached alternate clients keyed by tag (populated by .at()).
        # Each shares this instance's cache_dir + cache flag so all tag
        # scopes resolve under one root.
        self._alt_clients: dict[str, MatVisClient] = {}
        # In-memory cache of resolved redirect URLs (github.com -> signed CDN).
        # GitHub's signed URLs expire ~5 min; we cache for 4 min to be safe.
        # Avoids hitting the rate-limited github.com redirect on every
        # range read to the same parquet.
        self._redirect_cache: dict[str, tuple[str, float]] = {}
        self._tag = tag

        if manifest_url:
            self._manifest_url = manifest_url
        elif tag:
            self._manifest_url = f"{GITHUB_RELEASES}/download/{tag}/release-manifest.json"
        else:
            self._manifest_url = LATEST_MANIFEST_URL

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

    @property
    def manifest(self) -> dict:
        """Fetch and cache the release manifest. Validates schema_version."""
        if self._manifest is None:
            cache_path = self._cache_scope / ".manifest.json"
            cached = self._cache_read_text(cache_path)
            if cached is not None:
                self._manifest = json.loads(cached)
            else:
                self._manifest = _get_json(self._manifest_url)
                self._cache_write_text(cache_path, json.dumps(self._manifest, indent=2))
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

    def sources(self, tier: str = "1k") -> list[str]:
        """List available sources for a tier."""
        tier_data = self.manifest.get("tiers", {}).get(tier, {})
        return list(tier_data.get("sources", {}).keys())

    def tiers(self) -> list[str]:
        """List available tiers (discovered from the manifest)."""
        return list(self.manifest.get("tiers", {}).keys())

    def categories(self) -> tuple[str, ...]:
        """Discover material categories from the current release manifest.

        Derived from rowmap filenames (``{source}-{tier}-{category}-rowmap.json``).
        Always reflects the actual release — no hardcoded list to drift.
        """
        import re

        global CATEGORIES
        # Parse categories out of rowmap filenames across all tiers x sources.
        # Single regex covers simple and chunked names: ...-{cat}[-N]-rowmap.json
        pat = re.compile(r"-(?P<cat>[a-z]+)(?:-\d+)?-rowmap\.json$")
        found: set[str] = set()
        for tier_data in self.manifest.get("tiers", {}).values():
            for src_data in tier_data.get("sources", {}).values():
                for rm in src_data.get("rowmap_files", []):
                    m = pat.search(rm)
                    if m:
                        found.add(m.group("cat"))
                single = src_data.get("rowmap_file")
                if single:
                    m = pat.search(single)
                    if m:
                        found.add(m.group("cat"))
        result = tuple(sorted(found))
        # Populate the module-level constant for back-compat
        CATEGORIES = frozenset(result)
        return result

    def rowmap(self, source: str, tier: str, category: str | None = None) -> dict:
        """Fetch and cache rowmaps. Merges partitioned rowmaps into one."""
        key = f"{source}-{tier}-{category or 'all'}"
        if key not in self._rowmaps:
            tiers = self.manifest.get("tiers", {})
            tier_data = _lookup(tiers, tier, kind="tier")
            base_url = tier_data["base_url"]
            src_data = _lookup(
                tier_data.get("sources", {}), source, kind="source", context=f"tier {tier!r}"
            )

            rowmap_files = src_data.get("rowmap_files", [])
            if not rowmap_files:
                rowmap_file = src_data.get("rowmap_file", f"{source}-{tier}-rowmap.json")
                rowmap_files = [rowmap_file]

            if category:
                rowmap_files = [f for f in rowmap_files if category in f] or rowmap_files[:1]

            # Fetch all partition rowmaps and merge materials
            merged: dict = {"materials": {}}
            for rmf in rowmap_files:
                cache_path = self._cache_scope / ".rowmaps" / rmf
                cached = self._cache_read_text(cache_path)
                if cached is not None:
                    rm = json.loads(cached)
                else:
                    url = base_url + rmf
                    rm = _get_json(url)
                    self._cache_write_text(cache_path, json.dumps(rm, indent=2))

                # Each partitioned rowmap has its own parquet_file
                pq_file = rm.get("parquet_file", "")
                for mid, channels in rm.get("materials", {}).items():
                    # Tag each channel with its parquet file for range reads
                    for ch_data in channels.values():
                        ch_data["parquet_file"] = pq_file
                    merged["materials"][mid] = channels

                # Keep metadata from last rowmap (they're all the same except materials)
                for k in ("version", "release_tag", "source", "tier"):
                    if k in rm:
                        merged[k] = rm[k]

            self._rowmaps[key] = merged

        return self._rowmaps[key]

    def materials(self, source: str, tier: str) -> list[str]:
        """List material IDs available for a source × tier."""
        rm = self.rowmap(source, tier)
        return sorted(rm.get("materials", {}).keys())

    def channels(self, source: str, material_id: str, tier: str) -> list[str]:
        """List channels available for a material."""
        rm = self.rowmap(source, tier)
        mat = rm.get("materials", {}).get(material_id, {})
        return sorted(mat.keys())

    # ── Index & search ──────────────────────────────────────────

    def _index_url(self, source: str) -> str:
        """Build the URL for a source's index JSON."""
        ref = self._tag or "main"
        return f"{GITHUB_RAW}/{ref}/index/{source}.json"

    def index(self, source: str) -> list[dict]:
        """Fetch and cache the material index for a source.

        Tries git (raw.githubusercontent.com) first, falls back to
        release asset (some sources only ship the index on the release).
        Returns a list of material entries per index-schema.json.
        """
        if source not in self._indexes:
            cache_path = self._cache_scope / ".indexes" / f"{source}.json"
            cached = self._cache_read_text(cache_path)
            if cached is not None:
                self._indexes[source] = json.loads(cached)
            else:
                # Try git first, then fall back to release asset
                data = None
                try:
                    data = _get_json(self._index_url(source))
                except Exception:
                    tag = self.manifest.get("release_tag", self._tag or "")
                    if tag:
                        try:
                            data = _get_json(f"{GITHUB_RELEASES}/download/{tag}/{source}.json")
                        except Exception:
                            pass
                if data is None:
                    raise FileNotFoundError(f"Index for {source!r} not found in git or release")
                self._indexes[source] = data
                self._cache_write_text(cache_path, json.dumps(self._indexes[source], indent=2))
        return self._indexes[source]

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
                if category and entry.get("category") != category:
                    continue
                if roughness_range and not _in_range(entry.get("roughness"), *roughness_range):
                    continue
                if metalness_range and not _in_range(entry.get("metalness"), *metalness_range):
                    continue
                if tier not in entry.get("available_tiers", []):
                    continue
                results.append(entry)

        if score and (roughness is not None or metalness is not None):
            for r in results:
                s = 0.0
                if roughness is not None and r.get("roughness") is not None:
                    s += abs(r["roughness"] - roughness)
                if metalness is not None and r.get("metalness") is not None:
                    s += abs(r["metalness"] - metalness)
                r["score"] = s
            results.sort(key=lambda r: r["score"])

        if limit is not None:
            results = results[:limit]
        return results

    # ── Bulk operations ─────────────────────────────────────────

    def fetch_all_textures(
        self,
        source: str,
        material_id: str,
        tier: str = "1k",
        *,
        tag: str | None = None,
    ) -> dict[str, bytes]:
        """Fetch all texture channels for a material.

        Returns a dict mapping channel name to PNG bytes.
        """
        if tag is not None and tag != self._tag:
            return self.at(tag).fetch_all_textures(source, material_id, tier)
        chs = self.channels(source, material_id, tier)
        return {ch: self.fetch_texture(source, material_id, ch, tier) for ch in chs}

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

    def _scalars_for(self, source: str, material_id: str) -> dict:
        """Look up PBR scalars for a material from the source index.

        Silent on failure — returns ``{}`` if the index is unavailable or
        the material isn't found. Used by :class:`MtlxSource` to fill in
        shader scalar inputs when a texture channel is absent.
        """
        scalars: dict = {}
        try:
            for entry in self.index(source):
                if entry["id"] == material_id:
                    for k in ("roughness", "metalness", "ior", "color_hex"):
                        if k in entry and entry[k] is not None:
                            scalars[k] = entry[k]
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
            tag = self.manifest.get("release_tag", self._tag or "")
            url = f"{GITHUB_RELEASES}/download/{tag}/{source}-mtlx.json"
            try:
                self._mtlx_originals[source] = _get_json(url)
            except Exception:
                self._mtlx_originals[source] = {}
        return self._mtlx_originals[source]

    # ── Deprecated mtlx shims ──────────────────────────────────

    def rowmap_entry(
        self,
        source: str,
        material_id: str,
        tier: str = "1k",
    ) -> dict[str, dict]:
        """Get raw rowmap offsets for a material (for DIY consumers).

        Returns a dict of channel -> {offset, length, parquet_file}.
        """
        rm = self.rowmap(source, tier)
        mat = _lookup(
            rm.get("materials", {}),
            material_id,
            kind="material",
            context=f"{source}/{tier}",
        )
        fallback_pq = rm.get("parquet_file", "")
        return {
            ch: {
                "offset": info["offset"],
                "length": info["length"],
                "parquet_file": info.get("parquet_file", fallback_pq),
            }
            for ch, info in mat.items()
        }

    def _resolved_url(self, url: str) -> tuple[str, bool]:
        """Return (url_to_use, is_cached). Resolves github.com releases URL to
        its signed CDN URL and caches for 4 min. Used to amortize the
        rate-limited redirect across many range reads on the same parquet.
        """
        now = time.time()
        cached = self._redirect_cache.get(url)
        if cached and cached[1] > now:
            return cached[0], True
        return url, False

    def _cache_resolved(self, original_url: str, resolved_url: str) -> None:
        """Store a resolved CDN URL with a 4-minute TTL."""
        if resolved_url and resolved_url != original_url:
            self._redirect_cache[original_url] = (resolved_url, time.time() + 240)

    def fetch_texture(
        self,
        source: str,
        material_id: str,
        channel: str,
        tier: str = "1k",
        *,
        tag: str | None = None,
    ) -> bytes:
        """Fetch a single texture PNG via HTTP range read.

        Returns raw PNG bytes. Caches locally under the active tag scope
        (so releases don't collide). Pass ``cache=False`` at client
        construction to opt out entirely.

        Pass ``tag="v..."`` to fetch from a specific release without
        reinstantiating the client (hf-hub ``revision=`` pattern).
        """
        if tag is not None and tag != self._tag:
            return self.at(tag).fetch_texture(source, material_id, channel, tier)
        # Check cache first (tag-scoped)
        cache_path = self._cache_scope / source / tier / material_id / f"{channel}.png"
        cached = self._cache_read_bytes(cache_path)
        if cached is not None:
            return cached

        # Find in rowmap
        rm = self.rowmap(source, tier)
        mat = _lookup(
            rm.get("materials", {}),
            material_id,
            kind="material",
            context=f"{source}/{tier}",
        )
        rng = _lookup(
            mat,
            channel,
            kind="channel",
            context=f"{source}/{tier}/{material_id}",
        )
        offset = rng["offset"]
        length = rng["length"]

        # Defend against malicious/corrupt rowmaps claiming huge reads.
        # A bad rowmap could otherwise allocate gigabytes for one PNG
        # and OOM the client; see 0.3.1 security review.
        if not isinstance(length, int) or length <= 0:
            raise MatVisError(
                f"invalid rowmap entry for {source}/{material_id}/{channel}: length={length!r}"
            )
        if length > DEFAULT_MAX_FETCH_BYTES:
            raise MatVisError(
                f"rowmap claims {_fmt_size(length)} for {source}/{material_id}/{channel}, "
                f"over the {_fmt_size(DEFAULT_MAX_FETCH_BYTES)} safety cap. "
                "Raise MAT_VIS_MAX_FETCH_SIZE to override."
            )

        # Find parquet URL (per-partition from merged rowmap)
        tier_data = self.manifest["tiers"][tier]
        base_url = tier_data["base_url"]
        parquet_file = rng.get("parquet_file") or rm.get("parquet_file", "")
        original_url = base_url + parquet_file

        # Use cached resolved (signed CDN) URL if available — avoids the
        # rate-limited github.com redirect on repeat range reads.
        url, is_cached = self._resolved_url(original_url)
        range_header = f"bytes={offset}-{offset + length - 1}"

        try:
            if is_cached:
                data = _get(url, headers={"Range": range_header})
            else:
                data, resolved = _get(url, headers={"Range": range_header}, return_final_url=True)
                self._cache_resolved(original_url, resolved)
        except HTTPFetchError as e:
            # Signed URL may have expired between cache and use — retry once with fresh.
            if is_cached and e.code in (403, 404):
                self._redirect_cache.pop(original_url, None)
                data, resolved = _get(
                    original_url,
                    headers={"Range": range_header},
                    return_final_url=True,
                )
                self._cache_resolved(original_url, resolved)
            else:
                raise

        # Verify PNG
        if data[:4] != b"\x89PNG":
            raise ValueError(f"Expected PNG, got {data[:4]!r}")

        # Cache (tag-scoped; no-op if cache=False)
        self._cache_write_bytes(cache_path, data)

        # Soft-cap warning (once per process)
        self._maybe_warn_cache_cap()

        return data

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

            # Manifest is current-tag only — drop if not in keep_tags
            mf = self._cache_dir / ".manifest.json"
            if mf.exists() and (keep_set or tag):
                try:
                    mtag = json.loads(mf.read_text()).get("release_tag")
                except Exception:
                    mtag = None
                if (tag and mtag == tag) or (keep_set and mtag and mtag not in keep_set):
                    mf.unlink(missing_ok=True)

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
            scalars = []
            if entry.get("roughness") is not None:
                scalars.append(f"R={entry['roughness']:.2f}")
            if entry.get("metalness") is not None:
                scalars.append(f"M={entry['metalness']:.2f}")
            scalar_str = f" ({', '.join(scalars)})" if scalars else ""
            print(f"{entry['source']}/{entry['id']}  [{entry.get('category', '?')}]{scalar_str}")
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
