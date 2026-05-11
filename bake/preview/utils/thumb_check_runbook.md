# Thumb-check gate — operator runbook (#385)

When the thumb-check CI gate fails, follow these steps. The gate is
`bake/preview/utils/check_thumbs.py`. It runs after every thumb bake
and BEFORE the HF upload step (so a failed gate prevents bad data
from reaching the dataset).

## 1. Download the JSON artifact

The CI step uploads `thumb-check.json` as an artifact (default name
`thumb-check-report-<source>`). Download it from the failed run's
**Summary → Artifacts** section. It looks like:

```json
{
  "version": 1,
  "checked": 5247,
  "fingerprint_hits": [...],
  "duplicate_buckets": [...],
  "allowlisted": [...],
  "exit_code": 1,
  "exit_reason": "duplicate_bytes",
  "release_tag": "v2026.05.0",
  "generated_at": "2026-05-11T..."
}
```

## 2. Branch on `exit_code`

### exit 3 — `_bake_complete.json` missing

Bake didn't complete. The detector refuses to validate a partial
directory because some byte-duplicates that would otherwise show up
in a complete bake might be missing.

- Check the bake step logs above the gate step.
- Common causes: Playwright timeout, OOM in the renderer, a Three.js
  JS error caught by `main().catch()` in `thumb_render.html`.
- Fix the bake → re-run.

### exit 2 — fingerprint hit

One or more thumbs match `blank_default.png` or `blank_default_grey.png`:

- `blank_default.png` → renderer ran with an empty material spec
  (Three.js `MeshPhysicalMaterial` defaults). Likely the substrate is
  empty for that material → check `client._scalars_for(source, mid)`
  and confirm `--repo-id` resolved to the expected HF dataset.
- `blank_default_grey.png` → pymat `_PBR_DEFAULTS` (`#CCCCCC`,
  metalness=0, roughness=0.5) leaked through. The substrate catalog
  is stale or pymat is being consulted as a fallback. See #285 / #376.

This is the same gate that has shipped since the original
`check_thumbs.py`; fix the substrate, re-bake.

### exit 1 — duplicate bytes

Two or more thumbs hashed to the same md5. Read the
`duplicate_buckets[].suggested` field — the heuristic suggests where
to look first:

| Suggested string                                   | Likely root cause                                                                                       |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `scalar collapse — check substrate`                | `count == total_baked` — every thumb is identical. `_scalars_for` returns `{}` for every material, or `--repo-id` points at an empty dataset. |
| `orchestrator closure bug — check _build_threejs_for` | `count == 3` AND all paths share a single source. Closure / loop variable mixup in `bake/preview/run.py`. The original #385 trigger. |
| `texture-binding regression`                       | Small bucket (2–5) but several distinct buckets across the bake. Renderer scalars differ but textures don't bind, so materials reduce to scalar-only. Check `thumb_render.html` material assembly. |
| `unknown`                                          | Inspect the paths manually. Often a small natural dedup (e.g. two upstream entries of the same material). If legitimate, allow-list it (step 3 below). |

### exit 0 — pass

Done. No action needed. (If `allowlisted` is non-empty, the gate did
trip but allow-list entries absorbed the buckets — review them
periodically with `--dump-stale-allowlist`.)

## 3. Legitimate dedup → add an allow-list entry

Some dedups are real: two upstream entries that ARE the same material
under different names, an ops-decision to ship a substrate gap as
scalar-only and ship the same render N times, etc.

Edit `bake/preview/utils/thumb_check_allow.yml`:

```yaml
allowed:
  - md5: "<the digest from duplicate_buckets[].md5>"
    reason: "Two upstream entries gpuopen/Foo and gpuopen/Foo_Variant are the same material; substrate dedup tracked in #999"
    ticket: "#999"
    until: "v2026.06.0"   # release tag at which this entry expires
```

All four fields are required — the loader rejects entries missing any.
The `until` field is a soft expiry: once the dataset's `release_tag`
catches up, the gate logs a warning (non-fatal) and ops should drop or
bump the entry.

## 4. Audit stale allow-list entries

Periodically (e.g. before a release):

```sh
uv run python bake/preview/utils/check_thumbs.py \
  --dump-stale-allowlist --release-tag v2026.05.0
```

Prints (and exits 0) every entry whose `until` is at or below the
current tag. Drop or bump them.

## 5. Re-run

Once you've fixed the root cause (or added the allow-list entry),
re-dispatch the failed workflow.
