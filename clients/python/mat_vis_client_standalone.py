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
import os
import sys
import time
import urllib.request
from pathlib import Path

REPO = "MorePET/mat-vis"
GITHUB_RELEASES = f"https://github.com/{REPO}/releases"
GITHUB_API = f"https://api.github.com/repos/{REPO}"
GITHUB_RAW = f"https://raw.githubusercontent.com/{REPO}"
LATEST_MANIFEST_URL = f"{GITHUB_RELEASES}/latest/download/release-manifest.json"
PYPI_API = "https://pypi.org/pypi/mat-vis-client/json"
DEFAULT_CACHE_DIR = Path(os.environ.get("MAT_VIS_CACHE", Path.home() / ".cache" / "mat-vis"))
USER_AGENT = "mat-vis-client/0.2 (Python)"

# Update check: cache TTL (24h) + opt-out env var
UPDATE_CHECK_TTL_SECONDS = 24 * 3600
UPDATE_CHECK_DISABLED = os.environ.get("MAT_VIS_NO_UPDATE_CHECK", "").lower() in (
    "1",
    "true",
    "yes",
)


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

# Valid categories per index-schema.json
CATEGORIES = frozenset(
    [
        "metal",
        "wood",
        "stone",
        "fabric",
        "plastic",
        "concrete",
        "ceramic",
        "glass",
        "organic",
        "other",
    ]
)


# Rate limit / retry knobs (env-configurable).
MAX_RETRIES = int(os.environ.get("MAT_VIS_MAX_RETRIES", "5"))
BACKOFF_BASE_SECONDS = float(os.environ.get("MAT_VIS_BACKOFF_BASE", "1.0"))
RETRY_MAX_WAIT_SECONDS = int(os.environ.get("MAT_VIS_RETRY_MAX_WAIT", "60"))


class MatVisError(Exception):
    """Base class for mat-vis-client errors."""


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
    """True if this HTTPError is a rate-limit signal (429, 503, or 403+headers)."""
    if err.code in (429, 503):
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
            if not _is_rate_limited(e) or attempt >= MAX_RETRIES:
                raise
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
                raise
            wait = min(int(BACKOFF_BASE_SECONDS * (2**attempt)), RETRY_MAX_WAIT_SECONDS)
            print(
                f"mat-vis-client: network error ({e.reason}), "
                f"retry {attempt + 1}/{MAX_RETRIES} in {wait}s",
                file=sys.stderr,
            )
            time.sleep(wait)

    # Should be unreachable (loop always raises or returns), but fail loud
    raise RateLimitError(url, 0, f"exhausted {MAX_RETRIES} retries") from last_err


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
    ):
        self._cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._manifest: dict | None = None
        self._rowmaps: dict[str, dict] = {}
        self._indexes: dict[str, list[dict]] = {}
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
    def manifest(self) -> dict:
        """Fetch and cache the release manifest. Validates schema_version."""
        if self._manifest is None:
            cache_path = self._cache_dir / ".manifest.json"
            if cache_path.exists():
                self._manifest = json.loads(cache_path.read_text())
            else:
                self._manifest = _get_json(self._manifest_url)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(self._manifest, indent=2))
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

    def _maybe_warn_updates(self) -> None:
        """Print a one-line notice to stderr if newer data or client exists.

        Runs once per process. Opt out with MAT_VIS_NO_UPDATE_CHECK=1.
        Network failures are silent — never block usage.
        """
        if UPDATE_CHECK_DISABLED or getattr(self, "_update_warned", False):
            return
        self._update_warned = True
        try:
            result = self.check_updates()
        except Exception:
            return

        data = result["data"]
        client = result["client"]
        if data["newer_available"]:
            print(
                f"mat-vis: newer data release available "
                f"({data['current']} → {data['latest']}). "
                f"Use MatVisClient() for latest or set tag={data['latest']!r}.",
                file=sys.stderr,
            )
        if client["newer_available"]:
            print(
                f"mat-vis-client: newer version available "
                f"({client['current']} → {client['latest']}). "
                f"Upgrade: pip install -U mat-vis-client",
                file=sys.stderr,
            )

    @staticmethod
    def _check_schema_version(manifest: dict) -> None:
        """Refuse to operate on manifests with incompatible schema.

        Accepts legacy 'version' field (older manifests used 'version: 1')
        and the canonical 'schema_version' field (v2+).
        """
        schema = manifest.get("schema_version", manifest.get("version", 1))
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
        """List available tiers."""
        return list(self.manifest.get("tiers", {}).keys())

    def rowmap(self, source: str, tier: str, category: str | None = None) -> dict:
        """Fetch and cache rowmaps. Merges partitioned rowmaps into one."""
        key = f"{source}-{tier}-{category or 'all'}"
        if key not in self._rowmaps:
            tier_data = self.manifest["tiers"][tier]
            base_url = tier_data["base_url"]
            src_data = tier_data["sources"][source]

            rowmap_files = src_data.get("rowmap_files", [])
            if not rowmap_files:
                rowmap_file = src_data.get("rowmap_file", f"{source}-{tier}-rowmap.json")
                rowmap_files = [rowmap_file]

            if category:
                rowmap_files = [f for f in rowmap_files if category in f] or rowmap_files[:1]

            # Fetch all partition rowmaps and merge materials
            merged: dict = {"materials": {}}
            for rmf in rowmap_files:
                cache_path = self._cache_dir / ".rowmaps" / rmf
                if cache_path.exists():
                    rm = json.loads(cache_path.read_text())
                else:
                    url = base_url + rmf
                    rm = _get_json(url)
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(json.dumps(rm, indent=2))

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
            cache_path = self._cache_dir / ".indexes" / f"{source}.json"
            if cache_path.exists():
                self._indexes[source] = json.loads(cache_path.read_text())
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
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(self._indexes[source], indent=2))
        return self._indexes[source]

    def search(
        self,
        category: str | None = None,
        *,
        roughness_range: tuple[float, float] | None = None,
        metalness_range: tuple[float, float] | None = None,
        source: str | None = None,
        tier: str = "1k",
    ) -> list[dict]:
        """Search materials by category and scalar ranges.

        Fetches index JSON for the given source (or all sources for the
        tier) and filters locally. Returns matching index entries.

        Args:
            category: Filter by material category (e.g. "metal", "wood").
            roughness_range: (min, max) roughness filter, inclusive.
            metalness_range: (min, max) metalness filter, inclusive.
            source: Limit search to one source. If None, searches all
                    sources available for the given tier.
            tier: Only return materials that have this tier available.
        """
        if category and category not in CATEGORIES:
            raise ValueError(
                f"Unknown category {category!r}. Valid: {', '.join(sorted(CATEGORIES))}"
            )

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

        return results

    # ── Bulk operations ─────────────────────────────────────────

    def fetch_all_textures(
        self,
        source: str,
        material_id: str,
        tier: str = "1k",
    ) -> dict[str, bytes]:
        """Fetch all texture channels for a material.

        Returns a dict mapping channel name to PNG bytes.
        """
        chs = self.channels(source, material_id, tier)
        return {ch: self.fetch_texture(source, material_id, ch, tier) for ch in chs}

    def prefetch(
        self,
        source: str,
        tier: str = "1k",
        *,
        on_progress: callable | None = None,
    ) -> int:
        """Bulk download all materials for a source + tier to cache.

        Args:
            source: Source name (e.g. "ambientcg").
            tier: Resolution tier (default "1k").
            on_progress: Optional callback(material_id, index, total).

        Returns the number of materials fetched.
        """
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

    def to_mtlx(
        self,
        source: str,
        material_id: str,
        tier: str = "1k",
        output_dir: str | Path = ".",
    ) -> Path:
        """Materialize textures and generate a MaterialX document.

        Writes PNGs to output_dir/{material_id}/ and creates a .mtlx
        file referencing them with local paths. The result is loadable
        by any standard MaterialX renderer.

        Returns path to the .mtlx file.
        """
        from mat_vis_client.adapters import export_mtlx

        tex_dir = self.materialize(source, material_id, tier, output_dir)
        chs = self.channels(source, material_id, tier)

        # Get scalars from index if available
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

        return export_mtlx(
            scalars=scalars,
            output_dir=str(tex_dir),
            material_name=material_id,
            texture_dir=str(tex_dir),
            channels=chs,
        )

    # ── Original MaterialX (gpuopen) ───────────────────────────

    def fetch_mtlx_original(self, source: str, material_id: str) -> str | None:
        """Fetch original upstream MaterialX XML for a material.

        Downloads and caches the {source}-mtlx.json map from the release,
        then returns the XML string for the given material_id.
        Returns None if the source has no original mtlx or material not found.
        """
        if not hasattr(self, "_mtlx_originals"):
            self._mtlx_originals: dict[str, dict[str, str]] = {}

        if source not in self._mtlx_originals:
            tag = self.manifest.get("release_tag", self._tag or "")
            url = f"{GITHUB_RELEASES}/download/{tag}/{source}-mtlx.json"
            try:
                data = _get_json(url)
                self._mtlx_originals[source] = data
            except Exception:
                self._mtlx_originals[source] = {}

        return self._mtlx_originals[source].get(material_id)

    def materialize_mtlx(
        self,
        source: str,
        material_id: str,
        tier: str = "1k",
        output_dir: str | Path = ".",
    ) -> Path | None:
        """Fetch original MaterialX, rewrite texture paths, write to disk.

        For materials with original upstream mtlx (gpuopen), fetches the
        XML, materializes the PNG textures, then rewrites texture file
        references to point at the local PNGs.

        Falls back to generated UsdPreviewSurface if no original exists.

        Returns path to the .mtlx file, or None on failure.
        """
        import re

        xml_str = self.fetch_mtlx_original(source, material_id)
        if xml_str is None:
            # Fall back to generated
            return self.to_mtlx(source, material_id, tier, output_dir)

        # Materialize textures
        tex_dir = self.materialize(source, material_id, tier, output_dir)
        chs = self.channels(source, material_id, tier)

        # Build a map of possible original filenames → our channel filenames
        # GPUOpen uses names like BaseColor.png, Normal.png, Roughness.png
        _FILENAME_TO_CHANNEL = {
            "basecolor": "color",
            "base_color": "color",
            "diffuse": "color",
            "normal": "normal",
            "roughness": "roughness",
            "specular_roughness": "roughness",
            "metallic": "metalness",
            "metalness": "metalness",
            "occlusion": "ao",
            "ao": "ao",
            "ambientocclusion": "ao",
            "displacement": "displacement",
            "height": "displacement",
            "emission": "emission",
            "emissive": "emission",
        }

        def _rewrite_path(match: re.Match) -> str:
            """Replace texture filename with local path."""
            orig = match.group(1)
            stem = Path(orig).stem.lower().replace(" ", "").replace("-", "").replace("_", "")
            # Try direct match
            for pattern, channel in _FILENAME_TO_CHANNEL.items():
                clean_pattern = pattern.replace("_", "")
                if clean_pattern in stem and channel in chs:
                    return f'value="{tex_dir / (channel + ".png")}"'
            # Try substring
            for pattern, channel in _FILENAME_TO_CHANNEL.items():
                clean_pattern = pattern.replace("_", "")
                if clean_pattern in stem:
                    local = tex_dir / f"{channel}.png"
                    if local.exists():
                        return f'value="{local}"'
            return match.group(0)

        # Rewrite all filename values in the XML
        rewritten = re.sub(
            r'value="([^"]*\.(?:png|jpg|jpeg|tif|tiff|exr))"',
            _rewrite_path,
            xml_str,
            flags=re.IGNORECASE,
        )

        mtlx_path = tex_dir / f"{material_id}.mtlx"
        mtlx_path.write_text(rewritten, encoding="utf-8")
        return mtlx_path

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
        mat = rm["materials"][material_id]
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
    ) -> bytes:
        """Fetch a single texture PNG via HTTP range read.

        Returns raw PNG bytes. Caches locally.
        """
        # Check cache first
        cache_path = self._cache_dir / source / tier / material_id / f"{channel}.png"
        if cache_path.exists():
            return cache_path.read_bytes()

        # Find in rowmap
        rm = self.rowmap(source, tier)
        mat = rm["materials"][material_id]
        rng = mat[channel]
        offset = rng["offset"]
        length = rng["length"]

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
        except urllib.error.HTTPError as e:
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

        # Cache
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(data)

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
