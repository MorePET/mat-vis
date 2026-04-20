# 0009. Derive pipeline: HTTP-range streaming, parallel workers, ICC-strip, fail-fast

- Status: Accepted
- Date: 2026-04-20
- Deciders: @gerchowl

## Context

The v0.5.0 substrate (ADR-0007) splits the baker into primary bakes
(upstream → tar) and derive pipelines (existing tar → smaller tier /
KTX2 transcode). The first derive-pipeline implementation had three
distinct failure classes surface during the v2026.04.1 matrix:

1. **RAM**: loading a 17 GB (or 68 GB for 2k) source tar via
   `Path.read_bytes()` OOM-killed the runner. ~7 GB RAM on free GH
   runners; multi-tens-of-GB tars don't fit.
2. **Disk**: even if the runner doesn't OOM on the read, storing the
   full source tar + writing a same-size output tar exceeded the
   ~84 GB runner disk for 2k derivations.
3. **Silent format-mismatch failures**: `toktx` refuses PNGs with
   embedded ICC profiles. Polyhaven ships ICC-tagged PNGs, so ~40%
   of channels failed silently (logged as warnings, counted as
   `n_failed`, but the derive still "succeeded" and pushed a tar
   with holes).

Symptom trail: 7 of 15 concurrent derives OOM'd; the 2 that didn't
crash produced 60%-complete ktx2 tars that looked fine from the
outside but had hundreds of missing channels.

## Decision

### 1. Stream the source tar via HTTP Range reads

Never download the full source tar. The rowmap encodes
`(offset, length)` per channel; HF's resolve URL supports
`Range: bytes=<lo>-<hi>` natively (the client uses this for
`fetch_texture`). Reuse that pattern in the derive pipeline:

- `_pin_commit(repo_id, revision)` → resolve the branch/tag to a
  concrete SHA up front so the byte offsets we read stay consistent
  even if a concurrent bake commits during the run.
- `_fetch_rowmap()` → one GET for the per-source-tier rowmap.
- `_range_read(...)` → per-channel Range GET against
  `{HF_BASE}/<repo>/resolve/<sha>/<tar>`. One `requests.Session`
  reuses the TCP connection.

Resource envelope per run: **disk = O(output tar), RAM = O(N workers × one channel)**.
For a 68 GB source, disk peak drops from ~140 GB to ~50 GB; RAM peak
drops from 68 GB to ~80 MB.

### 2. Parallel workers, serial tar write

`ThreadPoolExecutor` with 8 workers for resize (I/O-bound on HTTPS +
PIL), 4 workers for KTX2 (CPU-bound on `toktx`). Per-channel fetch +
transform happens in parallel; results drain through
`as_completed()` to a serial `TarWriter.add_channel` — the writer
is not thread-safe and its offset/length sidecar must be written in
the order bytes land in the tar.

Workers are env-tunable: `MAT_VIS_DERIVE_WORKERS`,
`MAT_VIS_KTX2_WORKERS`. Memory pressure scales linearly with worker
count × channel size.

### 3. Strip ICC profile on the PNG → KTX2 path

`toktx` rejects PNGs with embedded ICC profiles unless passed
`--assign_oetf` to explicitly label the color space. Auto-assigning
one is unsafe — normal / roughness / AO / displacement textures are
**linear** data, not sRGB; a blanket `--assign_oetf srgb` would
mislabel them and subtly corrupt pixel interpretation downstream.

Instead: re-encode the incoming PNG through PIL with
`icc_profile=None`, which strips the ICC and EXIF blobs while
preserving pixel bytes byte-for-byte. `toktx` accepts the cleaned
PNG and writes a KTX2 with no color-space assumption baked in —
which is what we want, since channel semantics (color / normal /
roughness / …) are carried by the channel name in the tar, not by
a tag on the container.

### 4. Fail-fast on systemic failure

`_stream_transform_into_tar` watches the first N=50 completions. If
more than 20% have failed by then, the transform is broken
systematically (missing binary, bad auth, format mismatch) and the
pool is cancelled with a `RuntimeError` that includes the first
error's stderr.

Per-channel failures above that window keep being logged but do not
abort — a handful of bad textures shouldn't fail an 11,000-channel
bake, and the rowmap simply omits them (clients get a typed
`ChannelNotFoundError` on read if they ask).

### 5. Propagate `toktx` stderr

The initial implementation passed `capture_output=True` to
`subprocess.run(check=True)`, which swallowed stderr on non-zero
exit. Switched to capturing explicitly and raising
`RuntimeError("toktx exit N: <stderr>")` so the
workflow log surfaces the real reason (e.g. "It has an ICC profile.
These are not supported.") instead of an opaque exit-1.

### 6. `$GITHUB_STEP_SUMMARY` per derive

Each derive appends a small markdown block to
`$GITHUB_STEP_SUMMARY` (ok count, fail count, first error). Shows
up on the job summary page — readable post-run without scrubbing
the log. Cheap (~200 B per run) and no-op outside GH Actions.

## Consequences

### What the CI signal looks like now

- Broken transform (missing binary, auth): **fails in <1 minute**
  after the first 50 channels complete.
- Systematic data corruption (ICC on every input): fails in <1
  minute (same fail-fast).
- Sparse failures (a handful of bad textures out of thousands):
  counted, logged, step-summary records them; the run succeeds
  and the rowmap reflects the gap.

### What this doesn't fix

- The v1 of ADR-0007 had `release-manifest.json` as a shared
  write target. That race class is handled separately in
  ADR-0008.
- Runner disk still bounds the output tar size. For 4k / 8k
  derivations we'd need larger runners or output chunking.
- Per-channel HTTP is more round trips than one big download.
  HF's CDN is fine with this pattern (used by the client for
  every `fetch_texture`), but it would underperform against a
  rate-limiting origin.

## Upgrade triggers

- HF CDN changes rate-limit semantics for range reads (currently
  generous).
- We add a fifth pipeline that doesn't fit the "download → transform
  → upload" shape and needs different IO primitives.
