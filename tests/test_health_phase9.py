"""Phase 9 project health: check kinds -> report rows, git detail (unpushed, stale uncommitted work),
dependency status (npm / pip, fixed read-only commands only), Helios's built-in probes, potential
issues and the verdict, and the PROJECT HEALTH section in the night report."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from helios import conf, health, memory_store, projects

PY = Path(sys.executable).as_posix()


@pytest.fixture
def pdir(tmp_path, monkeypatch):
    d = tmp_path / "manifests"
    d.mkdir()
    monkeypatch.setattr(conf, "projects_dir", lambda: d)
    return d


def _proj(tmp_path, pdir, body: str, name="app", files=()):
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    for f in files:
        (folder / f).write_text("{}" if f.endswith(".json") else "", encoding="utf-8")
    (pdir / f"{name}.yaml").write_text(f"name: {name.title()}\npath: {folder.as_posix()}\n{body}",
                                       encoding="utf-8")
    return folder


OK = f"'{PY} -c pass'"
BAD = f"'{PY} -c \"raise SystemExit(4)\"'"


# ------------------------------------------------------------------ kinds + manifest fields

def test_check_kinds_are_inferred(pdir, tmp_path):
    _proj(tmp_path, pdir, f"commands: {{test: {OK}, build: {OK}}}\nhealth_checks:\n"
                          f"  - {{run: test}}\n  - {{run: build}}\n  - {{name: eslint, run: {OK}}}\n"
                          f"  - {{name: tsc, run: {OK}}}\n  - {{name: smoke, run: {OK}, kind: test}}\n"
                          f"  - {{name: misc, run: {OK}}}\n")
    kinds = {c["name"]: c["kind"] for c in projects.get("app")["health_checks"]}
    assert kinds == {"test": "test", "build": "build", "eslint": "lint", "tsc": "typecheck",
                     "smoke": "test", "misc": "other"}


def test_manifest_health_fields_are_validated(pdir, tmp_path):
    _proj(tmp_path, pdir, "dependencies: yarnish\nbuiltin_checks: [helios_memory, warp_drive]\n")
    p = projects.get("app")
    assert p["dependencies"] == "off" and p["builtin_checks"] == ["helios_memory"]
    assert any("dependencies" in x for x in p["problems"]) and any("warp_drive" in x for x in p["problems"])


# ------------------------------------------------------------------ report rows + verdict

def test_rows_and_failing_verdict(pdir, tmp_path, monkeypatch):
    monkeypatch.setattr(health, "dependency_status", lambda p: {"status": "ok", "summary": "npm: 0 outdated",
                                                                 "at": datetime.now().isoformat()})
    _proj(tmp_path, pdir, f"health_checks:\n  - {{name: test, run: {OK}}}\n  - {{name: lint, run: {BAD}}}\n")
    rep = health.run_all("app")[0]
    rows = {r["row"]: r for r in rep["rows"]}
    assert rows["Build"]["text"] == "not configured"                   # never implied to pass
    assert rows["Tests"]["text"].startswith("PASS (")
    assert rows["Lint"]["status"] == "fail" and "FAIL (exit 4" in rows["Lint"]["text"]
    assert rows["Git status"]["text"] == "not a git repository"
    assert rep["verdict"] == "FAILING" and "Lint failing" in rep["issues"]


def test_ok_verdict_needs_real_passes(pdir, tmp_path, monkeypatch):
    monkeypatch.setattr(health, "dependency_status", lambda p: {"status": "ok", "summary": "fine",
                                                                 "at": datetime.now().isoformat()})
    _proj(tmp_path, pdir, f"health_checks:\n  - {{name: test, run: {OK}}}\n")
    assert health.reports("app")[0]["verdict"] == "UNKNOWN"             # nothing checked yet
    assert health.run_all("app")[0]["verdict"] == "OK"


def test_no_tests_and_never_run_are_issues(pdir, tmp_path):
    _proj(tmp_path, pdir, f"dependencies: off\nhealth_checks:\n  - {{name: lint, run: {OK}}}\n")
    rep = health.reports("app")[0]
    assert "no tests configured" in rep["issues"] and "health checks never run" in rep["issues"]
    assert {r["row"]: r["text"] for r in rep["rows"]}["Dependencies"] == "turned off"


# ------------------------------------------------------------------ git detail

def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd,
                   check=True, capture_output=True)


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_git_detail_unpushed_and_stale_work(pdir, tmp_path):
    remote = tmp_path / "remote.git"
    _git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    work = _proj(tmp_path, pdir, "dependencies: off\n", name="work")
    _git("init", "-q", cwd=work)
    (work / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git("add", ".", cwd=work)
    _git("commit", "-qm", "one", cwd=work)
    _git("remote", "add", "origin", str(remote), cwd=work)
    _git("push", "-q", "-u", "origin", "HEAD", cwd=work)
    (work / "b.py").write_text("y = 2\n", encoding="utf-8")
    _git("add", "b.py", cwd=work)
    _git("commit", "-qm", "two (not pushed)", cwd=work)
    (work / "a.py").write_text("x = 1\nz = 3\n", encoding="utf-8")
    old = time.time() - 5 * 86400
    os.utime(work / "a.py", (old, old))
    g = projects.git_state(projects.get("work"), detail=True)
    assert g["ahead"] == 1 and g["behind"] == 0 and g["uncommitted"] == 1
    assert g["oldest_change_days"] >= 4.9 and "1 file changed" in g["diffstat"]
    assert g["last_commit_subject"] == "two (not pushed)"
    rep = health.reports("work")[0]
    git_row = {r["row"]: r["text"] for r in rep["rows"]}["Git status"]
    assert "1 not pushed" in git_row and "1 uncommitted (oldest 5 d)" in git_row
    assert "1 commit(s) not pushed" in rep["issues"]
    assert any("uncommitted work waiting 5 days" in i for i in rep["issues"])


# ------------------------------------------------------------------ dependencies

def test_npm_dependency_status(pdir, tmp_path, monkeypatch):
    folder = _proj(tmp_path, pdir, "", files=("package.json",))
    calls = []

    def fake_run(argv, cwd, timeout=180):
        calls.append(argv[1:])
        if argv[1] == "outdated":
            return 1, json.dumps({"next": {"current": "15.1.0", "wanted": "15.2.0", "latest": "16.0.1"},
                                  "three": {"current": "0.160.0", "latest": "0.161.0"}})
        return 1, json.dumps({"metadata": {"vulnerabilities": {"high": 1, "moderate": 2, "low": 0}},
                              "vulnerabilities": {"postcss": {"severity": "moderate"},
                                                  "next": {"severity": "high"}}})
    monkeypatch.setattr(health, "_run", fake_run)
    monkeypatch.setattr(health.shutil, "which", lambda n: "npm.cmd" if n == "npm" else None)
    d = health.dependency_status(projects.get("app"))
    assert d["manager"] == "npm" and len(d["outdated"]) == 2 and d["major"] == 1
    assert d["vulnerabilities"] == {"high": 1, "moderate": 2}
    assert "dependencies aren't installed (no node_modules)" in d["problems"]
    assert d["status"] == "fail" and "1 high" in d["summary"]           # fail = not installed
    assert d["vulnerable"][0] == {"name": "next", "severity": "high"}   # worst first
    assert calls == [["outdated", "--json"], ["audit", "--json"]]      # read-only commands only
    (folder / "node_modules").mkdir()
    d = health.dependency_status(projects.get("app"))
    assert d["status"] == "warn"                  # installed: advisories = attention, not failing
    st = projects.state(projects.get("app"))
    st["deps"] = d
    projects._save_state(projects.get("app"), st)
    rep = health.reports("app")[0]
    assert any("security advisories: 1 high, 2 moderate — next (high), postcss (moderate)" in i
               for i in rep["issues"])
    monkeypatch.setattr(health, "_run", lambda argv, cwd, timeout=180:
                        (0, "{}") if argv[1] == "outdated" else (0, json.dumps({"metadata": {"vulnerabilities": {}}})))
    d = health.dependency_status(projects.get("app"))
    assert d["status"] == "ok" and "no known vulnerabilities" in d["summary"]


def test_pip_dependency_status(pdir, tmp_path, monkeypatch):
    folder = _proj(tmp_path, pdir, "", files=("requirements.txt",))
    assert health.dependency_status(projects.get("app"))["status"] == "skip"    # no venv: never system pip
    (folder / ".venv" / "Scripts").mkdir(parents=True)
    (folder / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    calls = []

    def fake_run(argv, cwd, timeout=180):
        calls.append(argv[1:])
        if "check" in argv:
            return 1, "requests 2.0 has requirement idna<3, but you have idna 3.4.\n"
        return 0, json.dumps([{"name": "numpy", "version": "1.26.4", "latest_version": "2.1.0"}])
    monkeypatch.setattr(health, "_run", fake_run)
    d = health.dependency_status(projects.get("app"))
    # pip check complaints warn (often harmless, e.g. the old `typing` backport), never fail
    assert d["manager"] == "pip" and d["major"] == 1 and d["status"] == "warn"
    assert "idna" in d["warnings"][0] and not d["problems"]
    assert calls == [["-m", "pip", "check"], ["-m", "pip", "list", "--outdated", "--format=json"]]


# ------------------------------------------------------------------ probes

def test_memory_probe(pdir, tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(memory_store, "vault", lambda: vault)
    memory_store.remember("Prefers concise reports", "preferences")
    memory_store.remember("Always reply in English", "rules", status="pending")
    pr = health.probe_helios_memory({})
    assert pr["status"] == "warn" and any("1 preferences" in i for i in pr["items"])
    assert any("waiting for your approval" in i for i in pr["issues"])
    leak = vault / "User" / "leak.md"
    leak.parent.mkdir(exist_ok=True)
    leak.write_text("---\nid: user-x\ncategory: user\n---\n\nmy password is hunter22\n", encoding="utf-8")
    assert health.probe_helios_memory({})["status"] == "fail"


def test_runtime_probe(monkeypatch):
    monkeypatch.setattr(health.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(conf, "brain_engine", lambda: "antigravity")
    monkeypatch.setattr(conf, "proactive_cfg", lambda: {"disk_gb": 0})
    from helios import agy_cli
    monkeypatch.setattr(agy_cli, "command", lambda: [])
    pr = health.probe_helios_runtime({})
    assert pr["status"] == "fail" and "app: not running" in pr["items"]
    assert any("agy" in i for i in pr["issues"])


def test_voice_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(conf, "voice_cfg", lambda: {"enabled": True, "tts_engine": "cloud"})
    monkeypatch.setattr(health, "_voice_daemon_running", lambda: False)
    monkeypatch.setattr(conf, "LOGS_DIR", tmp_path)
    (tmp_path / "voice.log").write_text(
        f"{datetime.now():%Y-%m-%d %H:%M:%S}  stt failed: model missing\n", encoding="utf-8")
    pr = health.probe_helios_voice({})
    assert pr["status"] == "warn"
    assert any("listener isn't running" in i for i in pr["issues"])
    assert any("stt failed" in i for i in pr["issues"])


def test_probe_rows_and_crash_isolation(pdir, tmp_path, monkeypatch):
    monkeypatch.setitem(health.PROBES, "helios_voice", ("Voice", lambda p: 1 / 0))
    monkeypatch.setitem(health.PROBES, "helios_memory", ("Memory", lambda p: health._probe("ok", ["fine"], [])))
    _proj(tmp_path, pdir, "dependencies: off\nbuiltin_checks: [helios_memory, helios_voice]\n")
    rep = health.run_all("app")[0]
    rows = {r["row"]: r for r in rep["rows"]}
    assert rows["Memory"]["status"] == "ok" and rows["Voice"]["status"] == "fail"
    assert rep["verdict"] == "FAILING" and any("Voice probe crashed" in i for i in rep["issues"])


# ------------------------------------------------------------------ night report

def test_night_report_has_project_health_section(pdir, tmp_path, monkeypatch):
    from helios.night_mode import scheduler as sched
    monkeypatch.setattr(conf, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(memory_store, "vault", lambda: tmp_path / "vault")
    monkeypatch.setattr(sched, "cfg", lambda: {"enabled": True, "schedule": {"run_checks": "01:00",
                                                                           "project_summaries": "01:05"}})
    monkeypatch.setattr(sched, "online", lambda: False)
    _proj(tmp_path, pdir, f"dependencies: off\nhealth_checks:\n  - {{name: test, run: {BAD}}}\n")
    rec = sched.run_night()
    report = Path(rec["report"]).read_text(encoding="utf-8")
    assert "## Project health" in report and "### App — FAILING" in report
    assert "- Tests: FAIL (exit 4" in report and "- Build: not configured" in report
    assert "App: FAILING" in report                       # project summary line
    assert rec["tasks"]["run_checks"]["data"]["health"][0]["verdict"] == "FAILING"
