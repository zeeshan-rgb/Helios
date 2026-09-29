"""3D model files via trimesh: inspect | measure | convert | render | show.

Ported from Helios-main actions/model_3d.py (2026-07-08 consolidation). Numpy-only IO —
NO OpenGL/pyrender anywhere (pythonw host, 4 GB GPU budget). Blender GLB exports load as
a trimesh.Scene while single-mesh files load as a trimesh.Trimesh, so every path branches
on both. `render` is a pure-software painter's-algorithm rasterizer (numpy + PIL)
producing a 6-view contact sheet with a dimension banner — it exists so the brain can
LOOK at its own designs (read render.png with the native Read tool) and iterate.
`measure` is the QUANTITATIVE half of that critique loop: per-part dimensions, a center-
of-mass vs support-footprint stability check, floating/disconnected-part detection,
sampled wall thickness, and unit sanity — numbers the vision pass can't give.

`show` hands a GLB to the dashboard. This module runs in the MCP tool process (a child
of `claude -p`), NOT the app process — so instead of Helios-main's duck-typed player it
POSTs to the app's authed /model/show route (the voice-bridge pattern); when the app is
unreachable it degrades to a plain-text result. Returned text stays ASCII (cp1252).
"""
from __future__ import annotations

import base64
import json
import urllib.request
from pathlib import Path

_MESH_EXTS = (".glb", ".gltf", ".obj", ".stl", ".ply", ".off")
_CONVERT_TARGETS = ("glb", "gltf", "obj", "stl", "ply")
_WORK_ROOT = Path.home() / "Downloads" / "Helios Work"
_PNG_CAP = 2 * 1024 * 1024


def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1048576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _newest_mesh(folder: Path) -> Path | None:
    """Newest mesh, but GLB/GLTF FIRST when present: the GLB is the canonical viewer
    artifact, and mixing formats mid-loop flip-flops unit/axis conventions between calls
    (the controller-stand bug: measure read the mm STL, show shipped a 95 m GLB)."""
    files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in _MESH_EXTS]
    glbs = [f for f in files if f.suffix.lower() in (".glb", ".gltf")]
    return max(glbs or files, key=lambda f: f.stat().st_mtime, default=None)


def _resolve(raw: str) -> Path | None:
    """File path -> itself; folder -> its newest mesh; bare name -> that model folder
    inside the Helios Work sandbox."""
    if not raw:
        return None
    p = Path(raw).expanduser()
    if p.is_file():
        return p
    if p.is_dir():
        return _newest_mesh(p)
    cand = _WORK_ROOT / raw
    if cand.is_dir():
        return _newest_mesh(cand)
    return None


def _is_under(p: Path, root: Path) -> bool:
    try:
        return p.resolve().is_relative_to(root.resolve())
    except Exception:
        return False


def _stats(obj) -> dict:
    import trimesh
    if isinstance(obj, trimesh.Scene):
        geoms = list(obj.geometry.values())
        verts = sum(len(getattr(g, "vertices", [])) for g in geoms)
        faces = sum(len(getattr(g, "faces", [])) for g in geoms)
        tight = sum(1 for g in geoms if getattr(g, "is_watertight", False))
        extents = obj.extents if geoms else None
        return {"kind": f"Scene: {len(geoms)} meshes", "verts": verts, "faces": faces,
                "watertight": f"{tight}/{len(geoms)}", "extents": extents}
    return {"kind": "Single mesh", "verts": len(obj.vertices), "faces": len(obj.faces),
            "watertight": "yes" if obj.is_watertight else "no", "extents": obj.extents}


def _inspect_text(p: Path, obj) -> str:
    s = _stats(obj)
    if s["extents"] is not None:
        scale, _, note = _unit_ctx(p)
        raw = " x ".join(f"{v:.2f}" for v in s["extents"])
        mm = " x ".join(f"{v * scale:.0f}" for v in s["extents"])
        ext = f"{raw} (= {mm} mm; {note})"
    else:
        ext = "unknown"
    return (f"{p.name} - {p.suffix[1:].upper()}, {_fmt_size(p.stat().st_size)}. "
            f"{s['kind']}, {s['verts']:,} vertices, {s['faces']:,} faces. "
            f"Extents: {ext}. Watertight: {s['watertight']}.")


def _preview_png(folder: Path) -> str:
    """Newest small PNG beside the model (the Blender/tool render) as a data URI, else ''."""
    try:
        pngs = [f for f in folder.iterdir()
                if f.is_file() and f.suffix.lower() == ".png" and f.stat().st_size <= _PNG_CAP]
        if not pngs:
            return ""
        newest = max(pngs, key=lambda f: f.stat().st_mtime)
        return "data:image/png;base64," + base64.b64encode(newest.read_bytes()).decode("ascii")
    except Exception:
        return ""


# ── measure: quantitative design checks (the numeric half of the critique loop) ──
_MAX_PARTS_LISTED = 8
_WALL_RAY_SAMPLES = 100
_WALL_MAX_FACES = 150_000
_GAP_TOL_MM = 0.2


def _unit_ctx(p: Path):
    """(scale_to_mm, up_axis_index, note). GLB/GLTF are meters + Y-up (the viewer/export
    contract in the playbooks); mesh formats (STL/OBJ/PLY/OFF) are mm + Z-up (CAD/slicer
    convention). Conventions, not metadata — the note says which was assumed."""
    if p.suffix.lower() in (".glb", ".gltf"):
        return 1000.0, 1, "GLB convention: meters, Y-up"
    return 1.0, 2, f"{p.suffix[1:].upper()} convention: mm, Z-up"


def _world_parts(obj) -> list:
    """[(name, Trimesh in world space)] — scene children with transforms applied, or a
    single mesh split into connected components. These are the 'parts' measure reasons
    about."""
    import numpy as np
    import trimesh

    parts = []
    if isinstance(obj, trimesh.Scene):
        for node in obj.graph.nodes_geometry:
            T, gname = obj.graph[node]
            g = obj.geometry[gname]
            if not hasattr(g, "faces") or len(g.faces) == 0:
                continue
            V = trimesh.transformations.transform_points(np.asarray(g.vertices, float), T)
            parts.append((str(gname), trimesh.Trimesh(vertices=V, faces=np.asarray(g.faces, int),
                                                      process=False)))
    elif hasattr(obj, "faces") and len(obj.faces):
        try:
            comps = obj.split(only_watertight=False)
        except Exception:
            comps = []
        if len(comps) > 1:
            parts = [(f"part {i + 1}", c) for i, c in enumerate(comps)]
        else:
            parts = [("body", obj)]
    parts.sort(key=lambda nm: float(np.prod(nm[1].extents)), reverse=True)
    # OCC/CadQuery exports name nodes with UUIDs — meaningless to the critique; renumber
    # by size instead (biggest = part 1).
    import re
    uuidish = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
                         r"[0-9a-f]{12}(_\d+)?$", re.I)
    return [(f"part {i + 1}" if uuidish.match(name) else name, m)
            for i, (name, m) in enumerate(parts)]


def _com_mm(parts, scale) -> tuple:
    """(center_of_mass_mm, exact) — volume-weighted over watertight parts when possible,
    else the vertex mean (approx)."""
    import numpy as np
    coms, weights = [], []
    for _, m in parts:
        if m.is_watertight and abs(m.volume) > 1e-12:
            coms.append(m.center_mass)
            weights.append(abs(m.volume))
    if coms:
        com = np.average(np.asarray(coms), axis=0, weights=np.asarray(weights))
        return com * scale, True
    allv = np.vstack([m.vertices for _, m in parts])
    return allv.mean(axis=0) * scale, False


def _stability(parts, scale, up) -> str:
    """Center of mass vs the support footprint (convex hull of the lowest vertices).
    The check that catches 'looks fine in the render, tips over on a desk'."""
    import numpy as np
    allv = np.vstack([m.vertices for _, m in parts]) * scale
    horiz = [a for a in (0, 1, 2) if a != up]
    ground = allv[:, up].min()
    height = max(allv[:, up].max() - ground, 1e-9)
    tol = max(0.5, height * 0.005)
    feet = allv[allv[:, up] <= ground + tol][:, horiz]
    foot_ext = feet.max(axis=0) - feet.min(axis=0)
    foot_c = (feet.max(axis=0) + feet.min(axis=0)) / 2.0
    com, exact = _com_mm(parts, scale)
    com2, com_h = com[horiz], (com[up] - ground) / height
    head = (f"footprint {foot_ext[0]:.0f} x {foot_ext[1]:.0f} mm; center of mass at "
            f"{com_h * 100:.0f}% of height{'' if exact else ' (approx: not watertight)'}")

    margin = None
    try:
        from scipy.spatial import ConvexHull
        hull = ConvexHull(feet)
        margin = -float(np.max(hull.equations[:, :2] @ com2 + hull.equations[:, 2]))
        radius = float(np.max(np.linalg.norm(feet[hull.vertices] - foot_c, axis=1)))
    except Exception:                       # degenerate support (point/line) or no scipy
        half = foot_ext / 2.0
        d = np.abs(com2 - foot_c)
        margin = float(np.min(half - d))
        radius = float(max(np.linalg.norm(half), 1e-9))
    if min(foot_ext) < 1.0:
        verdict = "knife-edge contact - it cannot stand on this"
    elif margin <= 0:
        verdict = (f"TIPS OVER - the center of mass lands {-margin:.1f} mm OUTSIDE the "
                   f"footprint; widen the base or pull mass back")
    elif margin < 0.15 * radius:
        verdict = (f"borderline ({margin:.1f} mm of margin, {margin / radius * 100:.0f}% "
                   f"of footprint radius) - a nudge tips it; widen the base")
    else:
        verdict = f"stable ({margin:.1f} mm of margin, {margin / radius * 100:.0f}% of footprint radius)"
    ratio = max(foot_ext) / height
    return f"{head}. Verdict: {verdict}. Base/height ratio {ratio:.2f}."


def _connectivity(parts, scale) -> str:
    """Union-find over part AABBs: parts whose boxes touch nothing are floating —
    the classic 'cradle hovering above the column' build bug. AABB-based, so touching
    boxes with non-touching meshes can pass; treat a warning as definite, a pass as
    probable."""
    import numpy as np
    n = len(parts)
    if n == 1:
        _, m = parts[0]
        solid = "watertight solid" if m.is_watertight else "single connected mesh (not watertight)"
        return f"one part - {solid}."
    lo = [m.bounds[0] * scale for _, m in parts]
    hi = [m.bounds[1] * scale for _, m in parts]
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    gaps = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            g = np.maximum(0.0, np.maximum(lo[i] - hi[j], lo[j] - hi[i]))
            gaps[i][j] = gaps[j][i] = float(np.linalg.norm(g))
            if gaps[i][j] <= _GAP_TOL_MM:
                parent[find(i)] = find(j)
    islands = {}
    for i in range(n):
        islands.setdefault(find(i), []).append(i)
    if len(islands) == 1:
        return f"{n} parts, all in contact (AABB check)."
    groups = sorted(islands.values(), key=len, reverse=True)
    warn = []
    for grp in groups[1:]:
        names = ", ".join(parts[i][0] for i in grp[:3])
        nearest = min(gaps[i][j] for i in grp for j in range(n) if j not in grp)
        warn.append(f"[{names}] floats {nearest:.1f} mm from everything else")
    return (f"WARNING - {len(islands)} disconnected groups: " + "; ".join(warn)
            + ". Floating parts = build bug (wrong offset/rotation).")


def _wall_sample(parts, scale) -> str:
    """Sampled local thickness of the largest watertight part: rays cast inward from face
    centroids, first exit = thickness there. ~100 samples, pure-numpy ray casting."""
    import numpy as np
    cands = [(nm, m) for nm, m in parts if m.is_watertight and len(m.faces) <= _WALL_MAX_FACES]
    if not cands:
        return ""
    name, m = max(cands, key=lambda nm: abs(nm[1].volume))
    try:
        idx = np.linspace(0, len(m.faces) - 1, min(_WALL_RAY_SAMPLES, len(m.faces))).astype(int)
        normals = m.face_normals[idx]
        origins = m.triangles_center[idx] - normals * (0.05 / scale)
        hits, ray_ids, _ = m.ray.intersects_location(origins, -normals, multiple_hits=False)
        if len(hits) < 5:
            return ""
        t = np.linalg.norm(hits - origins[ray_ids], axis=1) * scale
        line = (f"local thickness of '{name}' (sampled {len(t)} spots): "
                f"min {t.min():.1f} mm, median {np.median(t):.1f} mm")
        if t.min() < 2.0:
            line += " - UNDER the 2 mm plausibility floor; thicken or it reads as paper"
        return line + "."
    except Exception:
        return ""


def _measure_text(p: Path, obj) -> str:
    import numpy as np
    scale, up, note = _unit_ctx(p)
    parts = _world_parts(obj)
    if not parts:
        return f"{p.name}: no triangle geometry to measure."
    allv = np.vstack([m.vertices for _, m in parts])
    ext = (allv.max(axis=0) - allv.min(axis=0)) * scale
    horiz = [a for a in (0, 1, 2) if a != up]
    axes = "XYZ"
    faces = sum(len(m.faces) for _, m in parts)

    lines = [f"{p.name} measured in mm ({note}; height = {axes[up]}).",
             f"OVERALL: W {ext[horiz[0]]:.0f} x D {ext[horiz[1]]:.0f} x H {ext[up]:.0f} mm; "
             f"{len(parts)} part(s), {faces:,} faces.",
             f"STABILITY: {_stability(parts, scale, up)}",
             f"CONNECTIVITY: {_connectivity(parts, scale)}"]
    walls = _wall_sample(parts, scale)
    if walls:
        lines.append(f"WALLS: {walls}")

    shown = parts[:_MAX_PARTS_LISTED]
    plines = []
    for name, m in shown:
        e = m.extents * scale
        bottom = m.bounds[0][up] * scale - allv[:, up].min() * scale
        plines.append(f"  - {name}: {e[horiz[0]]:.0f} x {e[horiz[1]]:.0f} x {e[up]:.0f} mm, "
                      f"underside {bottom:.0f} mm above ground"
                      + ("" if m.is_watertight else " (not watertight)"))
    if len(parts) > len(shown):
        plines.append(f"  - ... and {len(parts) - len(shown)} smaller part(s)")
    lines.append("PARTS (W x D x H):\n" + "\n".join(plines))

    big = float(ext.max())
    if p.suffix.lower() in (".glb", ".gltf") and big > 5000:
        lines.append(f"UNITS: {big / 1000:.0f} m tall?! Almost certainly still in mm - "
                     "rescale x0.001 before the GLB export (see the 3D playbook).")
    elif big < 10:
        lines.append("UNITS: under 1 cm overall - if this should be desk-scale, the build "
                     "units are off.")
    return "\n".join(lines)


# ── software renderer (no GL) ────────────────────────────────────────────────
_RENDER_VIEWS = (("iso", 35, 25), ("front", 0, 8), ("side", 90, 8),
                 ("iso-rear", 215, 25), ("top", 0, 88), ("bottom", 0, -80))
_MAX_RENDER_FACES = 300_000


def _tri_soup(obj):
    """World-space triangles + one RGB per mesh -> (list of (V, F, color))."""
    import numpy as np
    import trimesh

    def color_of(g):
        try:
            vis = g.visual
            mat = getattr(vis, "material", None)
            c = getattr(mat, "baseColorFactor", None) if mat is not None else None
            if c is None and mat is not None:
                c = getattr(mat, "diffuse", None)
            if c is None:
                fc = getattr(vis, "face_colors", None)
                if fc is not None and len(fc):
                    c = np.median(np.asarray(fc), axis=0)
            if c is None:
                return np.array([185.0, 185.0, 192.0])
            c = np.asarray(c, dtype=float)[:3]
            if c.max() <= 1.001:
                c = c * 255.0
            # Critique renders must stay legible: lift near-black materials toward a
            # midtone so the lambert shading (i.e. the SHAPE) is always visible.
            return 55.0 + c * 0.78
        except Exception:
            return np.array([185.0, 185.0, 192.0])

    out = []
    if isinstance(obj, trimesh.Scene):
        for node in obj.graph.nodes_geometry:
            T, gname = obj.graph[node]
            g = obj.geometry[gname]
            if not hasattr(g, "faces") or len(g.faces) == 0:
                continue
            V = trimesh.transformations.transform_points(np.asarray(g.vertices, float), T)
            out.append((V, np.asarray(g.faces, int), color_of(g)))
    elif hasattr(obj, "faces") and len(obj.faces):
        out.append((np.asarray(obj.vertices, float), np.asarray(obj.faces, int),
                    color_of(obj)))
    return out


def _decimate_soup(soup, target_total: int):
    """Vertex-clustering decimation, pure numpy: quantize vertices to a grid, merge
    clusters (mean position), drop degenerate faces. Crude — fillets facet a little —
    but render-grade, and it keeps the LOOK step alive on dense CadQuery tessellations
    (a default-tolerance export is easily 400k+ faces)."""
    import numpy as np
    total = sum(len(F) for _, F, _ in soup)
    out = []
    for V, F, col in soup:
        target = max(64, int(len(F) * target_total / total))
        if len(F) <= target:
            out.append((V, F, col))
            continue
        lo = V.min(axis=0)
        pitch = max((V.max(axis=0) - lo).max(), 1e-9) / 128.0
        V2, F2 = V, F
        for _ in range(8):
            q = np.floor((V - lo) / pitch).astype(np.int64)
            _, inv = np.unique(q, axis=0, return_inverse=True)
            f = inv[F]
            keep = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 2] != f[:, 0])
            n = int(inv.max()) + 1
            sums = np.zeros((n, 3))
            counts = np.zeros((n, 1))
            np.add.at(sums, inv, V)
            np.add.at(counts, inv, 1)
            V2, F2 = sums / counts, f[keep]
            if len(F2) <= target:
                break
            pitch *= 1.5
        out.append((V2, F2, col))
    return out


def _render_sheet(obj, out_png: Path, view_px: int = 540, caption: str = ""):
    """Shaded painter's-algorithm render, 6 labelled views (front AND rear orbit) on one
    sheet, with a dimension caption so the vision critique can judge real-world scale.
    Returns (error_or_None, note) — note says when the mesh was decimated to render."""
    import numpy as np
    from PIL import Image, ImageDraw

    note = ""
    soup = _tri_soup(obj)
    if not soup:
        return "nothing to render (no triangle geometry)", note
    total = sum(len(F) for _, F, _ in soup)
    if total > _MAX_RENDER_FACES:
        soup = _decimate_soup(soup, _MAX_RENDER_FACES // 2)
        slim = sum(len(F) for _, F, _ in soup)
        note = (f" Mesh decimated {total:,} -> {slim:,} faces FOR THIS RENDER ONLY "
                f"(model file untouched; small facets are the decimation, not the model).")
        if caption:
            caption += f"   [render decimated to {slim:,} faces]"

    center = np.mean([V.mean(axis=0) for V, _, _ in soup], axis=0)
    light = np.array([0.35, 0.55, 0.75])
    light = light / np.linalg.norm(light)
    tiles = []
    for label, azim, elev in _RENDER_VIEWS:
        az, el = np.radians(azim), np.radians(elev)   # +elev looks DOWN from above
        ca, sa, ce, se = np.cos(az), np.sin(az), np.cos(el), np.sin(el)
        Ry = np.array([[ca, 0, sa], [0, 1, 0], [-sa, 0, ca]])
        Rx = np.array([[1, 0, 0], [0, ce, -se], [0, se, ce]])
        R = Rx @ Ry

        tris, depths, shades = [], [], []
        for V, F, col in soup:
            Vr = (V - center) @ R.T
            t = Vr[F]                                    # (m, 3 verts, 3 xyz)
            n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
            nlen = np.linalg.norm(n, axis=1)
            keep = (nlen > 1e-12) & (n[:, 2] > 0)        # backface cull (camera at +Z)
            if not keep.any():
                continue
            t, n = t[keep], (n[keep].T / nlen[keep]).T
            lam = 0.35 + 0.65 * np.clip(n @ light, 0, 1)
            tris.append(t)
            depths.append(t[:, :, 2].mean(axis=1))
            shades.append(np.clip(lam[:, None] * col[None, :], 0, 255).astype(np.uint8))
        if not tris:
            tiles.append(None)
            continue
        T3 = np.concatenate(tris)
        depth = np.concatenate(depths)
        shade = np.concatenate(shades)

        xy = T3[:, :, :2] * np.array([1.0, -1.0])        # PIL's y grows downward
        lo, hi = xy.reshape(-1, 2).min(axis=0), xy.reshape(-1, 2).max(axis=0)
        span = max((hi - lo).max(), 1e-9)
        scale = (view_px * 0.86) / span
        off = (view_px - (hi - lo) * scale) / 2.0
        pix = (xy - lo) * scale + off

        img = Image.new("RGB", (view_px, view_px), (244, 245, 247))
        drw = ImageDraw.Draw(img)
        for i in np.argsort(depth):                      # far first (camera looks -Z)
            drw.polygon([tuple(p) for p in pix[i]], fill=tuple(shade[i]))
        drw.text((10, 8), label, fill=(60, 65, 75))
        tiles.append(img)

    banner = 30 if caption else 0
    sheet = Image.new("RGB", (view_px * 3 + 6, view_px * 2 + 3 + banner), (200, 202, 208))
    for k, tile in enumerate(tiles):
        if tile is not None:
            sheet.paste(tile, ((k % 3) * (view_px + 3), (k // 3) * (view_px + 3)))
    if caption:
        drw = ImageDraw.Draw(sheet)
        drw.rectangle([0, view_px * 2 + 3, sheet.width, sheet.height], fill=(38, 42, 50))
        drw.text((10, view_px * 2 + 11), caption, fill=(235, 237, 240))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(str(out_png))
    return None, note


def _convert(obj, src: Path, target: Path) -> str:
    import trimesh
    note = ""
    out = obj
    if isinstance(obj, trimesh.Scene) and target.suffix.lower() in (".stl", ".ply"):
        out = obj.to_geometry()            # single-mesh formats: apply transforms, merge
        note = " Note: scene flattened to one mesh; materials dropped."
    target.parent.mkdir(parents=True, exist_ok=True)
    out.export(str(target))
    return (f"Converted {src.name} -> {target.name} "
            f"({_fmt_size(target.stat().st_size)}) in {target.parent}.{note}")


def _post_show(payload: dict) -> dict | None:
    """Hand the GLB to the app process (registry + model3d SSE) over the authed local
    route — this tool runs in the MCP child process, so it cannot touch the Hub directly.
    Returns the registry entry, or None when the app is down/unreachable."""
    from helios import conf
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(conf.BASE_URL + "/model/show", data=data, headers={
            "Content-Type": "application/json", "X-Auth-Token": conf.auth_token() or ""})
        with urllib.request.urlopen(req, timeout=10) as r:
            out = json.loads(r.read() or b"{}")
        return out if out.get("id") else None
    except Exception:
        return None


def _show(p: Path, obj) -> str:
    glb = p
    note = ""
    if p.suffix.lower() != ".glb":
        # Viewer serves GLB only (self-contained). Write it INSIDE the sandbox so `show`
        # never needs approval anywhere else.
        glb = (p.with_suffix(".glb") if _is_under(p, _WORK_ROOT)
               else _WORK_ROOT / "_viewer" / (p.stem + ".glb"))
        glb.parent.mkdir(parents=True, exist_ok=True)
        out = obj
        scale, _, _ = _unit_ctx(p)
        if scale == 1.0:
            # mm/Z-up source (STL/OBJ/PLY) -> the viewer contract is meters/Y-up.
            # Without this a bare-STL show ships a 100-metre sideways model.
            import numpy as np
            import trimesh
            M = trimesh.transformations.rotation_matrix(-np.pi / 2, (1, 0, 0))
            M[:3, :3] *= 0.001
            out = obj.copy()
            out.apply_transform(M)
            note = " (normalized mm/Z-up -> m/Y-up for the viewer)"
        out.export(str(glb))
    s = _stats(obj)
    reg = _post_show({"path": str(glb), "verts": s["verts"], "faces": s["faces"],
                      "png": _preview_png(p.parent)})
    if not reg:
        return f"No dashboard reachable - the model file is ready at {glb}.{note}"
    return (f"Model '{reg['name']}' is on the dashboard - preview card with an "
            f"Open-in-3D-viewer button.{note}")


def model_3d(parameters) -> str:
    args = dict(parameters or {})
    action = str(args.get("action", "")).strip().lower()
    raw = str(args.get("path", "")).strip()

    try:
        import trimesh  # lazy: a missing optional dep must never break the other tools
    except ImportError:
        return "The 3D library (trimesh) is not installed - run: pip install trimesh"

    p = _resolve(raw)
    if p is None:
        return (f"No 3D model found for '{raw}'. Pass a mesh file "
                f"({', '.join(_MESH_EXTS)}), its folder, or a model-folder name inside "
                f"{_WORK_ROOT}.")

    try:
        obj = trimesh.load(str(p))
    except Exception as e:
        return f"Could not load {p.name}: {e}"

    if action == "inspect":
        return _inspect_text(p, obj)

    if action == "measure":
        try:
            return _measure_text(p, obj)
        except Exception as e:
            return f"Measure failed on {p.name}: {type(e).__name__}: {e}"

    if action == "convert":
        fmt = str(args.get("format", "")).strip().lower().lstrip(".")
        dest = str(args.get("destination", "")).strip()
        if dest:
            target = Path(dest).expanduser()
            if not target.suffix:                     # a folder: keep the stem
                target = target / (p.stem + "." + (fmt or "glb"))
        elif fmt:
            if fmt not in _CONVERT_TARGETS:
                return (f"Unsupported target format '{fmt}'. "
                        f"Choose one of: {', '.join(_CONVERT_TARGETS)}.")
            target = p.with_suffix("." + fmt)
        else:
            return ("Tell me the target: pass format (glb/gltf/obj/stl/ply) "
                    "or a destination path.")
        return _convert(obj, p, target)

    if action == "render":
        out_png = p.parent / "render.png"
        try:
            import numpy as np
            scale, up, note = _unit_ctx(p)
            ext = np.asarray(obj.extents, float) * scale
            horiz = [a for a in (0, 1, 2) if a != up]
            caption = (f"{p.stem}   W {ext[horiz[0]]:.0f} x D {ext[horiz[1]]:.0f} x "
                       f"H {ext[up]:.0f} mm   ({note})")
        except Exception:
            caption = ""
        err, note = _render_sheet(obj, out_png, caption=caption)
        if err:
            return f"Render failed: {err}"
        return (f"Rendered 6 views (iso/front/side + rear/top/bottom, dims in the banner) "
                f"to {out_png}.{note} Critique loop: READ {out_png} with your Read tool "
                f"for the visual critique, and model_3d action=measure for stability/"
                f"connections/thickness numbers - fix what either flags BEFORE showing.")

    if action == "show":
        return _show(p, obj)

    return f"Unknown action '{action}'. Use inspect, measure, convert, render, or show."
