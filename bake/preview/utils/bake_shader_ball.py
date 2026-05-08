"""Bake bernhard's shader_ball geometry into a static GLB asset.

Source: https://github.com/bernhard-42/vscode-ocp-cad-viewer/blob/main/ocp_vscode/utils.py
License: Apache-2.0, Copyright 2025 Bernhard Walter.

Run once. Output GLB is committed as a static asset; the function
itself isn't a runtime dep (no build123d in the bake container).
"""

from __future__ import annotations

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


def main():
    out_dir = Path("/Users/larsgerchow/Projects/mat-vis/.spike-thumb-node/assets")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "shader_ball.glb"

    print("# building shader_ball compound (build123d procedural)…")
    sb = create_shader_ball()
    print(f"# children: {len(sb.children)}, bbox: {sb.bounding_box()}")

    print(f"# exporting to {out_path}…")
    from build123d import export_gltf

    export_gltf(sb, str(out_path), unit=__import__("build123d").Unit.MM, binary=True)

    print(f"# done — {out_path.stat().st_size // 1024}KB")


if __name__ == "__main__":
    main()
