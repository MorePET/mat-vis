"""Bake bernhard's shader_ball geometry into a static GLB asset.

Source: https://github.com/bernhard-42/vscode-ocp-cad-viewer/blob/main/ocp_vscode/utils.py
License: Apache-2.0, Copyright 2025 Bernhard Walter.

Run once. Output GLB is committed as a static asset; the function
itself isn't a runtime dep (no build123d in the bake container).

Post-processes the build123d export to add TEXCOORD_0 (spherical UVs
from per-vertex world position). build123d's ``export_gltf`` does not
emit UVs; without them ``MeshPhysicalMaterial`` cannot sample any
texture map and matte dielectrics collapse to scalar-only renders
(#385 / #393). Spherical projection from world position is the
algorithm-equivalent of the JS load-time workaround the renderer
shipped pre-#393 — baking it into the GLB lets us drop that block.
"""

from __future__ import annotations

import struct
from pathlib import Path

from build123d import (
    Align,
    CenterArc,
    Compound,
    Cone,
    Cylinder,
    Pos,
    Rot,
    SlotArc,
    Sphere,
    Triangle,
    extrude,
    fillet,
)


def create_shader_ball(name: str = "shader_ball") -> Compound:
    """Bernhard Walter's shader-ball geometry, ported verbatim from
    ocp_vscode.utils. Apache-2.0.

    Returns a build123d Compound with 4 children: outer hollow sphere,
    inner display ball, cylindrical base with rings, central inner sphere.
    Bbox ~22×22×22. Designed for material preview at 30-unit grid spacing.
    """
    ccm = (Align.CENTER, Align.CENTER, Align.MIN)
    cM = (Align.CENTER, Align.MAX)
    r1, r2, r3, h = 10, 8.5, 8, 2
    s1, s2, s3 = Sphere(r1), Sphere(r2), Sphere(r3)
    s = Rot(0, 60, 0) * (s1 - s2 - Pos(0, 0, 14.3) * s3 - Pos(0, 0, -14.3) * s3)
    d = -r1 + 0.0
    c1 = Pos(0, 0, d - h) * Cylinder(7, h, align=ccm)
    c2 = Pos(0, 0, d - 0.1) * Cylinder(6, h, align=ccm)
    c3 = Pos(0, 0, d - 0.2) * Cylinder(5, h, align=ccm)
    c1 = fillet(c1.edges(), 0.2)
    c = c1 - c2 - c3
    b1 = Pos(0, 0, d - h) * (Cylinder(11, 2, align=ccm) - Cylinder(7.4, 2, align=ccm))
    sl1 = Pos(0, 0, d) * SlotArc(CenterArc((0, 0, 0), 10.0, 270 - 35, 70), 0.4)
    sl2 = Pos(0, 0, d) * SlotArc(CenterArc((0, 0, 0), 8.0, 270 - 25, 50), 0.4)
    sl3 = Pos(0, 0, d + 1e-2) * SlotArc(CenterArc((0, 0, 0), 9.0, 270 - 15, 30), 0.4)
    a1 = extrude(sl1, 0.2)
    a1 = fillet(a1.edges().group_by()[-1], 0.05)
    a2 = extrude(sl2, 0.2)
    a2 = fillet(a2.edges().group_by()[-1], 0.05)
    a3 = extrude(sl3, -0.2)
    a3 = fillet(a3.edges().group_by()[0], 0.05)
    b1 = b1 - a3 + a1 + a2
    t = Pos(0, 0, d) * Triangle(a=6, b=12, c=12, align=cM)
    h_ext, n = 4, 5

    def mask(r):
        return Pos(0, 0, -r1) * Cylinder(r, 20, align=ccm)

    b2 = Rot(0, 0, 180) * extrude(t, h_ext)
    cn = Pos(0, 0, -r1 + 2.8) * Cone(7, 15, 4, align=ccm)
    b = b1 + (b2 & mask(10) - cn)
    for i in range(1, n):
        for sign in [-1, 1]:
            b += (
                Rot(0, 0, 180 + sign * i * 28.6) * extrude(t, h_ext - 1.5 - i * h_ext / n / 2)
            ) & mask(10 - i * 0.4)
    b -= Cylinder(7.4, 20)
    b &= Cylinder(11, 50)
    b = b.solid()
    b = fillet(b.edges(), 0.1)
    s4 = Rot(0, 0, 90) * Sphere(r2 - 1)
    compound = Compound([b, s, c, s4])
    compound.label = name
    return compound


def _add_spherical_uvs(gltf_path: Path) -> tuple[int, int]:
    """Post-process a GLB: append TEXCOORD_0 (spherical UVs) on every
    primitive that lacks one. Returns ``(primitives_processed,
    primitives_already_had_uvs)``.

    Algorithm matches the JS load-time fallback in ``thumb_render.html``
    (pre-#393 commit 0822515) so the visual output is invariant before
    and after the GLB is regenerated:

        u = 0.5 + atan2(z, x) / (2π)
        v = 0.5 - asin(y / r) / π   where r = ||(x,y,z)||
    """
    import math

    from pygltflib import (
        FLOAT,
        VEC2,
        Accessor,
        BufferView,
        GLTF2,
    )

    gltf = GLTF2().load(gltf_path)
    bin_blob = bytearray(gltf.binary_blob() or b"")
    n_with_uvs = 0
    n_added = 0

    for mesh in gltf.meshes:
        for primitive in mesh.primitives:
            if primitive.attributes.TEXCOORD_0 is not None:
                n_with_uvs += 1
                continue

            # Read POSITION attribute
            pos_acc = gltf.accessors[primitive.attributes.POSITION]
            pos_view = gltf.bufferViews[pos_acc.bufferView]
            pos_offset = (pos_view.byteOffset or 0) + (pos_acc.byteOffset or 0)
            count = pos_acc.count
            positions = struct.unpack_from(f"<{count * 3}f", bin_blob, pos_offset)

            # Compute spherical UVs
            uvs = bytearray(count * 2 * 4)
            for i in range(count):
                x, y, z = positions[i * 3 : i * 3 + 3]
                r = math.sqrt(x * x + y * y + z * z) or 1.0
                u = 0.5 + math.atan2(z, x) / (2 * math.pi)
                v = 0.5 - math.asin(max(-1.0, min(1.0, y / r))) / math.pi
                struct.pack_into("<2f", uvs, i * 2 * 4, u, v)

            # Append to binary blob, register bufferView + accessor
            uv_byte_offset = len(bin_blob)
            bin_blob.extend(uvs)
            # Pad to 4-byte alignment
            while len(bin_blob) % 4:
                bin_blob.append(0)

            buffer_view_idx = len(gltf.bufferViews)
            gltf.bufferViews.append(
                BufferView(
                    buffer=pos_view.buffer,
                    byteOffset=uv_byte_offset,
                    byteLength=count * 2 * 4,
                )
            )
            accessor_idx = len(gltf.accessors)
            gltf.accessors.append(
                Accessor(
                    bufferView=buffer_view_idx,
                    componentType=FLOAT,
                    count=count,
                    type=VEC2,
                )
            )
            primitive.attributes.TEXCOORD_0 = accessor_idx
            n_added += 1

    # Update buffer length to match new blob
    gltf.buffers[0].byteLength = len(bin_blob)
    gltf.set_binary_blob(bytes(bin_blob))
    gltf.save_binary(gltf_path)
    return n_added, n_with_uvs


def main():
    out_dir = Path(__file__).resolve().parents[1] / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "shader_ball.glb"

    print("# building shader_ball compound (build123d procedural)…")
    sb = create_shader_ball()
    print(f"# children: {len(sb.children)}, bbox: {sb.bounding_box()}")

    print(f"# exporting to {out_path}…")
    from build123d import Unit, export_gltf

    export_gltf(sb, str(out_path), unit=Unit.MM, binary=True)
    pre_size = out_path.stat().st_size

    print("# post-processing GLB to add spherical UVs (#393)…")
    n_added, n_existing = _add_spherical_uvs(out_path)
    post_size = out_path.stat().st_size
    print(f"# UVs added to {n_added} primitive(s); {n_existing} already had UVs")
    print(f"# size: {pre_size // 1024}KB → {post_size // 1024}KB")


if __name__ == "__main__":
    main()
