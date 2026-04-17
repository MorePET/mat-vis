---
type: issue
state: open
created: 2026-04-16T17:27:56Z
updated: 2026-04-16T17:27:56Z
author: gerchowl
author_url: https://github.com/gerchowl
url: https://github.com/MorePET/mat-vis/issues/11
comments: 0
labels: milestone
assignees: none
milestone: none
projects: none
parent: none
children: none
synced: 2026-04-17T04:47:28.583Z
---

# [Issue 11]: [M7: Reference clients + output adapters](https://github.com/MorePET/mat-vis/issues/11)

## Goal

Non-Python consumers can fetch materials. mat's Material class
can emit Three.js / glTF output directly.

## Tasks

- [ ] `clients/js.mjs` — fetchMaterial() returns channel→Blob
- [ ] `clients/example.sh` — documented curl pattern (exists,
      needs real example values)
- [ ] MorePET/mat#35 — `Material.to_threejs()` adapter
- [ ] MorePET/mat#35 — `Material.to_gltf()` adapter
- [ ] MorePET/mat#35 — `Material.export_mtlx()` adapter
- [ ] Coordinate with @bernhard-42 on ocp_vscode accepting
      duck-typed `.to_threejs()` interface

## Test cases

```js
// JS client test (node or browser)
const mat = await fetchMaterial('ambientcg', '1k', 'Metal064', manifest);
assert(mat.color instanceof Blob);
assert(mat.color.size > 0);
```

```python
# Output adapter tests
steel = Material("ambientcg/Metal064", tier="1k")
d = steel.to_threejs()
assert d["type"] == "MeshPhysicalMaterial"
assert "map" in d  # base64 PNG
assert d["metalness"] == 1.0
```

## Gate

JS client works. to_threejs() works. ocp_vscode PR opened
or merged to accept duck-typed interface.

## Related

- mat-vis#3 — reference clients
- MorePET/mat#35 — output adapters
