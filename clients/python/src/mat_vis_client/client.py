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
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

from mat_vis_client.match import Match
from mat_vis_client.progress import ClientEvent

if TYPE_CHECKING:
    from mat_vis_client.progress import EventKind, OnEvent

REPO = "MorePET/mat-vis"
GITHUB_API = f"https://api.github.com/repos/{REPO}"  # update-check only
PYPI_API = "https://pypi.org/pypi/mat-vis-client/json"

# v0.6.0 (ADR-0007): HF Datasets is the canonical substrate. URLs are
# built as ``{HF_BASE}/<tag>/<path>``. There is no "latest" alias on HF
# — callers must pin a revision (tag or branch).
#
# Repo + tag override precedence (highest first, mat-vis#384):
#
#   1. Constructor kwargs ``MatVisClient(repo=..., tag=...)``.
#   2. ``MAT_VIS_DATASET=<repo>@<tag>`` — combined env var, single
#      string. Last ``@`` separates repo from tag so org/repo names
#      with internal ``@`` (rare) round-trip cleanly.
#   3. ``MAT_VIS_HF_DATASET=<repo>`` (PR #388) + ``MAT_VIS_TAG=<tag>``
#      (or fall back to ``DEFAULT_TAG``) — split env-var form.
#   4. ``MAT_VIS_HF_BASE=<full-url>`` — legacy back-compat. Full
#      resolve-URL prefix; preserved verbatim so private mirrors with
#      non-HF URL shapes keep working.
#   5. Default: ``gerchowl/mat-vis`` @ ``DEFAULT_TAG``.
#
# All five layers are unit-tested in
# ``tests/test_client_repo_resolution.py``.
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


def _resolve_repo_and_tag(
    *,
    repo_kwarg: str | None,
    tag_kwarg: str | None,
) -> tuple[str, str, str | None]:
    """Resolve the effective ``(repo, tag, base_url_override)`` triple.

    Walks the precedence chain documented above (mat-vis#384). Pure
    function — no side effects, easy to unit-test.

    Returns:
        ``(repo, tag, base_url_override)``.

        - ``repo``: dataset coordinate (e.g. ``gerchowl/mat-vis``).
        - ``tag``: revision (CalVer release tag or branch).
        - ``base_url_override``: when ``MAT_VIS_HF_BASE`` is the only
          override active, the full legacy URL prefix. Callers should
          use it verbatim to compose URLs (preserves private-mirror
          shapes that don't match ``huggingface.co/datasets/<repo>``).
          ``None`` when any higher-precedence layer is in effect.
    """
    # Layer 1: constructor kwargs win outright.
    if repo_kwarg is not None and tag_kwarg is not None:
        return repo_kwarg, tag_kwarg, None

    # Layer 2: ``MAT_VIS_DATASET=repo@tag`` (combined). Last ``@``
    # splits — defensive against repo names containing ``@`` (unusual
    # but legal for branch-style refs).
    combined = os.environ.get("MAT_VIS_DATASET")
    env_repo: str | None = None
    env_tag: str | None = None
    if combined and "@" in combined:
        env_repo, env_tag = combined.rsplit("@", 1)
        env_repo = env_repo or None
        env_tag = env_tag or None

    # Layer 3: split form. ``MAT_VIS_HF_DATASET`` already exists in
    # the standalone (PR #388); use it here too. Falls back to
    # ``MAT_VIS_TAG`` then ``DEFAULT_TAG``.
    if env_repo is None:
        env_repo = os.environ.get("MAT_VIS_HF_DATASET")
    if env_tag is None:
        env_tag = os.environ.get("MAT_VIS_TAG")

    # Constructor kwargs always win over their respective env layers.
    repo = repo_kwarg or env_repo
    tag = tag_kwarg or env_tag

    # Layer 4: legacy ``MAT_VIS_HF_BASE``. Only honored when nothing
    # higher set the repo, AND the env var is actually present (not
    # just the module-level default). Pre-existing tests rely on the
    # full URL being preserved (private mirrors).
    base_override: str | None = None
    if repo is None:
        legacy_base = os.environ.get("MAT_VIS_HF_BASE")
        if legacy_base:
            base_override = legacy_base
            # Best-effort repo extraction for cache namespacing.
            # Pattern: ``https://<host>/datasets/<owner>/<name>/resolve``.
            # Falls through to the default if the URL doesn't match —
            # the override URL is still used verbatim for I/O.
            import re as _re

            m = _re.search(r"/datasets/([^/]+/[^/]+)/resolve", legacy_base)
            if m:
                repo = m.group(1)

    # Layer 5: defaults.
    if repo is None:
        repo = HF_DATASET
    if tag is None:
        tag = DEFAULT_TAG

    return repo, tag, base_override


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


def _client_cache_segment(version: str) -> str:
    """Cache subdirectory namespaced by client major.minor (mat-vis#355).

    ``"0.7.1+local"`` → ``"v0.7"``. The ``v`` prefix matches the
    CalVer tag convention and the on-disk shape consumers see when
    they ``ls ~/.cache/mat-vis/``. Patch + local versions collapse to
    the same segment so a 0.7.1 → 0.7.2 upgrade reuses the cache.
    Major bump (0.7 → 0.8) starts a fresh segment; the old one
    becomes orphan and triggers a ``cache_stale_detected`` event.
    """
    parts = version.split(".")
    if len(parts) < 2:
        return f"v{version}"
    return f"v{parts[0]}.{parts[1]}"


_CLIENT_CACHE_SEGMENT = _client_cache_segment(__version__)

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


def _rank_by_query(matches: list["Match"], query: str) -> list["Match"]:
    """Rank ``matches`` by fuzzy similarity to ``query``.

    Tokenizes ``query`` on whitespace; tokens AND-narrow (every token
    must appear in ``name`` or any ``tag``). When ``rapidfuzz`` is
    installed (``mat-vis-client[search]`` extra), ranking uses
    ``WRatio`` against ``name`` (weighted ×1.5) and the tag-joined
    string. Without rapidfuzz, falls back to token-AND substring with
    a stable id-sort within the matched set.
    """
    if not query.strip():
        return matches
    tokens = [t.casefold() for t in query.split() if t]

    def _haystack(m: "Match") -> tuple[str, str]:
        mv = m.mat_vis
        return (mv.get("name") or "").casefold(), " ".join(
            (t or "").casefold() for t in (mv.get("tags") or [])
        )

    # Token-AND prefilter: every token must appear in name OR tags.
    filtered = []
    for m in matches:
        n, tg = _haystack(m)
        if all((tok in n) or (tok in tg) for tok in tokens):
            filtered.append(m)

    try:
        from rapidfuzz import fuzz  # type: ignore[import-not-found]
    except ImportError:
        # No fuzz library — stable id-sort within matched set.
        filtered.sort(key=lambda m: m.id)
        return filtered

    # Score: max(WRatio(query, name) * 1.5, WRatio(query, tags))
    scored: list[tuple[float, "Match"]] = []
    for m in filtered:
        n, tg = _haystack(m)
        s_name = fuzz.WRatio(query, n) * 1.5 if n else 0.0
        s_tags = fuzz.WRatio(query, tg) if tg else 0.0
        scored.append((max(s_name, s_tags), m))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [m for _, m in scored]


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
    - ``candidates``: top-3 fuzzy did-you-mean suggestions (#359). Empty
      when ``available`` is small enough to render directly without
      ranking, OR when no candidate scores above the cutoff.
    - ``context``: optional path qualifier (e.g. ``"ambientcg/1k"``)
    - ``kind``: class-level label ("material", "source", ...)
    """

    kind: str = "item"

    def __init__(
        self,
        key: str,
        available: list[str] | None = None,
        context: str = "",
        candidates: list[str] | None = None,
    ) -> None:
        self.key = key
        self.available = list(available or [])
        self.context = context
        # #359: did-you-mean suggestions. Computed by callers (typically
        # via ``difflib.get_close_matches`` or ``rapidfuzz``) so the
        # exception class stays dependency-free; we just hold the list.
        # When the caller doesn't compute it, fall back to a small
        # difflib pass against ``available`` so error messages still
        # surface likely-typo hints automatically.
        if candidates is None and available:
            candidates = difflib.get_close_matches(key, list(available), n=3, cutoff=0.6)
        self.candidates = list(candidates or [])
        where = f" in {context}" if context else ""
        msg_bits = [f"{self.kind} {key!r} not found{where}"]
        if self.candidates:
            msg_bits.append(f"Did you mean: {', '.join(repr(c) for c in self.candidates)}?")
        if self.available:
            msg_bits.append(f"Available: {self.available}")
        super().__init__(". ".join(msg_bits))


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
        available: list[str] | None = None,
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
        # mat-vis#332: surface the tiers the material IS staged at so
        # users can pick a working alternative without grep'ing the
        # catalog. Replaces "Needs a re-bake" (actionable only for
        # maintainers) with concrete options.
        self.available = list(available) if available else []
        if original_name is not None and original_name != material_id:
            msg = (
                f"material {original_name!r} (resolved id {material_id!r}) "
                f"exists in {source!r} index but is not staged for "
                f"tier {tier!r}."
            )
        else:
            msg = (
                f"material {material_id!r} exists in {source!r} index "
                f"but is not staged for tier {tier!r}."
            )
        if self.available:
            msg += f" Available tiers: {self.available}."
        else:
            # No alternatives available — this material isn't staged
            # at any tier. Surface the original "needs a re-bake"
            # actionable so the message stays useful for that case.
            msg += " Needs a re-bake."
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


class NoPreviewError(MatVisError):
    """Material has no preview because the source is scalar-only.

    Raised by :attr:`VisAsset.thumb` / :meth:`VisAsset.thumb_for` for
    sources like ``physicallybased`` whose entries advertise no staged
    tiers. A "preview" requires PNG bytes from somewhere; scalar-only
    entries don't have any until mat-vis#361 ships a baked sphere
    render. Distinct from :class:`PreviewUnavailableError`, which fires
    when textures *exist* but none small enough are staged.
    """

    def __init__(self, source: str, material_id: str):
        self.source = source
        self.material_id = material_id
        super().__init__(
            f"no preview available for {source}/{material_id}: "
            f"source has no PNG textures (scalar-only entry). "
            f"Tracked by mat-vis#361 (baked sphere previews)."
        )


class PreviewUnavailableError(MatVisError):
    """No tier staged that's small enough to qualify as a preview.

    Raised by :attr:`VisAsset.thumb` / :meth:`VisAsset.thumb_for` when
    the material has textures but every staged tier is larger than the
    preview ladder (``128``/``256``/``512``/``1k``) and no dedicated
    ``thumb`` tier (mat-vis#361) is staged. Carries ``available`` —
    the tiers that ARE staged — so callers can pick one explicitly via
    :meth:`VisAsset.thumb_for(tier=...)`.
    """

    def __init__(
        self,
        source: str,
        material_id: str,
        *,
        available: list[str] | None = None,
    ):
        self.source = source
        self.material_id = material_id
        self.available = list(available) if available else []
        msg = (
            f"no preview tier staged for {source}/{material_id}. "
            f"Need one of [thumb, 128, 256, 512, 1k]; "
            f"available: {self.available or 'none'}."
        )
        if self.available:
            msg += (
                f" Use .thumb_for(tier={self.available[0]!r}) to fetch "
                f"a non-preview-sized texture explicitly."
            )
        super().__init__(msg)


@dataclass(frozen=True, slots=True)
class ThumbResult:
    """Non-raising preview result for iteration / MCP / CLI grids.

    Returned by :meth:`VisAsset.safe_thumb`. ``png`` is non-None on
    success; ``error`` + ``reason`` are populated on failure. ``channel``
    and ``tier`` record what the resolver ended up picking (useful for
    debugging fallback chains and for MCP consumers that want to surface
    "we returned the normal map because color was missing").
    """

    png: bytes | None
    error: str | None
    reason: str | None
    channel: str | None
    tier: str | None


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

    # mat-vis#374: tier-rank dict for forward-compat ``auto``/``best``
    # resolution. Maps each known tier name to a pixel-rank ordering.
    # KTX2 derived tiers carry the same rank as their PNG twin so they
    # land in the same auto/best slot; at ties we prefer PNG (the
    # ``best`` ladder explicitly enumerates PNG tiers, KTX2 callers opt
    # in via ``tier="ktx2-1k"``). ``thumb=0`` keeps it out of the
    # quality ladder (different layer: source-quality vs render-quality).
    # ``scalar=-1`` is the always-available terminal fallback for
    # ``auto`` on scalar-only materials.
    # Unknown tier names sort to ``-inf`` and are filtered out — future
    # substrates can stage new tier names without crashing old clients.
    _TIER_RANK: ClassVar[dict[str, int]] = {
        "scalar": -1,
        "thumb": 0,
        "128": 128,
        "256": 256,
        "512": 512,
        "1k": 1024,
        "ktx2-1k": 1024,
        "2k": 2048,
        "4k": 4096,
        "8k": 8192,
    }
    # Walked top-to-bottom; ``auto`` prefers larger-but-not-larger-than-1k.
    _AUTO_TIER_LADDER: ClassVar[tuple[str, ...]] = ("1k", "512", "256", "128")
    # Walked top-to-bottom; ``best`` prefers the largest staged tier
    # with no scalar fallback (archival contract).
    _BEST_TIER_LADDER: ClassVar[tuple[str, ...]] = (
        "8k",
        "4k",
        "2k",
        "1k",
        "512",
        "256",
        "128",
    )

    def __init__(
        self,
        *,
        manifest_url: str | None = None,
        cache_dir: Path | None = None,
        tag: str | None = None,
        repo: str | None = None,
        cache: bool = True,
        on_event: "OnEvent | None" = None,
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

        # mat-vis#384: resolve the (repo, tag, base_override) triple
        # via the layered precedence chain documented on
        # ``_resolve_repo_and_tag``. ``self._tag`` keeps the resolved
        # value so cache scoping + URL composition agree even when the
        # caller relied on env vars / defaults.
        self._repo, self._tag, _base_override = _resolve_repo_and_tag(
            repo_kwarg=repo,
            tag_kwarg=tag,
        )
        # The legacy ``MAT_VIS_HF_BASE`` form lets callers point at a
        # private mirror with a non-HF URL shape. When that's the only
        # override active, preserve the full prefix verbatim — don't
        # try to recompose from ``self._repo`` (which may have been
        # parsed best-effort from the URL or fallen back to default).
        self._base = _base_override or f"https://huggingface.co/datasets/{self._repo}/resolve"
        # mat-vis#312 + #355: optional observability callback. ``None``
        # = silent default; pass a reporter from
        # ``mat_vis_client.progress`` (or write your own) to receive
        # download + cache lifecycle events. Stored as a guarded
        # invocation method (`_emit`) so call sites read cleanly and
        # consumer exceptions never break the fetch.
        self._on_event: OnEvent | None = on_event

        if manifest_url:
            self._manifest_url = manifest_url
        else:
            # v0.6.0: HF substrate only. No "latest" alias on HF — the
            # client picks a sensible default release (DEFAULT_TAG) so
            # out-of-the-box use returns real data instead of the empty
            # `main` baseline (#242). Explicit ``tag=...`` overrides.
            self._manifest_url = f"{self._base}/{self._tag}/release-manifest.json"

        # mat-vis#355: detect orphan cache layouts on first init and
        # emit a single CacheStaleEvent. Cheap (one listdir + str
        # check); no I/O for the user. Default reporter is silent so
        # this only surfaces when the consumer wired tty_reporter() /
        # log_reporter() / mcp_reporter().
        self._emit_legacy_layout_warning_if_any()

    def _emit(
        self,
        kind: "EventKind",
        *,
        source: str | None = None,
        material: str | None = None,
        channel: str | None = None,
        tier: str | None = None,
        url: str | None = None,
        bytes_total: int | None = None,
        bytes_done: int | None = None,
        detail: dict | None = None,
    ) -> None:
        """Dispatch a :class:`ClientEvent` through ``self._on_event``.

        No-op when ``on_event`` was not supplied. Consumer exceptions
        are caught + dropped — observability MUST NOT break the fetch
        path. mat-vis#312 + #355.
        """
        if self._on_event is None:
            return
        try:
            self._on_event(
                ClientEvent(
                    kind=kind,
                    source=source,
                    material=material,
                    channel=channel,
                    tier=tier,
                    url=url,
                    bytes_total=bytes_total,
                    bytes_done=bytes_done,
                    tag=self._tag,
                    detail=detail or {},
                )
            )
        except Exception:  # noqa: BLE001 — observability MUST NOT break fetches
            pass

    def _emit_legacy_layout_warning_if_any(self) -> None:
        """One-shot scan for orphan cache layouts at init.

        mat-vis#355: clients that upgrade across the version-namespace
        cutover leave a stale ``latest/`` or older ``v0.X/`` directory
        under ``cache_dir``. Detect cheaply and surface the finding
        through TWO channels:

        - ``cache_stale_detected`` event via ``on_event`` for wired
          reporters (tty / mcp / log / custom).
        - **``log.warning`` line** for silent-default consumers — the
          root logger sits at WARNING by default so this reaches
          anyone who doesn't actively suppress warnings, including
          consumers reaching mat-vis through pymat's singleton
          (where ``on_event`` is wired but the log surface is
          additive coverage).

        Why both: post-#358 reviewer caught that an ``on_event``-only
        path leaves silent-default consumers with no signal. Stale
        cache silently bloats their disk; one WARN line per process
        at startup is the right cost / clarity ratio.
        """
        if not self._cache:
            return
        try:
            if not self._cache_dir.is_dir():
                return
            current_segment = _CLIENT_CACHE_SEGMENT
            orphans: list[str] = []
            for entry in self._cache_dir.iterdir():
                if not entry.is_dir():
                    continue
                name = entry.name
                if name == current_segment:
                    continue
                # Treat anything that looks like a version segment OR
                # the legacy "latest" / a tag dir at root as orphan.
                if name == "latest" or name.startswith("v") or "." in name:
                    orphans.append(name)
            if not orphans:
                return
            total = 0
            for n in orphans:
                for path in (self._cache_dir / n).rglob("*"):
                    if path.is_file():
                        try:
                            total += path.stat().st_size
                        except OSError:
                            pass
            # Always log at WARNING so silent consumers see one line.
            log.warning(
                "mat-vis cache: %d legacy layout(s) at %s (~%s); "
                "run `python -m mat_vis_client cache clear --stale-only` to reclaim.",
                len(orphans),
                self._cache_dir,
                _fmt_size(total),
            )
            # Also emit through the event channel for wired reporters.
            self._emit(
                "cache_stale_detected",
                detail={"layouts": sorted(orphans), "bytes": total},
            )
        except Exception:  # noqa: BLE001 — best-effort; never break init
            pass

    @property
    def _cache_scope(self) -> Path:
        """Version-namespaced + repo-scoped + tag-scoped cache subdirectory.

        Layout (mat-vis#355 + mat-vis#384):
        ``<cache_dir>/<client-version>/<repo-slug>/<tag>/...``.

        The client-version segment ensures upgrades across major.minor
        boundaries never read through a stale layout (the bug surfaced
        twice; see #281, #283 retraction). The repo-slug segment
        (added mat-vis#384) keeps per-dataset data isolated so a
        ``MatVisClient(repo="gerchowl/mat-vis-tst", tag="v1")`` cache
        never serves bytes for a default-repo ``tag="v1"`` request.
        The tag segment keeps per-release data isolated likewise.

        For the default repo (``gerchowl/mat-vis``) the layout
        appearing under ``<client-version>/`` is
        ``gerchowl__mat-vis/<tag>``. Pre-#384 default-repo caches
        (``<client-version>/<tag>/...``) become one-shot orphans and
        get reaped by the standard
        :meth:`_emit_legacy_layout_warning_if_any` path.
        """
        return (
            self._cache_dir
            / _CLIENT_CACHE_SEGMENT
            / self._repo.replace("/", "__")
            / (self._tag or DEFAULT_TAG)
        )

    def at(self, tag: str) -> "MatVisClient":
        """Return a client pinned to ``tag``, sharing this one's cache.

        Cheap lazy alternate: reuses the parent's ``cache_dir``,
        ``cache`` flag, and ``on_event`` callback so every tag lives
        under a common root, tag-scoped cache paths stay coherent, and
        observability composes across tag-pinned operations.
        Subclients are memoized.
        """
        if tag == self._tag:
            return self
        if tag not in self._alt_clients:
            self._alt_clients[tag] = MatVisClient(
                cache_dir=self._cache_dir,
                tag=tag,
                # mat-vis#384: forward the resolved repo so
                # ``client.at("v...")`` keeps routing to the same
                # dataset even when the parent was constructed via
                # env-var overrides.
                repo=self._repo,
                cache=self._cache,
                on_event=self._on_event,
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

    def _cache_read_etag_pair(self, body_path: Path) -> tuple[str | None, str | None]:
        """Read a body file + its sibling .etag file. Returns
        ``(body, etag)`` where either may be ``None``.

        Per #258 (manifest) + #355 (per-index): on-disk body files are
        paired with sibling ``<name>.etag`` files so a conditional GET
        can validate against the remote without refetching the body.
        Bare body without ``.etag`` is treated as etag-less (forces an
        unconditional GET next lifecycle).

        ``body_path`` is the body file's full path; the ETag lives at
        the same path with ``.etag`` substituted for the body suffix
        (e.g. ``.indexes/gpuopen.json`` ↔ ``.indexes/gpuopen.json.etag``).
        Path-suffix substitution rather than name-based so callers
        nested under ``_cache_scope`` keep their layout.
        """
        body = self._cache_read_text(body_path)
        if body is None:
            return None, None
        etag = self._cache_read_text(body_path.with_suffix(".etag"))
        return body, (etag or None)

    def _cache_write_etag_pair(self, body_path: Path, body: str | bytes, etag: str | None) -> None:
        """Persist a body file + sibling ETag.

        ``body`` accepts bytes (raw response) or str (already-decoded);
        always stored as text. The ETag is only written when the server
        provided one — absent ``.etag`` signals "next lifecycle,
        refetch unconditionally" (defensive cold-start). Stale ETag
        from a prior lifecycle is cleared if present so we don't 304
        against a body we no longer have.
        """
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        self._cache_write_text(body_path, body)
        etag_path = body_path.with_suffix(".etag")
        if etag:
            self._cache_write_text(etag_path, etag)
        elif etag_path.exists():
            try:
                etag_path.unlink()
            except OSError:
                pass

    def _cache_read_manifest_with_etag(self) -> tuple[str | None, str | None]:
        """Manifest-specific wrapper around :meth:`_cache_read_etag_pair`.
        Kept as a thin alias so the surface in the manifest property
        stays readable. Same contract, fixed path."""
        return self._cache_read_etag_pair(self._cache_scope / ".manifest.json")

    def _cache_write_manifest(self, body: str | bytes, etag: str | None) -> None:
        """Manifest-specific wrapper around :meth:`_cache_write_etag_pair`."""
        self._cache_write_etag_pair(self._cache_scope / ".manifest.json", body, etag)

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
                self._emit("etag_not_modified", url=self._manifest_url)
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
        # mat-vis#384: composes from ``self._base`` (resolved at init
        # via ``_resolve_repo_and_tag``) so a constructor ``repo=``
        # kwarg or ``MAT_VIS_DATASET=repo@tag`` env var routes every
        # URL through the chosen dataset, not just the manifest.
        return f"{self._base}/{self._revision()}/{path}"

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
        ``materials("physicallybased", "scalar")``. The mat-vis#281
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

        Cache strategy (mat-vis#355): ETag-validated like the manifest
        (#258). One conditional GET per client lifecycle per source.
        Server responds 304 if the index hasn't moved (immutable on a
        pinned tag) and we serve the cached body. Pre-#355 this path
        bypassed ETag entirely, which is why the two false-report
        cycles (mat-vis#281/#283) surfaced as cache staleness — the index
        cache had no invalidation hook beyond manual ``rm -rf``.

        Guards the v2/v3 boundary: a v3 client pointed at a v2 catalog (e.g.
        a user who pinned ``tag="v2026.04.0"`` before rebaking) would silently
        return empty ``search()`` / ``categories()`` because every ``mat_vis``
        lookup misses. Fail loudly instead (ADR-0011 / mat-vis#152).
        """
        if source not in self._indexes:
            cache_path = self._cache_scope / ".indexes" / f"{source}.json"
            cached_body, cached_etag = self._cache_read_etag_pair(cache_path)
            url = self._index_url(source)
            # mat-vis#355: ETag-validated path — taken when we have a
            # cached etag (warm cache). Cold start (cached_etag is None)
            # routes through `_get_json`, preserving the pre-#355 fetch
            # surface that tests mock heavily and avoiding a 16-test-
            # file rewrite. The warm path is what the mat-vis#281/#283
            # cycles needed; the cold path is unchanged behavior.
            if cached_etag is not None:
                body, new_etag = _get_with_etag(url, etag=cached_etag)
                if body is None:
                    # 304 Not Modified — cached body is authoritative.
                    assert cached_body is not None
                    self._indexes[source] = json.loads(cached_body)
                    self._emit("etag_not_modified", source=source, url=url)
                else:
                    body_text = body.decode("utf-8") if isinstance(body, bytes) else body
                    self._indexes[source] = json.loads(body_text)
                    self._cache_write_etag_pair(cache_path, body_text, new_etag)
            else:
                # Cold-start: use the legacy `_get_json` surface so
                # existing tests + cold paths in production behave
                # identically. We don't get an ETag this way (it'd
                # require a HEAD probe; not worth the round-trip), so
                # the warm-cache validation kicks in only after the
                # second client lifecycle when an ETag is available.
                # On a fresh process with NO cache, the next
                # mat-vis#281/#283-class staleness incident still
                # requires `cache clear`
                # — but only ONCE. Subsequent fetches ETag-validate.
                data = _get_json(url)
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

    def index(self, source: str) -> list[Match]:
        """Fetch and cache the per-source catalog JSON. Returns ``list[Match]``.

        v0.6.0: resolves to ``<HF_BASE>/<revision>/<source>.json`` via
        the manifest. No GH-Raw fallback — the catalog lives in the
        same dataset revision as everything else (ADR-0007).

        The ``upstream`` block (Layer 2 of ADR-0011) is stripped from
        every entry — it's the verbatim upstream response, intentionally
        NOT part of the stable query surface. Use :meth:`upstream` to
        access it for a specific material.

        #359: returns ``list[Match]`` (dict-subclass) for shape parity
        with :meth:`search`. ``isinstance(m, dict)`` stays True so all
        existing key-access patterns keep working.
        """
        return [Match(self._strip_upstream(e)) for e in self._load_index_raw(source)]

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
        # text + structural filters (#359)
        query: str | None = None,
        name: str | None = None,
        tag: str | None = None,
        is_conductor: bool | None = None,
        has_map: str | None = None,
        transmission_range: tuple[float, float] | None = None,
        dispersion_range: tuple[float, float] | None = None,
        # scalar filters (existing)
        roughness: float | None = None,
        metalness: float | None = None,
        roughness_range: tuple[float, float] | None = None,
        metalness_range: tuple[float, float] | None = None,
        # scoping
        source: str | None = None,
        tier: str = "1k",
        release: str | None = None,
        # tuning
        distance: bool = False,
        limit: int | None = None,
    ) -> list[Match]:
        """Discover materials by text + filters. Returns ``list[Match]``.

        The single discovery verb on the client (#359). With no ``query=``,
        results are sorted by id; with ``query=``, ranked by fuzzy
        similarity (rapidfuzz when ``mat-vis-client[search]`` is installed,
        token-AND substring fallback otherwise). Structural filters
        AND-narrow the candidate set first; ``query=`` ranks within.

        Args:
            category: Filter by material category (e.g. "metal", "wood").
            query: Free-text fuzzy match across (name, tags). Tokens
                AND-narrow; ranking uses rapidfuzz.WRatio with name >
                tag weighting when the ``[search]`` extra is present.
            name: Substring on ``mat_vis.name`` (case-insensitive).
            tag: Substring on any tag in ``mat_vis.tags``.
            is_conductor: Filter by Phase 1 procedural-PBR conductor
                stamp (#316). Useful to discriminate procedural-walker
                metals from convention metals.
            has_map: Material has this map name in ``maps[]``
                (e.g. ``"opacity"``, ``"displacement"``, ``"normal"``).
            transmission_range: (min, max) on ``pbr.transmission`` (#340).
            dispersion_range: (min, max) on ``pbr.dispersion`` (#340).
            roughness: Scalar shorthand. Matches within ± ``_SCALAR_WIDEN``.
                Mutually exclusive with ``roughness_range``.
            metalness: Scalar shorthand. Same semantics as ``roughness``.
            roughness_range: (min, max) roughness filter, inclusive.
            metalness_range: (min, max) metalness filter, inclusive.
            source: Limit search to one source. If None, searches all
                    sources available for the given tier.
            tier: Only return materials that have this tier available.
            release: Optional release tag override (see :meth:`.at`).
                Renamed from ``tag=`` (#359, which now means material-tag).
            distance: When True and a scalar shorthand is passed, attach
                a ``"distance"`` field (absolute scalar distance) and
                sort ascending. Renamed from ``score=`` (#359).
            limit: Cap the returned list length.
        """
        if release is not None and release != self._tag:
            return self.at(release).search(
                category,
                query=query,
                name=name,
                tag=tag,
                is_conductor=is_conductor,
                has_map=has_map,
                transmission_range=transmission_range,
                dispersion_range=dispersion_range,
                roughness=roughness,
                metalness=metalness,
                roughness_range=roughness_range,
                metalness_range=metalness_range,
                source=source,
                tier=tier,
                distance=distance,
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
        name_q = name.casefold() if name else None
        tag_q = tag.casefold() if tag else None
        results: list[Match] = []

        for src in sources:
            for entry in self.index(src):
                mv = entry.get("mat_vis") or {}
                pbr = mv.get("pbr") or {}
                if category and mv.get("category") != category:
                    continue
                # name= substring (case-insensitive on mat_vis.name)
                if name_q and name_q not in (mv.get("name") or "").casefold():
                    continue
                # tag= substring on any tag
                if tag_q is not None:
                    tags = mv.get("tags") or []
                    if not any(tag_q in (t or "").casefold() for t in tags):
                        continue
                # is_conductor= exact bool
                if is_conductor is not None and pbr.get("is_conductor") != is_conductor:
                    continue
                # has_map= membership
                if has_map is not None and has_map not in (entry.get("maps") or []):
                    continue
                # transmission_range / dispersion_range — None is excluded
                # (range filter implies "must have a value to compare")
                if transmission_range is not None and not _in_range(
                    pbr.get("transmission"), *transmission_range
                ):
                    continue
                if dispersion_range is not None and not _in_range(
                    pbr.get("dispersion"), *dispersion_range
                ):
                    continue
                if roughness_range and not _in_range(pbr.get("roughness"), *roughness_range):
                    continue
                if metalness_range and not _in_range(pbr.get("metalness"), *metalness_range):
                    continue
                # Scalar-only entries (e.g. physicallybased) advertise no
                # textures — treat missing/empty ``available_tiers`` as
                # tier-independent so they pass any tier filter (#167).
                entry_tiers = entry.get("available_tiers")
                if entry_tiers and tier not in entry_tiers:
                    continue
                results.append(Match(entry))

        # query= fuzzy text ranking. Structural filters above have already
        # AND-narrowed; this just sorts within. Pure-Python token-AND
        # substring fallback when rapidfuzz isn't installed.
        if query:
            results = _rank_by_query(results, query)
        elif distance and (roughness is not None or metalness is not None):
            for r in results:
                pbr = r.pbr
                d = 0.0
                if roughness is not None and pbr.get("roughness") is not None:
                    d += abs(pbr["roughness"] - roughness)
                if metalness is not None and pbr.get("metalness") is not None:
                    d += abs(pbr["metalness"] - metalness)
                r["distance"] = d
            results.sort(key=lambda r: r["distance"])
        else:
            # Stable id-sort default — predictable for both single-source
            # and multi-source scans.
            results.sort(key=lambda r: r.id)

        if limit is not None:
            results = results[:limit]
        return results

    # ── Bulk operations ─────────────────────────────────────────

    @staticmethod
    def _normalize_name(s: str) -> str:
        """Case/whitespace-fold a name for index lookup. NFKC + casefold."""
        import unicodedata

        return unicodedata.normalize("NFKC", s).strip().casefold()

    @staticmethod
    def _entry_matches_id_or_name(entry: dict, query: str) -> bool:
        """Three-way name-aware match for catalog lookups (mat-vis#372).

        Centralizes the asymmetry-prone substrate lookup used by
        :meth:`_scalars_for` and :class:`VisAsset._is_scalar_only_entry`:
        callers may receive a canonical UUID/slug, a normalized lowercase
        id, or a display name like ``"Aluminum Brushed"``. All three
        must match the same entry, or scalar-only sources silently lose
        scalars when callers pass display names (#367/#368).

        :meth:`_resolve_material_id` keeps its own logic — it builds
        by-id + by-name lists for ambiguity detection and raises typed
        errors, a different responsibility.
        """
        if not isinstance(entry, dict):
            return False
        eid = entry.get("id", "")
        name = (entry.get("mat_vis") or {}).get("name", "")
        nq = MatVisClient._normalize_name(query)
        return (
            eid == query
            or (eid and MatVisClient._normalize_name(eid) == nq)
            or (name and MatVisClient._normalize_name(name) == nq)
        )

    def _lookup_available_tiers(self, source: str, material_id: str) -> list[str]:
        """Return ``available_tiers`` for ``material_id`` in ``source``'s index.

        Helper for :meth:`_resolve_tier` (mat-vis#374). Best-effort —
        any failure returns ``[]`` so the auto/best resolver can route
        through its no-textures-staged path with a clean error message,
        rather than cascading the index lookup error.
        """
        try:
            idx = self._load_index_raw(source)
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(idx, list):
            return []
        for entry in idx:
            if self._entry_matches_id_or_name(entry, material_id):
                tiers = entry.get("available_tiers") or []
                return [t for t in tiers if isinstance(t, str)]
        return []

    def _resolve_tier(self, source: str, material_id: str, tier: str) -> str:
        """Collapse ``"auto"``/``"best"`` to a concrete tier (mat-vis#374).

        - ``"auto"``: scalar-precheck → ``1k`` → ``512`` → ``256`` →
          ``128``. REPL-friendly: scalar fallback is included so
          ``client.fetch_all_textures("physicallybased", "Aluminum")``
          returns ``{}`` instead of raising. Returns ``"scalar"`` if
          the material is scalar-only (texture-fetch callers
          short-circuit to ``{}``).
        - ``"best"``: ``8k`` → ``4k`` → ``2k`` → ``1k`` → ``512`` →
          ``256`` → ``128``. NO scalar fallback (archival contract).
          Raises :class:`MaterialNotStagedError` with ``available=[]``
          when no texture tier is staged.
        - Any other tier name returns unchanged — caller named it.

        Critical: this must be called *before* cache-path composition
        so the on-disk cache key is the resolved tier, never the literal
        ``"auto"``/``"best"`` (would pollute the cache under
        ``…/auto/…`` and double-download on the next explicit
        ``tier="1k"`` request).
        """
        if tier not in ("auto", "best"):
            return tier
        staged = self._lookup_available_tiers(source, material_id)
        # Filter to known tier names — unknown future tiers sort to
        # nowhere and are skipped, not crashed.
        known = [t for t in staged if t in self._TIER_RANK]

        if tier == "auto":
            # Scalar-only materials: short-circuit to "scalar" without
            # walking the texture ladder.
            if known and all(t == "scalar" for t in known):
                return "scalar"
            for candidate in self._AUTO_TIER_LADDER:
                if candidate in known:
                    return candidate
            # No texture tier in the ladder is staged. If "scalar" is
            # advertised (post-#369), fall to it; the texture-fetch
            # caller short-circuits to {} and scalars still resolve via
            # _scalars_for. If neither textures nor scalar are staged,
            # nothing is fetchable — let the downstream
            # _resolve_material_id raise MaterialNotStagedError with the
            # full available-tiers hint.
            if "scalar" in known:
                return "scalar"
            # Materials with no index entry (or empty available_tiers)
            # have no usable tier under auto. Surface the loud error
            # via _resolve_material_id by returning a tier that will
            # fail its membership check.
            raise MaterialNotStagedError(
                source=source,
                material_id=material_id,
                tier="auto",
                available=list(staged),
            )

        # tier == "best"
        for candidate in self._BEST_TIER_LADDER:
            if candidate in known:
                return candidate
        # No texture tier staged. ``best`` does NOT fall back to scalar.
        raise MaterialNotStagedError(
            source=source,
            material_id=material_id,
            tier="best",
            available=[t for t in staged if t != "scalar"],
        )

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

        Special case ``tier="scalar"`` (mat-vis#370): scalar-only sources
        (physicallybased + the 18-entry gpuopen scalar-only subset) carry
        no texture tiers — ``"scalar"`` is the convention sentinel
        (cf. pymat ``TIERS_SCALAR = ["scalar"]``), not a missing bake. We
        skip the ``available_tiers`` membership check in that case so
        every entry counts as "staged". Without this, scalar-only callers
        always raised :class:`MaterialNotStagedError`.
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
            # mat-vis#370: tier="scalar" is the scalar-only sentinel —
            # treat as always-staged so scalar-only callers don't trip
            # MaterialNotStagedError.
            if tier == "scalar":
                return True
            return tier in (entry.get("available_tiers") or [])

        if by_id is not None:
            if _is_staged(by_id):
                return material_id
            # Direct-UUID path: original_name stays None so the legacy
            # single-id message is preserved (#280). mat-vis#332:
            # surface the tiers this material IS staged at.
            raise MaterialNotStagedError(
                source=source,
                material_id=material_id,
                tier=tier,
                available=list(by_id.get("available_tiers") or []),
            )

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
            # mat-vis#332: surface the tiers this material IS staged at.
            raise MaterialNotStagedError(
                source=source,
                material_id=resolved,
                tier=tier,
                original_name=material_id,
                available=list(by_name[0].get("available_tiers") or []),
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
        tier: str = "auto",
        *,
        tag: str | None = None,
    ) -> dict[str, bytes]:
        """Fetch all texture channels for a material.

        ``material_id`` may be the canonical id (UUID/slug used as the rowmap
        key) or a human-readable name from the source's index; the latter is
        resolved via :meth:`_resolve_material_id` (mat-vis#143). Unknown ids,
        un-staged materials, and ambiguous names raise typed errors rather
        than returning ``{}`` silently (mat-vis#141 / #144).

        ``tier`` defaults to ``"auto"`` since 0.7.0 (mat-vis#374) — the
        client picks the best-quality texture tier the material has
        staged, or short-circuits to ``{}`` for scalar-only materials.
        Explicit ``tier="1k"`` callers are unaffected. Pass
        ``tier="best"`` for the highest-quality tier (no scalar
        fallback — raises :class:`MaterialNotStagedError` if no
        textures are staged).

        Returns a dict mapping channel name to PNG bytes. Empty dict
        for scalar-only materials when ``tier="auto"``.
        """
        if tag is not None and tag != self._tag:
            return self.at(tag).fetch_all_textures(source, material_id, tier)
        # mat-vis#374: collapse auto/best to a concrete tier *before*
        # cache-key composition. Without this, the on-disk cache would
        # accumulate spurious ``…/auto/…`` paths and double-download on
        # the next explicit-tier request.
        tier = self._resolve_tier(source, material_id, tier)
        if tier == "scalar":
            # Scalar-only short-circuit (mat-vis#374): no textures to
            # fetch. Callers compose with ``_scalars_for`` to get the
            # full PBR scalar dict — the asset still renders.
            return {}
        resolved = self._resolve_material_id(source, material_id, tier)
        chs = self.channels(source, resolved, tier)
        return {ch: self.fetch_texture(source, resolved, ch, tier) for ch in chs}

    def prefetch(
        self,
        source: str,
        tier: str = "auto",
        *,
        on_progress: callable | None = None,
        tag: str | None = None,
    ) -> int:
        """Bulk download all materials for a source + tier to cache.

        Args:
            source: Source name (e.g. "ambientcg").
            tier: Resolution tier (default ``"auto"`` — picks the best
                staged tier per-material). Pass ``"best"`` for highest-
                quality (raises if a material has no textures); pass an
                explicit tier name (``"1k"``, ``"512"``, ...) to fetch a
                single tier across the source. Pre-0.7.0 default was
                ``"1k"`` (mat-vis#374).
            on_progress: Optional callback(material_id, index, total).
            tag: Optional release tag override (see .at()).

        Returns the number of materials fetched.
        """
        if tag is not None and tag != self._tag:
            return self.at(tag).prefetch(source, tier, on_progress=on_progress)
        # When tier is auto/best the per-material listing has to come
        # from the full index (any material we can fetch_all_textures
        # for is in scope), not the explicit-tier ``materials(tier)``
        # filter. ``materials(tier="auto")`` would otherwise raise via
        # the manifest tier lookup.
        if tier in ("auto", "best"):
            mat_ids = [e["id"] for e in self.index(source) if isinstance(e, dict) and e.get("id")]
        else:
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
        tier: str = "auto",
        output_dir: str | Path = ".",
    ) -> Path:
        """Write all texture PNGs for a material to disk.

        ``tier`` defaults to ``"auto"`` since 0.7.0 (mat-vis#374); see
        :meth:`fetch_all_textures` for resolution semantics. Scalar-only
        materials produce an empty output directory.

        Returns the directory containing the PNG files, named by channel
        (e.g. color.png, normal.png, roughness.png).
        """
        out = Path(output_dir) / material_id
        out.mkdir(parents=True, exist_ok=True)

        # mat-vis#374: collapse auto/best up front so the channel-list
        # lookup + per-channel fetch all use the resolved tier.
        tier = self._resolve_tier(source, material_id, tier)
        if tier == "scalar":
            # Scalar-only short-circuit: nothing to write.
            return out

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
        tier: str = "auto",
        *,
        tag: str | None = None,
    ) -> MtlxSource:
        """Get a lazy :class:`MtlxSource` for a material.

        Use ``.xml`` for the document string, ``.export(path)`` to write
        files, and ``.original`` for the upstream-author variant (None
        if not available for this source).

        ``tier`` defaults to ``"auto"`` since 0.7.0 (mat-vis#374); the
        actual tier is collapsed lazily on first ``.xml``/``.export``
        access so creation stays free.

        Creation is free — no network IO happens until ``.xml`` or
        ``.export(...)`` is called. Pass ``tag=`` to scope the source to
        a specific release (see .at()).
        """
        if tag is not None and tag != self._tag:
            return self.at(tag).mtlx(source, material_id, tier)
        return MtlxSource(self, source, material_id, tier, is_original=False)

    def asset(
        self,
        ref: "Match | str | None" = None,
        material_id: str | None = None,
        tier: str | None = None,
        *,
        source: str | None = None,
        id: str | None = None,
    ) -> "VisAsset":
        """Return a :class:`VisAsset` for a material. Polymorphic dispatch (#359).

        Three input shapes:

        - ``asset(match)`` — a :class:`Match` from ``search()``/``index()``;
          identity comes from the Match (with ``tier=`` defaulted from its
          ``available_tiers`` if not given).
        - ``asset("ambientcg/Rock064")`` — a string ``"source/id"`` ref.
          Malformed refs raise ``ValueError``.
        - ``asset(source="ambientcg", id="Rock064")`` — explicit kwargs.
        - ``asset("ambientcg", "Rock064", "1k")`` — legacy 3-positional
          form (preserved so existing callers don't break).

        VisAsset bundles identity, lazy scalars, lazy textures, and adapter
        methods (``.to_threejs() / .to_gltf() / .to_mtlx()``). Creation
        is free — no network IO until ``.scalars`` / ``.textures`` / an
        adapter method is accessed. Mat-vis#93.
        """
        # Resolve the (source, material_id, tier) triple from whichever
        # input shape the caller used.
        s, mid, t = self._resolve_asset_triple(ref, material_id, tier, source, id)
        return VisAsset(self, s, mid, t)

    def _resolve_asset_triple(
        self,
        ref: "Match | str | None",
        positional_mid: str | None,
        positional_tier: str | None,
        kw_source: str | None,
        kw_id: str | None,
    ) -> tuple[str, str, str]:
        """Resolve the polymorphic ``asset()`` input into ``(source, id, tier)``.

        Precedence: positional ref > positional source/material_id/tier
        triple (legacy) > kwargs. Tier defaults to ``"auto"`` since
        0.7.0 (mat-vis#374).
        """
        # Match handle path. Match knows which tiers it's staged at —
        # but with auto/best now in play, the resolver picks just-in-
        # time on first .textures access. Default to ``"auto"`` and let
        # the asset's lazy resolver use the same staged-tier list later.
        if isinstance(ref, Match):
            return ref.source, ref.id, positional_tier or "auto"
        # String ref path (must contain '/').
        if isinstance(ref, str) and positional_mid is None and kw_source is None and kw_id is None:
            if "/" not in ref:
                raise ValueError(f"asset() string ref must be 'source/id', got {ref!r}")
            s, mid = ref.split("/", 1)
            return s, mid, positional_tier or "auto"
        # Legacy 3-positional path: ``asset("source", "id", "1k")``.
        if isinstance(ref, str) and positional_mid is not None:
            return ref, positional_mid, positional_tier or "auto"
        # Pure-kwarg path.
        if kw_source is not None and kw_id is not None:
            return kw_source, kw_id, positional_tier or "auto"
        raise TypeError(
            "asset() requires a Match, a 'source/id' string, "
            "(source, id, tier) positionals, or source=, id=, tier= kwargs"
        )

    # Plain float/int passthrough fields read verbatim from
    # ``mat_vis.pbr.*`` into the adapter scalars dict. Adapter-side
    # (``to_threejs`` / ``to_gltf``) already routes each of these to the
    # correct MeshPhysicalMaterial / KHR-extension key — the gap was
    # purely on the read side. mat-vis#380.
    _PBR_SCALAR_PASSTHROUGH: tuple[str, ...] = (
        "roughness",
        "metalness",
        "ior",
        "transmission",
        "thickness",
        "dispersion",
        "clearcoat",
        "clearcoat_roughness",
        "specular_intensity",
        "emissive",
        # Subsurface scattering (#409). The glTF adapter emits the
        # (draft) ``KHR_materials_subsurface`` extension; the Three.js
        # adapter is a documented no-op because MeshPhysicalMaterial
        # has no native SSS field.
        "subsurface",
        "subsurface_color",
        "subsurface_radius",
        # Emission scalar coverage (#406 / #405 Phase 3a) — factor +
        # linear-RGB tint. Adapter splits HDR strengths > 1 into
        # emissiveIntensity / KHR_materials_emissive_strength.
        "emission",
        "emission_color",
    )

    def _scalars_for(self, source: str, material_id: str) -> dict:
        """Look up PBR scalars for a material from the source index.

        Reads ``mat_vis.pbr.*`` (v3 catalog shape, ADR-0011) and passes
        through every PBR field the adapters can consume so glass-class
        materials (``transmission``, ``thickness``, ``dispersion``),
        coated materials (``clearcoat`` / ``clearcoat_roughness``), and
        gpuopen-style authored specular (``specular_intensity`` /
        ``specular_color``) actually reach the renderer instead of
        silently rendering as opaque-default-grey. mat-vis#380.

        Returns a flat dict keyed by the adapter interface's scalar
        names. ``color_hex`` is synthesized from ``pbr.color_rgb`` for
        ``to_threejs`` / ``to_gltf`` / ``to_mtlx``; ``specular_color``
        (linear RGB per :class:`PBRBlock`) is forwarded as
        ``specular_color_linear`` so the shared
        :func:`adapters._resolve_specular_color` path picks it up
        without re-doing the de-gamma boundary.

        Dumb-adapter contract (ADR-0013 / mat-vis#290): copy what's in
        the substrate verbatim, do not inject defaults, do not normalize.
        The baker already materialized neutral-multiplier conventions.

        Lookup is name-aware (mat-vis#368): substrate stores normalized
        lowercase ids, but callers (pymat, downstream tests) routinely
        pass display names like ``"Aluminum"`` or ``"Plastic (Acrylic)"``.
        Match against the canonical ``id`` exact, the casefold-normalized
        ``id``, or the casefold-normalized ``mat_vis.name``.

        Silent on failure — returns ``{}`` if the index is unavailable or
        the material isn't found. Used by :class:`MtlxSource` to fill in
        shader scalar inputs when a texture channel is absent.
        """
        scalars: dict = {}
        try:
            for entry in self.index(source):
                if not self._entry_matches_id_or_name(entry, material_id):
                    continue
                pbr = (entry.get("mat_vis") or {}).get("pbr") or {}
                for k in self._PBR_SCALAR_PASSTHROUGH:
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
                # specular_color is authored in linear RGB by the baker
                # (cf. ``PBRBlock.specular_color``); forward as the
                # ``specular_color_linear`` alias the adapters read.
                spec_rgb = pbr.get("specular_color")
                if isinstance(spec_rgb, list) and len(spec_rgb) >= 3:
                    scalars["specular_color_linear"] = list(spec_rgb[:3])
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
        tier: str = "auto",
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

        ``tier`` defaults to ``"auto"`` since 0.7.0 (mat-vis#374). For
        scalar-only materials ``"auto"`` raises :class:`NoPreviewError`
        (no PNG bytes exist to fetch); pass ``tier="best"`` for the
        same loud failure with the available-tiers hint.

        Returns raw bytes. Caches locally under the active tag scope.
        Pass ``tag="v..."`` to delegate to ``self.at(tag)`` for a
        specific release without reinstantiating.
        """
        if tag is not None and tag != self._tag:
            return self.at(tag).fetch_texture(source, material_id, channel, tier)

        # mat-vis#374: collapse auto/best to a concrete tier *before*
        # cache-key composition. Single-channel callers shouldn't pollute
        # the on-disk cache with ``…/auto/…`` paths.
        tier = self._resolve_tier(source, material_id, tier)
        if tier == "scalar":
            # Scalar-only materials have no PNG bytes to fetch — surface
            # the same loud error as the texture-fetch path always did
            # for un-staged channels.
            raise NoPreviewError(source, material_id)

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
        # Emit one progress notice per real network fetch (#287/#312).
        # Cache hits returned above stay silent. Library users
        # (build123d, Jupyter, pymat-mcp) wire this via on_event=
        # using a reporter from mat_vis_client.progress.
        log.info(
            "Downloading %s/%s/%s @ %s ...",
            source,
            resolved,
            channel,
            tier,
        )
        self._emit(
            "download_start",
            source=source,
            material=resolved,
            channel=channel,
            tier=tier,
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
            self._emit(
                "download_end",
                source=source,
                material=resolved,
                channel=channel,
                tier=tier,
                url=url,
                bytes_done=len(data),
                bytes_total=len(data),
            )
            self._maybe_warn_cache_cap()
            return data

        # Both extensions failed. Re-raise the underlying network error
        # so HTTPFetchError (and friends) propagate to callers.
        if last_exc is not None:
            raise last_exc
        raise MatVisError(f"channel {channel!r} not available for {source}/{resolved} @ {tier}")

    def prefetch_thumbs(
        self,
        source: str,
        *,
        max_workers: int = 8,
        tag: str | None = None,
    ) -> dict[str, int]:
        """Warm the on-disk cache with every material's preview thumbnail.

        Walks ``index(source)`` and concurrently fetches each material's
        ``.thumb`` (named-tier alias — dedicated baked thumb if staged
        per mat-vis#361, else smallest preview-ladder tier). Designed
        for grid views and CLI discovery surfaces where the first
        cold-cache iteration would otherwise serialise hundreds of HTTP
        round-trips.

        Returns a summary dict::

            {"ok": int, "no_preview": int, "unavailable": int, "errors": int}

        Concurrency uses :class:`concurrent.futures.ThreadPoolExecutor`
        — fetch_texture is I/O-bound (urllib + disk write); GIL
        contention is negligible. Default ``max_workers=8`` keeps HF
        request volume bounded.

        Per-fetch failures (NoPreviewError, PreviewUnavailableError,
        HTTPFetchError, network errors) are counted but never raise —
        a single bad material doesn't poison a 5000-material warm-up.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if tag is not None and tag != self._tag:
            return self.at(tag).prefetch_thumbs(source, max_workers=max_workers)

        entries = self.index(source)
        targets: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            mid = entry.get("id")
            if isinstance(mid, str):
                targets.append(mid)

        counters = {"ok": 0, "no_preview": 0, "unavailable": 0, "errors": 0}

        def _one(material_id: str) -> str:
            asset = VisAsset(self, source, material_id, "1k")
            result = asset.safe_thumb()
            if result.png is not None:
                return "ok"
            if result.error == "NoPreviewError":
                return "no_preview"
            if result.error == "PreviewUnavailableError":
                return "unavailable"
            return "errors"

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_one, mid) for mid in targets]
            for fut in as_completed(futures):
                counters[fut.result()] += 1

        return counters

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

    def cache_clear(self, *, stale_only: bool = False) -> int:
        """Delete cached data. Returns bytes freed.

        ``stale_only=True`` (mat-vis#355): keep the current client
        version's cache (``<cache_dir>/v0.7/...``) and only remove
        orphan layouts from previous client majors (``v0.6/``,
        ``latest/``, etc.). Default ``False`` removes everything for
        the v0.6 → v0.7 migration runbook hand-off; CLI surfaces
        ``--stale-only`` as the safer default.

        ``stale_only=False`` is the legacy semantics — unchanged.
        """
        import shutil

        if not self._cache_dir.exists():
            return 0

        if stale_only:
            freed = 0
            current = _CLIENT_CACHE_SEGMENT
            for entry in self._cache_dir.iterdir():
                if not entry.is_dir() or entry.name == current:
                    continue
                # Anything other than the current version segment is
                # orphan: legacy "latest" / older v0.X / a top-level
                # tag dir from before #355.
                try:
                    for path in entry.rglob("*"):
                        if path.is_file():
                            try:
                                freed += path.stat().st_size
                            except OSError:
                                pass
                    shutil.rmtree(entry, ignore_errors=True)
                except OSError:
                    pass
            return freed

        size = self.cache_size()
        shutil.rmtree(self._cache_dir, ignore_errors=True)
        return size

    def cache_check(self) -> dict:
        """Verify cache against HF and report sync state (mat-vis#355).

        Returns a JSON-serializable dict with the following keys:

        - ``manifest_in_sync``: ``bool`` — manifest ETag matches HF
        - ``indexes_in_sync``: ``dict[str, bool]`` — per-source ETag
          state (only sources we have a cached index for)
        - ``schema_version``: ``str`` — client version (matches the
          ``v<major.minor>`` cache segment for this client)
        - ``pinned_tag``: ``str`` — release tag the client is reading
        - ``stale_layouts``: ``list[str]`` — orphan cache directories
          from previous client majors / pre-#355 layouts
        - ``stale_bytes``: ``int`` — total bytes occupied by orphan
          layouts (reclaim hint for ``cache_clear(stale_only=True)``)
        - ``recommend``: ``str`` — one of ``"ok"`` / ``"refresh"`` /
          ``"clear-stale"`` / ``"clear-all"``

        The method does network I/O (one HEAD per cached file with
        ``If-None-Match``); use the cheap :meth:`cache_status`
        property for a local-only snapshot.

        Designed to round-trip cleanly through JSON (every value is a
        primitive type) so pymat-mcp / CLIs / dashboards can serialize
        directly without dataclass conversion.
        """
        # 1. Pinned tag + schema version — local, free.
        pinned = self._tag or DEFAULT_TAG
        schema = _CLIENT_CACHE_SEGMENT

        # 2. Manifest ETag check.
        manifest_in_sync = False
        cached_body, cached_etag = self._cache_read_manifest_with_etag()
        if cached_etag is not None:
            try:
                body, _ = _get_with_etag(self._manifest_url, etag=cached_etag)
                manifest_in_sync = body is None  # 304 means in-sync
            except Exception:  # noqa: BLE001
                manifest_in_sync = False

        # 3. Per-index ETag check (only sources we already cached).
        indexes_in_sync: dict[str, bool] = {}
        indexes_dir = self._cache_scope / ".indexes"
        if indexes_dir.is_dir():
            for entry in indexes_dir.iterdir():
                if entry.suffix != ".json":
                    continue
                source = entry.stem
                cached_etag = self._cache_read_text(entry.with_suffix(".etag"))
                if not cached_etag:
                    indexes_in_sync[source] = False
                    continue
                try:
                    body, _ = _get_with_etag(self._index_url(source), etag=cached_etag)
                    indexes_in_sync[source] = body is None
                except Exception:  # noqa: BLE001
                    indexes_in_sync[source] = False

        # 4. Stale-layout detection.
        stale_layouts: list[str] = []
        stale_bytes = 0
        if self._cache_dir.is_dir():
            for entry in self._cache_dir.iterdir():
                if not entry.is_dir() or entry.name == schema:
                    continue
                stale_layouts.append(entry.name)
                for path in entry.rglob("*"):
                    if path.is_file():
                        try:
                            stale_bytes += path.stat().st_size
                        except OSError:
                            pass

        # 5. Recommend.
        all_indexes_sync = all(indexes_in_sync.values()) if indexes_in_sync else True
        if not manifest_in_sync or not all_indexes_sync:
            recommend = "refresh"
        elif stale_layouts:
            recommend = "clear-stale"
        else:
            recommend = "ok"

        return {
            "manifest_in_sync": manifest_in_sync,
            "indexes_in_sync": indexes_in_sync,
            "schema_version": schema,
            "pinned_tag": pinned,
            "stale_layouts": sorted(stale_layouts),
            "stale_bytes": stale_bytes,
            "recommend": recommend,
        }

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
        # mat-vis#374: collapse auto/best so the channels() lookup uses
        # a concrete tier. Scalar-only short-circuits to no channels.
        resolved_tier = self._client._resolve_tier(self._source, self._material_id, self._tier)
        if resolved_tier == "scalar":
            chs: list[str] = []
        else:
            chs = self._client.channels(self._source, self._material_id, resolved_tier)
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

        # mat-vis#374: resolve auto/best up front so materialize() and
        # channels() agree on the concrete tier (otherwise auto could
        # collapse twice on different mtimes — at minimum confusing).
        resolved_tier = self._client._resolve_tier(self._source, self._material_id, self._tier)
        tex_dir = self._client.materialize(
            self._source, self._material_id, resolved_tier, output_dir
        )
        if resolved_tier == "scalar":
            chs: list[str] = []
        else:
            chs = self._client.channels(self._source, self._material_id, resolved_tier)

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
        "_resolved_tier_cache",
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
        # mat-vis#374: holds the concrete tier the auto/best resolver
        # picked. Populated lazily on first .textures / .resolved_tier
        # access. ``None`` means "not resolved yet". For literal-tier
        # assets it equals ``self._tier`` after first resolution.
        object.__setattr__(self, "_resolved_tier_cache", None)
        object.__setattr__(self, "_initialized", True)

    @classmethod
    def from_client(
        cls,
        client: MatVisClient,
        source: str,
        material_id: str,
        tier: str = "auto",
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

        mat-vis#374: when ``tier`` is ``"auto"`` or ``"best"`` we
        collapse to a concrete tier here and cache the result on
        ``resolved_tier`` so the bake pipeline (and downstream
        consumers) can record what was actually fetched.
        """
        if self._textures_cache is None:
            if self._is_scalar_only_entry():
                fetched: dict[str, bytes] = {}
                # Scalar-only: record "scalar" as the resolved tier so
                # downstream consumers can distinguish "no textures
                # because scalar-only" from "no textures because empty".
                object.__setattr__(self, "_resolved_tier_cache", "scalar")
            else:
                # mat-vis#374: collapse auto/best up front so the
                # cache key is the concrete tier, never the literal
                # ``"auto"``/``"best"``.
                resolved_tier = self._client._resolve_tier(
                    self._source, self._material_id, self._tier
                )
                object.__setattr__(self, "_resolved_tier_cache", resolved_tier)
                if resolved_tier == "scalar":
                    fetched = {}
                else:
                    fetched = self._client.fetch_all_textures(
                        self._source, self._material_id, resolved_tier
                    )
            object.__setattr__(self, "_textures_cache", fetched)
        return self._textures_cache

    @property
    def resolved_tier(self) -> str:
        """The concrete tier the auto/best resolver picked (mat-vis#374).

        For literal tiers (``"1k"``, ``"512"``, ...) this equals
        :attr:`tier`. For ``"auto"``/``"best"`` it's the staged tier
        the resolver chose — useful for the bake pipeline's manifest
        so downstream tools know what they're consuming.

        Triggers tier resolution on first access if textures haven't
        been fetched yet (no network IO — only an index lookup, which
        is itself cached). Subsequent accesses return the cached value.
        """
        if self._resolved_tier_cache is None:
            # Force resolution. For scalar-only entries the textures
            # property short-circuits and records "scalar"; for textured
            # entries it collapses auto/best via _resolve_tier.
            if self._is_scalar_only_entry():
                object.__setattr__(self, "_resolved_tier_cache", "scalar")
            else:
                resolved = self._client._resolve_tier(self._source, self._material_id, self._tier)
                object.__setattr__(self, "_resolved_tier_cache", resolved)
        return self._resolved_tier_cache

    def _is_scalar_only_entry(self) -> bool:
        """True if this asset's index entry advertises no texture tiers.

        Scalar-only entries are physicallybased (always) and the 18
        gpuopen subset (mat-vis#369) — they publish
        ``available_tiers=["scalar"]`` (the sentinel) under the post-#369
        substrate convention. The check is shape-driven so future
        scalar-only sources work without code changes. We treat
        ``[]``/``None``/missing-key as scalar-only too, for resilience
        to legacy substrates baked before #369 landed. Best-effort: a
        missing index or lookup error falls back to ``False``,
        preserving the existing (loud) error path through
        :meth:`fetch_all_textures`.
        """
        try:
            entries = self._client.index(self._source)
        except Exception:
            return False
        if not isinstance(entries, list):
            return False
        # mat-vis#372: route through the centralized 3-way predicate.
        # Previously a bespoke 2-way match here (exact id / norm name)
        # missed the norm-id leg — silent #367-class drift relative to
        # _scalars_for. Now both sites share one matcher.
        for entry in entries:
            if self._client._entry_matches_id_or_name(entry, self._material_id):
                tiers = entry.get("available_tiers") or []
                # Scalar-only iff no tier is a real texture tier.
                # Pre-#369 substrates emit ``[]`` or omit the key (both
                # land here as ``[]`` after the ``or``); post-#369 emit
                # ``["scalar"]``. Either way the entry is scalar-only.
                return all(t == "scalar" for t in tiers)
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

    # ── Discovery surface (mat-vis#asset-thumb) ───────────────────
    #
    # Three surfaces, three contracts:
    #   .thumb            — property, raises typed errors  (REPL one-liner)
    #   .thumb_for(...)   — method, raises typed errors    (explicit override)
    #   .safe_thumb()     — method, never raises           (iteration / MCP)
    #
    # Tier name ``"thumb"`` is a **first-class alias** (mat-vis#361):
    # resolves to a dedicated baked tier when present, else falls back
    # to the smallest staged texture tier in the preview ladder. When
    # bake-side #361 ships, .thumb starts returning ~10KB sphere
    # renders without any client-side change — same call site, better
    # bytes.
    #
    # Channel default for ``.thumb`` walks a small fallback ladder
    # (color → basecolor → albedo → normal → roughness) so a
    # metallic-only material still produces *some* preview. When
    # caller passes channel= explicitly, only that channel is tried.

    _PREVIEW_TIER_LADDER: ClassVar[tuple[str, ...]] = ("128", "256", "512", "1k")
    _THUMB_CHANNEL_LADDER: ClassVar[tuple[str, ...]] = (
        "color",
        "basecolor",
        "albedo",
        "normal",
        "roughness",
    )

    @property
    def thumb(self) -> bytes:
        """Small preview PNG for material discovery (REPL / Jupyter / MCP).

        Resolves ``tier="thumb"``: a dedicated baked sphere render
        (mat-vis#361) when present, else the smallest staged texture
        tier in the preview ladder (``128``/``256``/``512``/``1k``).
        Channel defaults walk ``color`` → ``basecolor`` → ``albedo``
        → ``normal`` → ``roughness`` so metallic-only materials still
        return some preview.

        Raises:
            NoPreviewError: source is scalar-only (no PNG textures
                exist; only fixable by mat-vis#361 sphere bakes).
            PreviewUnavailableError: textures exist but no preview-
                sized tier is staged. Carries ``available`` so the
                caller can call :meth:`thumb_for(tier=...)` explicitly.
            HTTPFetchError: 5xx or persistent network failure
                (transient errors are retried by the underlying fetch).
            NetworkError: DNS / connection failure after retries.

        For non-raising semantics (iteration, MCP tool results), use
        :meth:`safe_thumb` which returns a :class:`ThumbResult`.
        """
        return self.thumb_for()

    def thumb_for(
        self,
        *,
        channel: str | None = None,
        tier: str = "thumb",
    ) -> bytes:
        """Fetch a preview-sized texture with explicit overrides.

        ``tier`` defaults to ``"thumb"`` — a named alias resolved to
        the dedicated baked thumb tier (mat-vis#361) if staged, else
        the smallest preview-ladder tier. Pass an explicit tier name
        (e.g. ``"512"``) to bypass the alias and fetch one specific
        size.

        ``channel`` defaults to ``None`` — walks the channel fallback
        ladder. Pass an explicit channel (``"color"``, ``"normal"``,
        etc.) to fetch only that channel; raises if it's missing.

        Raises the same typed errors as :attr:`thumb`. See that
        property's docstring for the full error contract.
        """
        if self._is_scalar_only_entry():
            raise NoPreviewError(self._source, self._material_id)

        target_tiers = self._resolve_tier_candidates(tier)
        if not target_tiers:
            raise PreviewUnavailableError(
                self._source,
                self._material_id,
                available=self._available_tiers(),
            )

        target_channels: tuple[str, ...]
        if channel is None:
            target_channels = self._THUMB_CHANNEL_LADDER
        else:
            target_channels = (channel,)

        last_exc: Exception | None = None
        for t in target_tiers:
            for ch in target_channels:
                try:
                    return self._client.fetch_texture(self._source, self._material_id, ch, tier=t)
                except NetworkError:
                    # Network failure — not a "doesn't exist" signal.
                    # Don't keep walking; the next combo would just
                    # re-fail the same way and waste retries.
                    raise
                except HTTPFetchError as e:
                    # 5xx is not a "this tier/channel doesn't exist"
                    # signal — it's a real substrate problem. Surface
                    # immediately so retries don't mask outages.
                    if 500 <= e.code < 600:
                        raise
                    last_exc = e
                    continue
                except (ChannelNotFoundError, MatVisError) as e:
                    # ChannelNotFoundError + the loose MatVisError
                    # raised by fetch_texture's pre-flight channel
                    # check (L1822) both mean "this channel isn't
                    # staged at this tier" — try the next combo.
                    last_exc = e
                    continue

        # Exhausted every (tier, channel) combination. If user passed
        # explicit args, surface the underlying error verbatim so they
        # can debug what they asked for; otherwise wrap in
        # PreviewUnavailableError with the staged-tiers hint.
        if channel is not None or tier != "thumb":
            if last_exc is not None:
                raise last_exc
        raise PreviewUnavailableError(
            self._source,
            self._material_id,
            available=self._available_tiers(),
        ) from last_exc

    def safe_thumb(
        self,
        *,
        channel: str | None = None,
        tier: str = "thumb",
    ) -> ThumbResult:
        """Non-raising thumbnail fetch — returns :class:`ThumbResult`.

        Designed for the two surfaces where exceptions break flow:

        - **Iteration / grids**: ``[m.safe_thumb() for m in materials]``
          composes; one bad material doesn't poison the comprehension.
        - **MCP / structured tools**: pymat-mcp serialises the result
          as JSON so the LLM sees ``{png, error, reason, channel, tier}``
          rather than catching exceptions.

        Internally calls :meth:`thumb_for` with the same kwargs and
        traps everything. ``error`` is the exception class name (stable
        tag for routing), ``reason`` is the message. ``channel`` /
        ``tier`` reflect what the resolver picked on success; both
        ``None`` on failure (resolver never committed to a target).
        """
        try:
            png = self.thumb_for(channel=channel, tier=tier)
        except (NoPreviewError, PreviewUnavailableError) as e:
            return ThumbResult(
                png=None,
                error=type(e).__name__,
                reason=str(e),
                channel=None,
                tier=None,
            )
        except (HTTPFetchError, NetworkError) as e:
            return ThumbResult(
                png=None,
                error=type(e).__name__,
                reason=str(e),
                channel=None,
                tier=None,
            )
        except MatVisError as e:
            return ThumbResult(
                png=None,
                error=type(e).__name__,
                reason=str(e),
                channel=None,
                tier=None,
            )
        # Success — record what the resolver picked. We can't know
        # the exact (tier, channel) pair after the fact without
        # re-running the resolver, but we can report the inputs the
        # caller chose; explicit args reflect their intent, defaults
        # reflect "the resolver decided".
        return ThumbResult(png=png, error=None, reason=None, channel=channel, tier=tier)

    def _available_tiers(self) -> list[str]:
        """Return the ``available_tiers`` list from this asset's index entry.

        Empty list when entry not found, no index, or scalar-only.
        Best-effort: any failure returns ``[]`` so callers (typed-error
        constructors) get a useful-or-empty hint rather than cascading
        exceptions out of an error path.
        """
        try:
            entries = self._client.index(self._source)
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(entries, list):
            return []
        norm = self._client._normalize_name(self._material_id)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if (
                entry.get("id") == self._material_id
                or self._client._normalize_name((entry.get("mat_vis") or {}).get("name") or "")
                == norm
            ):
                staged = entry.get("available_tiers") or []
                return list(staged) if isinstance(staged, list) else []
        return []

    def _resolve_tier_candidates(self, tier: str) -> tuple[str, ...]:
        """Resolve a tier name to the ordered candidate list to try.

        - ``"thumb"`` → dedicated baked tier first if staged
          (mat-vis#361), then the preview ladder (``128`` →
          ``256`` → ``512`` → ``1k``) filtered to staged tiers
        - ``"auto"`` / ``"best"`` → single-element tuple of the
          concrete tier picked by :meth:`MatVisClient._resolve_tier`
          (mat-vis#374). ``"auto"`` skips ``"thumb"`` (different
          layer: source-quality vs render-quality). ``"best"`` may
          raise :class:`MaterialNotStagedError` here when no texture
          tier is staged — surfaced loudly per the archival contract.
        - any other value → single-element tuple, caller named it

        Empty tuple iff ``"thumb"`` requested but the entry has no
        staged tier ≤ ``1k`` and no dedicated thumb tier — caller
        raises :class:`PreviewUnavailableError`.
        """
        if tier in ("auto", "best"):
            # Collapse via the client-level resolver; raises for
            # ``best`` with no texture tier (loud archival contract).
            resolved = self._client._resolve_tier(self._source, self._material_id, tier)
            return (resolved,)
        if tier != "thumb":
            return (tier,)
        staged = self._available_tiers()
        if not staged:
            # Could be index miss; fall back to the asset's pinned
            # tier rather than refusing — preserves the pre-thumb
            # behavior when an entry exists but available_tiers is
            # absent (older catalogs).
            return (self._tier,)
        candidates: list[str] = []
        if "thumb" in staged:
            candidates.append("thumb")
        for t in self._PREVIEW_TIER_LADDER:
            if t in staged and t not in candidates:
                candidates.append(t)
        return tuple(candidates)

    def _repr_png_(self) -> bytes | None:
        """IPython rich-repr hook: inline image in Jupyter cells.

        Returns the same bytes as :attr:`thumb` on success. On any
        failure returns ``None`` so IPython falls through to
        :meth:`_repr_html_` (which renders the diagnostic) and finally
        :meth:`__repr__`. Never breaks the cell.
        """
        try:
            return self.thumb
        except Exception:  # noqa: BLE001
            return None

    def _repr_html_(self) -> str | None:
        """IPython rich-repr fallback: diagnostic HTML when no PNG.

        Returns ``None`` on success so :meth:`_repr_png_` wins (IPython
        prefers the higher-priority repr that returns non-None). On
        failure returns a small ``<div>`` explaining *why* there's no
        preview — bernhard's #312 pain point was a silent ``None``
        repr; this surface makes the failure visible without raising.
        """
        result = self.safe_thumb()
        if result.png is not None:
            return None
        return (
            f'<div style="font-family:monospace;color:#888;'
            f'border:1px solid #ddd;padding:6px 10px;border-radius:4px">'
            f"<b>{type(self).__name__}</b>"
            f"({self._source!r}, {self._material_id!r}, tier={self._tier!r})"
            f"<br/>preview unavailable: <b>{result.error}</b>: {result.reason}"
            f"</div>"
        )

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
    p_cache_sub.add_parser("status", help="Show cache size breakdown (local-only, free)")
    p_check = p_cache_sub.add_parser(
        "check", help="Verify cache against HF (mat-vis#355) — emits JSON status"
    )
    p_check.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (default: human-readable summary)",
    )
    p_clear = p_cache_sub.add_parser("clear", help="Delete cached data")
    p_clear.add_argument(
        "--stale-only",
        action="store_true",
        help="Only delete orphan layouts from previous client versions (mat-vis#355)",
    )
    p_clear.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
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
        elif args.cache_cmd == "check":
            status = client.cache_check()
            if args.json:
                print(json.dumps(status, indent=2))
            else:
                print(
                    f"  schema:    {status['schema_version']} (cache lives at "
                    f"{client._cache_dir / status['schema_version']})"
                )
                print(f"  pinned:    {status['pinned_tag']}")
                print(
                    f"  manifest:  {'in-sync' if status['manifest_in_sync'] else 'STALE'}",
                    file=sys.stderr,
                )
                if status["indexes_in_sync"]:
                    for src, ok in sorted(status["indexes_in_sync"].items()):
                        print(f"  index/{src}:  {'in-sync' if ok else 'STALE'}", file=sys.stderr)
                if status["stale_layouts"]:
                    print(
                        f"  orphans:   {', '.join(status['stale_layouts'])} "
                        f"(~{_fmt_size(status['stale_bytes'])})",
                        file=sys.stderr,
                    )
                print(f"\nrecommendation: {status['recommend']}", file=sys.stderr)
                if status["recommend"] == "clear-stale":
                    print(
                        "  → run `python -m mat_vis_client cache clear --stale-only`",
                        file=sys.stderr,
                    )
        elif args.cache_cmd == "clear":
            if not args.yes and not args.stale_only:
                print(
                    f"This will delete ALL cached data at {client._cache_dir} "
                    f"(~{_fmt_size(client.cache_size())}). Use --yes to confirm "
                    "or --stale-only to keep current version's cache.",
                    file=sys.stderr,
                )
                sys.exit(1)
            freed = client.cache_clear(stale_only=args.stale_only)
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
