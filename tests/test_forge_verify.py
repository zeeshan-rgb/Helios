"""Forge verify-before-activate (Ada-SI-inspired): create_tool must load-test a candidate custom
tool in isolation and only save it if it imports clean and exposes a usable function. A syntax or
import error must be REJECTED with the real error — not silently saved then skipped at next startup
(the old write-and-hope behaviour). Targets mcp/helios_server.py's _verify_tool_file + create_tool.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent  # C:\Users\Tim\helios


def _load_server():
    """Import mcp/helios_server.py. The local mcp/ dir has no __init__.py, so we bind the INSTALLED
    mcp package with repo-root off sys.path first (else `from mcp.server.fastmcp import FastMCP`
    resolves to the local namespace dir and fails), then load the server module by file path."""
    rs = str(_ROOT)
    cached = sys.modules.get("mcp")
    if cached is not None and not hasattr(cached, "server"):
        for k in [k for k in list(sys.modules) if k == "mcp" or k.startswith("mcp.")]:
            del sys.modules[k]
    had = rs in sys.path
    if had:
        sys.path.remove(rs)
    try:
        import mcp.server.fastmcp  # noqa: F401  -> installed pkg, cached in sys.modules
    finally:
        if had:
            sys.path.insert(0, rs)
    spec = importlib.util.spec_from_file_location("helios_server_uut", _ROOT / "mcp" / "helios_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


js = _load_server()

GOOD = "def greet(who: str) -> str:\n    \"\"\"Say hi.\"\"\"\n    return 'hi ' + who\n"
SYNTAX = "def bad(:\n    return 1\n"
BAD_IMPORT = "import nonexistent_pkg_xyz\n\ndef t() -> int:\n    return 1\n"
NO_FUNC = "X = 3\n"


def _verify(tmp_path, code):
    f = tmp_path / "_cand.py"
    f.write_text(code, encoding="utf-8")
    return js._verify_tool_file(f, "custom_cand")


def test_verify_accepts_clean_function(tmp_path):
    ok, detail = _verify(tmp_path, GOOD)
    assert ok is True
    assert "greet" in detail            # reports the discovered function signature


def test_verify_rejects_syntax_error(tmp_path):
    ok, detail = _verify(tmp_path, SYNTAX)
    assert ok is False
    assert detail                       # carries the real error text back to the model


def test_verify_rejects_bad_import(tmp_path):
    ok, _ = _verify(tmp_path, BAD_IMPORT)
    assert ok is False


def test_verify_rejects_no_public_function(tmp_path):
    ok, detail = _verify(tmp_path, NO_FUNC)
    assert ok is False
    assert "no public function" in detail


def test_create_tool_saves_good_and_rejects_bad():
    """Full create_tool path: a good tool is promoted into custom_tools/ and reported verified; a
    broken one leaves NO file behind and returns an error. Uses a unique probe name and cleans up."""
    name = "pytest_forge_probe_zzz"
    good_target = js.CUSTOM_DIR / f"{name}.py"
    bad_target = js.CUSTOM_DIR / f"{name}bad.py"
    for t in (good_target, bad_target):
        if t.exists():
            t.unlink()
    try:
        msg_good = js.create_tool(name, GOOD)
        assert "verified" in msg_good.lower()
        assert good_target.exists()

        msg_bad = js.create_tool(name + "bad", SYNTAX)
        assert "did not verify" in msg_bad.lower()
        assert not bad_target.exists()
        assert not list(js.CUSTOM_DIR.glob("_verify_*"))   # no temp files leaked
    finally:
        for t in (good_target, bad_target):
            if t.exists():
                t.unlink()
