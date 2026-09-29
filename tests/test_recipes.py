"""Procedural memory: capturing the tool sequence of a PC-control task and recalling it by
keyword overlap. Isolated to a tmp DB so it's safe while Helios is live.
"""

from __future__ import annotations

import json

import pytest

from helios import db, memory


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init()
    return tmp_path


def test_short_tool():
    assert memory._short_tool("mcp__computer__click_element") == "click_element"
    assert memory._short_tool("mcp__helios__set_reminder") == "set_reminder"
    assert memory._short_tool("Bash") == "Bash"


def test_save_recipe_stores_sequence(tmpdb):
    memory.save_recipe("Open Calculator and compute 47x89", [
        "mcp__computer__launch_app", "mcp__computer__get_window_state",
        "mcp__computer__click_element", "mcp__computer__get_window_state"])
    rows = db.list_recipes()
    assert len(rows) == 1
    steps = json.loads(rows[0]["steps"])
    assert steps[0] == "launch_app" and "click_element" in steps


def test_save_recipe_dedupes_by_task(tmpdb):
    memory.save_recipe("open spotify and play music", ["mcp__computer__launch_app"])
    memory.save_recipe("Open Spotify and play music!", ["mcp__computer__launch_app", "mcp__computer__click_element"])
    rows = db.list_recipes()
    assert len(rows) == 1                                   # same normalized task -> upsert
    assert len(json.loads(rows[0]["steps"])) == 2          # latest steps win


def test_save_recipe_ignores_empty(tmpdb):
    memory.save_recipe("", ["mcp__computer__launch_app"])
    memory.save_recipe("has a task but no tools", [])
    assert db.list_recipes() == []


def test_recall_requires_two_keyword_overlap(tmpdb):
    memory.save_recipe("open the sound settings and switch output device",
                       ["mcp__computer__launch_app", "mcp__computer__click_element"])
    # >=2 shared keywords ("sound","settings"/"output") -> recalled
    hit = memory._recall_recipes("change my sound output settings")
    assert "launch_app" in hit and "→" in hit
    # only one weak shared word -> no recall
    assert memory._recall_recipes("what's the weather") == ""


def test_build_digest_includes_procedural_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t2.db")
    db.init()
    # empty vault so only the recipe section shows
    monkeypatch.setattr(memory, "ensure_vault", lambda: None)
    for attr in ("PEOPLE", "PROJECTS", "DAILY"):
        d = tmp_path / attr.lower(); d.mkdir(); monkeypatch.setattr(memory, attr, d)
    monkeypatch.setattr(memory, "_read", lambda p: "")
    memory.save_recipe("open notepad and type a note",
                       ["mcp__computer__launch_app", "mcp__computer__type_text"])
    out = memory.build_digest("open notepad please and type something")
    assert "Procedural memory" in out
    assert "launch_app" in out and "type_text" in out
    # fenced like the rest of the digest
    assert "<memory>" in out and out.rstrip().endswith("</memory>")
