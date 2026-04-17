---
type: issue
state: open
created: 2026-04-16T17:09:54Z
updated: 2026-04-16T19:53:48Z
author: gerchowl
author_url: https://github.com/gerchowl
url: https://github.com/MorePET/mat-vis/issues/3
comments: 0
labels: none
assignees: none
milestone: none
projects: none
parent: none
children: none
synced: 2026-04-17T04:47:31.422Z
---

# [Issue 3]: [Ship reference clients (Python, JS, Rust, shell) alongside release assets](https://github.com/MorePET/mat-vis/issues/3)

## Context

The rowmap + HTTP range-read pattern is the entire client API:

1. Fetch rowmap JSON (~few hundred KB)
2. Look up material ID → byte offset + length per channel
3. HTTP Range-read into the Parquet → raw PNG bytes

This is 10–50 lines in any language. No pyarrow, no parquet
reader, no binary deps.

Each shim exposes the same three operations:

1. **get_manifest(tag)** — fetch release-manifest.json, discover
   URLs for all sources × tiers
2. **fetch(source, id, tier)** — pull textures, return channel →
   bytes
3. **rowmap_entry(source, id, tier)** — return raw offset/length
   info so the consumer can do the fetch themselves with whatever
   HTTP client they prefer

## Reference clients

### Python (`clients/python.py` / `pymat.vis.shim`)

~150 lines. Ships inside the mat wheel. Also importable
standalone for users who don't want the full Material hierarchy.

```python
from pymat.vis import shim

# Full fetch — returns channel → PNG bytes
textures = shim.fetch("ambientcg", "Metal_Brushed_001", tier="1k")
textures["color"]  # PNG bytes

# Just the pointer — for DIY consumers
entry = shim.rowmap_entry("ambientcg", "Metal_Brushed_001", tier="1k")
# → {"color": {"offset": 102400, "length": 51200}, ...}
# Hand this to curl, JS fetch(), Rust reqwest, whatever
```

### JavaScript (`clients/js.mjs`)

~50 lines. No npm package — import from repo or copy-paste.

```js
import { getManifest, fetchMaterial, rowmapEntry } from './clients/js.mjs'

const manifest = await getManifest('v2026.04.0')
const textures = await fetchMaterial('ambientcg', '1k', 'Metal_Brushed_001', manifest)
textures.color  // Blob

// Or just get the pointer
const entry = await rowmapEntry('ambientcg', '1k', 'Metal_Brushed_001', manifest)
// → {color: {offset: 102400, length: 51200}, ...}
// Consumer does their own fetch()
```

### Shell (`clients/example.sh`)

```bash
# Fetch manifest
curl -sL https://github.com/MorePET/mat-vis/releases/download/v2026.04.0/release-manifest.json

# Fetch one texture channel (offset + length from rowmap)
OFFSET=102400; LENGTH=51200
curl -sH "Range: bytes=${OFFSET}-$((OFFSET+LENGTH-1))" \
  https://github.com/.../mat-vis-ambientcg-1k.parquet \
  -o Metal_Brushed_001_color.png
```

### Rust (`clients/rust.rs`)

~80 lines. Reference impl. Not a crate unless demand justifies it.

## Versioning

Co-released with each calver tag. Client code + rowmap format +
data always match. No separate package registries.

## Acceptance

- [ ] Python shim: get_manifest, fetch, rowmap_entry
- [ ] JS shim: getManifest, fetchMaterial, rowmapEntry
- [ ] Shell example with real values from first release
- [ ] Rust reference impl (can defer to post-M5)
- [ ] All shims expose the same three operations
- [ ] README documents all four language paths
- [ ] release.yml includes clients/ in release assets

## Related

- [MorePET/mat#35](https://github.com/MorePET/mat/issues/35)
  — Material.vis + adapters (Python consumer side)
- ADR-0001 — rowmap format spec
- ADR-0004 — cache layout (Python shim implements this)
