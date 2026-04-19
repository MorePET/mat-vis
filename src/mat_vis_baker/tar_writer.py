"""Streaming tar writer for (source, tier) bundles + offset/length sidecar.

Counterpart to ``parquet_writer`` for the v0.5.0 HF-substrate substrate.
One ``TarWriter`` per (source, tier). Each ``add_channel`` call appends
one texture blob under ``{material_id}/{channel}.{ext}`` and records the
exact byte offset (first byte after the 512-byte tar header) + length
so HTTP range reads can slice the payload without untarring.

The resulting rowmap has the same shape as the parquet-era rowmap:

    {material_id: {channel: {"offset": int, "length": int}}}

so downstream consumers stay unchanged (see ADR-0007).
"""

from __future__ import annotations

import io
import logging
import tarfile
from pathlib import Path

log = logging.getLogger("mat-vis-baker.tar")

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_KTX2_MAGIC = b"\xabKTX 20\xbb\r\n\x1a\n"


def _detect_extension(data: bytes) -> str:
    if data.startswith(_KTX2_MAGIC):
        return "ktx2"
    if data.startswith(_PNG_MAGIC):
        return "png"
    return "bin"


class TarWriter:
    """Append-only tar writer with an authoritative offset/length sidecar.

    Every ``add_channel`` call writes one entry. Duplicates (same
    ``material_id``/``channel`` pair) raise — an accidental double-add
    would leave two payloads in the archive and clobber one rowmap
    entry, silently dropping a channel.
    """

    def __init__(self, output_path: Path) -> None:
        self._output_path = output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._tar = tarfile.open(output_path, mode="w", format=tarfile.USTAR_FORMAT)
        self._materials: dict[str, dict[str, dict[str, int]]] = {}
        self._closed = False

    def add_channel(self, material_id: str, channel: str, data: bytes) -> None:
        if self._closed:
            raise RuntimeError(f"TarWriter({self._output_path}) already finalized")
        channels = self._materials.setdefault(material_id, {})
        if channel in channels:
            raise ValueError(
                f"duplicate entry: {material_id}/{channel} already added to "
                f"{self._output_path.name}"
            )

        ext = _detect_extension(data)
        info = tarfile.TarInfo(name=f"{material_id}/{channel}.{ext}")
        info.size = len(data)

        # `tarfile.addfile` copies the TarInfo internally and modifies
        # the copy, so ``info.offset_data`` on our instance stays at its
        # default. Capture the block start from ``self._tar.offset``
        # before the write; the header is always 512 bytes so the
        # payload starts at ``block_start + 512``.
        block_start = self._tar.offset
        self._tar.addfile(info, io.BytesIO(data))

        channels[channel] = {
            "offset": block_start + tarfile.BLOCKSIZE,
            "length": len(data),
        }

    def finalize(self) -> dict[str, dict[str, dict[str, int]]]:
        if not self._closed:
            self._tar.close()
            self._closed = True
        log.info(
            "wrote %s (%d materials, %d channels, %.1f MB)",
            self._output_path,
            len(self._materials),
            sum(len(v) for v in self._materials.values()),
            self._output_path.stat().st_size / 1e6,
        )
        return self._materials

    def __enter__(self) -> TarWriter:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._closed:
            self._tar.close()
            self._closed = True
