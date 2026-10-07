"""Folder discovery: lists folders with recent work (read-only), rolls files up to their project
root, folds sub-folders into their parent, skips caches / old work / credential stores / Helios's
own data, and `helios projects pick` turns chosen numbers into manifests."""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

from helios import conf, projects

_ROOT = Path(__file__).resolve().parent.parent


def touch(p: Path, age_days: float = 1):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))


@pytest.fixture
def tree(tmp_path, monkeypatch):
    pdir = tmp_path / "manifests"
    pdir.mkdir()
    monkeypatch.setattr(conf, "projects_dir", lambda: pdir)
    monkeypatch.setattr(conf, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    home = tmp_path / "Documents"
    touch(home / "Chronos" / "pyproject.toml")
    touch(home / "Chronos" / "src" / "deep" / "main.py")
    touch(home / "PlmDocs" / "modules" / "a" / "doc.xml")
    touch(home / "PlmDocs" / "trunk" / ".project")
    touch(home / "PlmDocs" / "trunk" / "x.txt")
    touch(home / "Old" / "notes.txt", age_days=200)                  # not touched lately
    touch(home / "npm-cache" / "pkg" / "index.js")                   # cache: skipped
    touch(home / "Keys" / ".ssh" / "id_rsa")                          # protected: never walked
    touch(home / "Repo" / ".git" / "HEAD")
    touch(home / "Repo" / "app.py")
    return type("T", (), {"home": home, "pdir": pdir, "tmp": tmp_path})


def test_discovers_recent_project_folders(tree):
    items = projects.discover_recent(60, [tree.home])
    paths = {Path(e["path"]).name: e for e in items}
    assert set(paths) == {"Chronos", "PlmDocs", "Repo"}
    assert paths["Chronos"]["recent_files"] == 2 and "python" in paths["Chronos"]["technology"]
    assert paths["PlmDocs"]["recent_files"] == 3                      # trunk + modules folded in
    assert paths["Repo"]["git"] and not paths["Repo"]["known"]
    assert items[0]["latest_iso"]
    text = projects.format_discovered(items)
    assert "Chronos [python" in text and "(git)" in text


def test_discovery_is_read_only_and_marks_known(tree):
    projects.add(str(tree.home / "Chronos"))
    before = sorted(p.name for p in tree.pdir.iterdir())
    items = projects.discover_recent(60, [tree.home])
    assert sorted(p.name for p in tree.pdir.iterdir()) == before       # nothing added by listing
    assert next(e for e in items if e["path"].endswith("Chronos"))["known"]


def test_discovery_never_lists_helios_data(tree, monkeypatch):
    monkeypatch.setattr(conf, "DATA_DIR", tree.home / "Repo")
    names = {Path(e["path"]).name for e in projects.discover_recent(60, [tree.home])}
    assert "Repo" not in names


def test_pick_adds_chosen_folders(tree, capsys):
    spec = importlib.util.spec_from_file_location("helios_cli_disc", _ROOT / "helios_cli.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.cmd_projects(["pick", "1"]) == 1                       # no list yet
    assert "discover" in capsys.readouterr().out
    items = projects.discover_recent(60, [tree.home])
    (conf.DATA_DIR / "projects_discovered.json").write_text(json.dumps(items), encoding="utf-8")
    cli.cmd_projects(["pick", "1", "2", "99", "x"])
    out = capsys.readouterr().out
    assert out.count("added") == 2 and "99: not in the list" in out and "x: not in the list" in out
    assert len(list(tree.pdir.glob("*.yaml"))) == 2
    cli.cmd_projects(["pick", "1"])
    assert "skipped" in capsys.readouterr().out                        # already a project
