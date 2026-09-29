"""helios.model_3d — the trimesh pipeline (inspect/measure/convert/render/show), no UI
needed. measure is the quantitative critique: stability, floaters, walls, units.
Ported from Helios-main tests/test_model_tool.py; `show` now hands off over HTTP
(_post_show), mocked here — no app process involved."""
import pytest

trimesh = pytest.importorskip("trimesh")

from helios import model_3d as m3d               # noqa: E402


@pytest.fixture
def box_glb(tmp_path):
    p = tmp_path / "box.glb"
    trimesh.creation.box(extents=(1, 2, 3)).export(str(p))
    return p


@pytest.fixture
def two_box_glb(tmp_path):
    s = trimesh.Scene()
    s.add_geometry(trimesh.creation.box(), geom_name="a")
    b = trimesh.creation.box()
    b.apply_translation((2.0, 0.0, 0.0))
    s.add_geometry(b, geom_name="b")
    p = tmp_path / "two.glb"
    s.export(str(p))
    return p


@pytest.fixture
def no_dashboard(monkeypatch):
    """App unreachable: _post_show returns None (the degraded text-only path)."""
    monkeypatch.setattr(m3d, "_post_show", lambda payload: None)


# ---------------- inspect ----------------
def test_inspect_box(box_glb):
    out = m3d.model_3d({"action": "inspect", "path": str(box_glb)})
    assert "box.glb" in out
    assert "8 vertices" in out and "12 faces" in out
    assert "1.00 x 2.00 x 3.00" in out


def test_inspect_scene_two_boxes(two_box_glb):
    # Blender-style GLB: trimesh loads it as a Scene, not a Trimesh — the #1 pipeline risk.
    assert isinstance(trimesh.load(str(two_box_glb)), trimesh.Scene)
    out = m3d.model_3d({"action": "inspect", "path": str(two_box_glb)})
    assert "2 meshes" in out
    assert "16 vertices" in out and "24 faces" in out


def test_missing_path_is_friendly():
    out = m3d.model_3d({"action": "inspect", "path": "C:/nope/definitely_missing.glb"})
    assert "No 3D model found" in out


# ---------------- convert ----------------
def test_convert_glb_to_stl(box_glb):
    out = m3d.model_3d({"action": "convert", "path": str(box_glb), "format": "stl"})
    stl = box_glb.with_suffix(".stl")
    assert stl.exists() and "box.stl" in out
    assert len(trimesh.load(str(stl)).faces) > 0


def test_convert_scene_to_stl_flattens(two_box_glb):
    out = m3d.model_3d({"action": "convert", "path": str(two_box_glb), "format": "stl"})
    stl = two_box_glb.with_suffix(".stl")
    assert stl.exists()
    assert "materials dropped" in out            # flatten note surfaced to the brain
    assert len(trimesh.load(str(stl)).faces) == 24


def test_convert_needs_target(box_glb):
    out = m3d.model_3d({"action": "convert", "path": str(box_glb)})
    assert "format" in out or "destination" in out


# ---------------- measure ----------------
def _stand_glb(tmp_path):
    """Stable two-part 'stand': wide flat base + column standing on it, meters, Y-up
    (GLB convention). Parts touch: column bottom = base top."""
    s = trimesh.Scene()
    base = trimesh.creation.box(extents=(0.16, 0.02, 0.16))       # W x H x D, Y is up
    base.apply_translation((0, 0.01, 0))                          # sits on ground (y=0)
    col = trimesh.creation.box(extents=(0.03, 0.20, 0.03))
    col.apply_translation((0, 0.02 + 0.10, 0))                    # standing on the base
    s.add_geometry(base, geom_name="base")
    s.add_geometry(col, geom_name="column")
    p = tmp_path / "stand.glb"
    s.export(str(p))
    return p


def test_measure_stable_stand(tmp_path):
    out = m3d.model_3d({"action": "measure", "path": str(_stand_glb(tmp_path))})
    assert "OVERALL: W 160 x D 160 x H 220 mm" in out
    assert "stable" in out and "TIPS" not in out
    assert "all in contact" in out
    assert "base" in out and "column" in out


def test_measure_flags_floating_part(two_box_glb):
    # the fixture's two unit boxes sit 1 m apart — a disconnected build
    out = m3d.model_3d({"action": "measure", "path": str(two_box_glb)})
    assert "WARNING" in out and "floats" in out


def test_measure_flags_tipping(tmp_path):
    # tiny foot, huge mass cantilevered off to +X: COM far outside the footprint
    s = trimesh.Scene()
    foot = trimesh.creation.box(extents=(0.02, 0.02, 0.02))
    foot.apply_translation((0, 0.01, 0))
    arm = trimesh.creation.box(extents=(0.30, 0.02, 0.02))
    arm.apply_translation((0.14, 0.03, 0))                        # touches foot's top corner
    s.add_geometry(foot, geom_name="foot")
    s.add_geometry(arm, geom_name="arm")
    p = tmp_path / "tippy.glb"
    s.export(str(p))
    out = m3d.model_3d({"action": "measure", "path": str(p)})
    assert "TIPS OVER" in out or "OUTSIDE" in out


def test_measure_flags_wrong_units(tmp_path):
    # a "250 m tall" GLB = builder forgot the mm -> m rescale
    p = tmp_path / "huge.glb"
    trimesh.creation.box(extents=(160.0, 250.0, 160.0)).export(str(p))
    out = m3d.model_3d({"action": "measure", "path": str(p)})
    assert "UNITS" in out and "rescale" in out


def test_measure_single_mesh_is_one_part(box_glb):
    out = m3d.model_3d({"action": "measure", "path": str(box_glb)})
    assert "1 part" in out and "one part" in out


# ---------------- render ----------------
def test_render_contact_sheet(two_box_glb):
    import statistics

    from PIL import Image

    out = m3d.model_3d({"action": "render", "path": str(two_box_glb)})
    png = two_box_glb.parent / "render.png"
    assert png.exists() and str(png) in out
    assert "6 views" in out and "measure" in out       # nudges the full critique loop
    assert "Read" in out                               # LOOK step = the brain's Read tool
    im = Image.open(png)
    assert im.size[0] > im.size[1]                     # 3 x 2 grid + caption banner
    assert im.size[0] > 1500
    g = im.convert("L")
    assert statistics.pstdev(list(g.getdata())) > 5    # shaded boxes, not a blank sheet


def test_render_decimates_dense_mesh(tmp_path, monkeypatch):
    # over the face cap: render must decimate and say so, never refuse (a refused render
    # kills the LOOK step — the controller-stand live-test lesson)
    monkeypatch.setattr(m3d, "_MAX_RENDER_FACES", 2000)
    p = tmp_path / "dense.glb"
    trimesh.creation.icosphere(subdivisions=4).export(str(p))     # 5,120 faces
    out = m3d.model_3d({"action": "render", "path": str(p)})
    assert "Render failed" not in out
    assert "decimated" in out
    assert (tmp_path / "render.png").exists()


# ---------------- folder resolution ----------------
def test_resolve_prefers_glb_over_newer_stl(tmp_path):
    import os
    import time
    glb = tmp_path / "model.glb"
    stl = tmp_path / "model.stl"
    trimesh.creation.box(extents=(1, 2, 3)).export(str(glb))
    trimesh.creation.box().export(str(stl))
    now = time.time()
    os.utime(glb, (now - 60, now - 60))                           # STL is NEWER
    os.utime(stl, (now, now))
    out = m3d.model_3d({"action": "inspect", "path": str(tmp_path)})
    assert "model.glb" in out                                     # GLB still wins


# ---------------- show ----------------
def test_show_degrades_without_dashboard(box_glb, no_dashboard):
    # App down/unreachable: _post_show -> None -> text-only result.
    out = m3d.model_3d({"action": "show", "path": str(box_glb)})
    assert "ready at" in out and str(box_glb) in out


def test_show_converts_non_glb_into_sandbox(tmp_path, monkeypatch, no_dashboard):
    monkeypatch.setattr(m3d, "_WORK_ROOT", tmp_path / "work")
    src = tmp_path / "box.stl"
    trimesh.creation.box().export(str(src))
    out = m3d.model_3d({"action": "show", "path": str(src)})
    glb = tmp_path / "work" / "_viewer" / "box.glb"
    assert glb.exists() and str(glb) in out


def test_show_normalizes_stl_to_viewer_conventions(tmp_path, monkeypatch, no_dashboard):
    # STL is mm + Z-up; the shipped GLB must be meters + Y-up — a 100mm-wide, 25mm-tall
    # box becomes 0.1m wide with its old Z (0.025) on Y. (The 95-metre-sideways-model bug.)
    import numpy as np
    monkeypatch.setattr(m3d, "_WORK_ROOT", tmp_path / "work")
    src = tmp_path / "plate.stl"
    trimesh.creation.box(extents=(100.0, 50.0, 25.0)).export(str(src))
    out = m3d.model_3d({"action": "show", "path": str(src)})
    assert "normalized" in out
    shipped = trimesh.load(str(tmp_path / "work" / "_viewer" / "plate.glb"))
    assert np.allclose(sorted(shipped.extents), [0.025, 0.05, 0.1], atol=1e-6)
    assert abs(shipped.extents[1] - 0.025) < 1e-6                 # old Z is now on Y (up)


def test_show_posts_dashboard_card(box_glb, monkeypatch):
    posted = {}

    def fake_post(payload):
        posted.update(payload)
        return {"id": "abc123", "name": "box", "url": "/model/abc123", "size": 960}

    monkeypatch.setattr(m3d, "_post_show", fake_post)
    out = m3d.model_3d({"action": "show", "path": str(box_glb)})
    assert posted["path"] == str(box_glb)
    assert posted["verts"] == 8 and posted["faces"] == 12
    assert "dashboard" in out and "box" in out
