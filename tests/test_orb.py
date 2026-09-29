"""Unit tests for the layered orb engine's pure surfaces (helios/orb_layered.py).

Everything here runs headless — geometry, the pulse field, premultiplication, and a
full Renderer frame — no Win32 window is created (that path only runs under
__main__ / Orb()).
"""

import math
import random
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helios.orb_layered import (  # noqa: E402
    DISC_R, N, SIZE, PulseField, Renderer, premultiply_bgra, project,
    sphere_edges, sphere_nodes,
)


# ---------------------------------------------------------------- geometry
def test_sphere_nodes_on_unit_sphere():
    nodes = sphere_nodes(N)
    assert len(nodes) == N
    for p in nodes:
        assert math.isclose(p[0] ** 2 + p[1] ** 2 + p[2] ** 2, 1.0, abs_tol=1e-9)


def test_sphere_edges_respect_threshold():
    nodes = sphere_nodes(24)
    edges = sphere_edges(nodes, 0.5)
    assert edges, "expected some links at this density"
    for i, j in edges:
        d2 = sum((nodes[i][k] - nodes[j][k]) ** 2 for k in range(3))
        assert d2 < 0.5
        assert i < j                       # no duplicates / self-links


def test_sphere_edges_zero_threshold_is_empty():
    nodes = sphere_nodes(24)
    assert sphere_edges(nodes, 0.0) == []


def test_project_depth_bounds_and_center():
    nodes = sphere_nodes(48)
    for ang in (0.0, 1.3, 4.7):
        for p in nodes:
            x, y, d = project(p, ang, 0.42, 100.0, 100.0, 80.0)
            assert 0.0 <= d <= 1.0
    # the sphere center projects to the canvas center at any angle
    x, y, d = project((0.0, 0.0, 0.0), 2.2, 0.42, 100.0, 100.0, 80.0)
    assert math.isclose(x, 100.0, abs_tol=1e-9) and math.isclose(y, 100.0, abs_tol=1e-9)
    assert math.isclose(d, 0.5, abs_tol=1e-9)


# ---------------------------------------------------------- premultiply
def test_premultiply_bgra_known_values():
    rgba = np.zeros((1, 2, 4), dtype=np.uint8)
    rgba[0, 0] = (255, 128, 0, 255)        # opaque orange: unchanged, swapped to BGRA
    rgba[0, 1] = (200, 100, 50, 0)         # fully transparent: premultiplies to zero RGB
    out = np.frombuffer(premultiply_bgra(rgba), dtype=np.uint8).reshape(1, 2, 4)
    assert tuple(out[0, 0]) == (0, 128, 255, 255)   # B,G,R,A
    assert tuple(out[0, 1]) == (0, 0, 0, 0)


def test_premultiply_bgra_half_alpha_rounds():
    rgba = np.array([[[255, 255, 255, 128]]], dtype=np.uint8)
    out = np.frombuffer(premultiply_bgra(rgba), dtype=np.uint8).reshape(1, 1, 4)
    assert tuple(out[0, 0]) == (128, 128, 128, 128)


# ---------------------------------------------------------- pulse field
def test_pulse_field_deterministic_with_seed():
    a = PulseField(50, random.Random(7))
    b = PulseField(50, random.Random(7))
    for _ in range(60):
        a.step(1 / 30, 6.0)
        b.step(1 / 30, 6.0)
    assert a.active == b.active


def test_pulse_field_spawns_advances_and_expires():
    pf = PulseField(50, random.Random(1))
    for _ in range(30):
        pf.step(1 / 30, 12.0)
    assert pf.active, "high rate should have live pulses"
    assert all(0.0 <= t < 1.0 for _e, t, _v in pf.active)
    for _ in range(300):                   # long silence: everything expires
        pf.step(1 / 30, 0.0)
    assert pf.active == []


def test_pulse_field_capped():
    pf = PulseField(10, random.Random(2))
    for _ in range(600):
        pf.step(1 / 30, 500.0)
    assert len(pf.active) <= 48


def test_pulse_field_no_edges_never_spawns():
    pf = PulseField(0, random.Random(3))
    for _ in range(60):
        pf.step(1 / 30, 100.0)
    assert pf.active == []


# ------------------------------------------------------------- renderer
@pytest.fixture(scope="module")
def renderer():
    return Renderer()


def test_frame_shape_and_alpha(renderer):
    buf = renderer.frame((77, 208, 225), 0.8, 0.0, 0.5, 0.3, 6.0, 1 / 30)
    assert len(buf) == SIZE * SIZE * 4
    a = np.frombuffer(buf, dtype=np.uint8).reshape(SIZE, SIZE, 4)
    c = SIZE // 2
    assert a[c, c, 3] > 100, "disc center must be solidly clickable (alpha carries hits)"
    # corners fully transparent -> Windows passes clicks through (the whole point)
    for (yy, xx) in ((1, 1), (1, SIZE - 2), (SIZE - 2, 1), (SIZE - 2, SIZE - 2)):
        assert a[yy, xx, 3] == 0


def test_frame_alpha_inside_valid_range(renderer):
    buf = renderer.frame((224, 85, 107), 2.0, 0.5, 1.0, 1.0, 8.0, 1 / 30)
    a = np.frombuffer(buf, dtype=np.uint8).reshape(SIZE, SIZE, 4)
    assert a[..., 3].max() <= 255 and a[..., 3].min() == 0


def test_frame_reacts_to_state_colour(renderer):
    cold = renderer.frame((77, 208, 225), 1.0, 0.0, 0.5, 0.0, 0.4, 1 / 30)
    warm = renderer.frame((224, 85, 107), 1.0, 0.0, 0.5, 0.0, 0.4, 1 / 30)
    ca = np.frombuffer(cold, dtype=np.uint8).reshape(SIZE, SIZE, 4).astype(int)
    wa = np.frombuffer(warm, dtype=np.uint8).reshape(SIZE, SIZE, 4).astype(int)
    # BGRA: the red channel (index 2) dominates under the error colour, blue under cyan
    assert wa[..., 2].sum() > ca[..., 2].sum()
    assert ca[..., 0].sum() > wa[..., 0].sum()
