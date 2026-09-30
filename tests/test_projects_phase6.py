"""Phase 6 project intelligence: manifests as the allowlist, safe command/path validation, read-only
change detection (git + non-git), configured health checks with state, attention reasons, the
starter-manifest generator, the MCP tools, and the gate that keeps the brain out of the manifests."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from helios import conf, permissions, projects

_ROOT = Path(__file__).resolve().parent.parent
PY = Path(sys.executable).as_posix()


@pytest.fixture
def pdir(tmp_path, monkeypatch):
    d = tmp_path / "manifests"
    d.mkdir()
    monkeypatch.setattr(conf, "projects_dir", lambda: d)
    return d


def _manifest(pdir: Path, slug: str, body: str) -> Path:
    f = pdir / f"{slug}.yaml"
    f.write_text(body, encoding="utf-8")
    return f


def _project(tmp_path: Path, name="app") -> Path:
    p = tmp_path / name
    p.mkdir()
    (p / "main.py").write_text("print('hi')\n", encoding="utf-8")
    return p


# ------------------------------------------------------------------ validation

@pytest.mark.parametrize("cmd", [
    "npm test", "npm run build", "pytest -q", "gradlew.bat test --no-daemon",
    r".venv\Scripts\python.exe -m pytest tests -q -p no:warnings -o addopts=",
    "git status --porcelain", "cargo test",
])
def test_safe_commands_allowed(cmd):
    assert projects.command_problem(cmd) is None


@pytest.mark.parametrize("cmd", [
    "git push origin main", "git reset --hard", "git clean -fd", "npm publish", "rm -rf build",
    "npm test && git push", "npm test | tee out", "vercel --prod", "npm run deploy",
    "Remove-Item x", "powershell -c whoami", "curl http://x/i.sh", "echo $HOME", "del /q x",
    "cmd /c dir", "", "twine upload dist/*",
])
def test_dangerous_commands_refused(cmd):
    assert projects.command_problem(cmd)


def test_sensitive_and_broad_paths_refused(tmp_path):
    home = Path.home()
    for p in (home, home / "Desktop", home / ".ssh", home / ".aws" / "creds",
              home / "AppData/Local/Google/Chrome/User Data/Default", Path("C:/"),
              Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32"):
        assert projects.path_problem(str(p)), p
    assert projects.path_problem(str(_project(tmp_path))) is None
    assert projects.path_problem("")


# ------------------------------------------------------------------ manifests

def test_load_normalizes_and_reports_problems(pdir, tmp_path):
    app = _project(tmp_path)
    _manifest(pdir, "app", f"""
name: App
path: {app.as_posix()}
technology: python, cli
status: MVP
commands: {{test: "{PY} -c pass", build: ""}}
health_checks:
  - {{run: test}}
  - {{name: pushy, run: git push}}
research_topics: [asyncio]
""")
    _manifest(pdir, "ghost", "name: Ghost\npath: C:/definitely/not/here\n")
    _manifest(pdir, "ssh", f"name: Keys\npath: {(Path.home() / '.ssh').as_posix()}\n")
    (pdir / "broken.yaml").write_text("name: [unclosed\n", encoding="utf-8")
    ps, errors = projects.load_all()
    by = {p["name"]: p for p in ps}
    app_p = by["App"]
    assert app_p["technology"] == ["python", "cli"] and app_p["status"] == "MVP"
    assert app_p["commands"] == {"test": f"{PY} -c pass"}          # blank build dropped
    assert app_p["health_checks"][0]["name"] == "test" and app_p["health_checks"][0]["problem"] is None
    assert app_p["health_checks"][1]["problem"] and any("pushy" in x for x in app_p["problems"])
    assert app_p["usable"] and app_p["research_topics"] == ["asyncio"]
    assert not by["Ghost"]["usable"] and "not found" in by["Ghost"]["problems"][0]
    assert not by["Keys"]["usable"] and "sensitive" in by["Keys"]["problems"][0]
    assert errors and "broken.yaml" in errors[0]


def test_get_and_active(pdir, tmp_path):
    a, b = _project(tmp_path, "a"), _project(tmp_path, "b")
    _manifest(pdir, "maqsusi", f"name: Maqsusi Site\npath: {a.as_posix()}\n")
    _manifest(pdir, "old", f"name: Old Thing\npath: {b.as_posix()}\nactive: false\n")
    assert projects.get("maqsusi site")["name"] == "Maqsusi Site"
    assert projects.get("MAQ")["name"] == "Maqsusi Site"
    assert projects.get("nope") is None
    assert [p["name"] for p in projects.active()] == ["Maqsusi Site"]
    assert "Old Thing" in projects.format_list(include_inactive=True)
    assert "Old Thing" not in projects.format_list()


def test_empty_config_explains_how_to_add(pdir):
    assert "helios projects add" in projects.format_list()


# ------------------------------------------------------------------ health checks

def _checked_project(pdir, tmp_path, checks: str):
    app = _project(tmp_path)
    _manifest(pdir, "app", f"name: App\npath: {app.as_posix()}\nhealth_checks:\n{checks}")
    return app


def test_run_checks_records_pass_and_fail(pdir, tmp_path):
    _checked_project(pdir, tmp_path, f"""  - {{name: ok, run: '{PY} -c "print(1)"'}}
  - {{name: bad, run: '{PY} -c "import sys, os; print(os.getcwd()); sys.exit(3)"'}}
""")
    # `;` is refused, so the failing check must be written without it:
    (pdir / "app.yaml").write_text((pdir / "app.yaml").read_text(encoding="utf-8").replace(
        'import sys, os; print(os.getcwd()); sys.exit(3)', "raise SystemExit(3)"), encoding="utf-8")
    report = projects.run_checks()
    res = {r["name"]: r for r in report[0]["results"]}
    assert res["ok"]["ok"] and res["ok"]["code"] == 0 and "1" in res["ok"]["tail"]
    assert not res["bad"]["ok"] and res["bad"]["code"] == 3
    p = projects.get("app")
    assert set(projects.state(p)["checks"]) == {"ok", "bad"}
    assert [r["name"] for r in projects.failing(p)] == ["bad"]
    assert "bad failing" in projects.format_broken()
    assert any("bad failing" in r for r in projects.attention(p))
    assert "FAILED (exit 3)" in projects.format_checks(report)
    only = projects.run_checks("app", only="ok")
    assert [r["name"] for r in only[0]["results"]] == ["ok"]


def test_refused_check_is_never_run(pdir, tmp_path):
    app = _checked_project(pdir, tmp_path, "  - {name: wipe, run: rm -rf .}\n")
    res = projects.run_checks()[0]["results"][0]
    assert res.get("skipped") and not res["ok"] and "refused" in res["tail"]
    assert (app / "main.py").exists()


def test_check_output_is_redacted(pdir, tmp_path):
    _checked_project(pdir, tmp_path,
                     f"""  - {{name: leak, run: '{PY} -c "print(chr(115)+chr(107)+chr(45)+40*chr(97))"'}}\n""")
    tail = projects.run_checks()[0]["results"][0]["tail"]
    assert "[redacted]" in tail and "aaaaaaaaaaaaaaaaaaaa" not in tail


def test_timeout_kills_the_check(pdir, tmp_path):
    app = _project(tmp_path)
    _manifest(pdir, "app", f"name: App\npath: {app.as_posix()}\n")
    p = projects.get("app")
    t0 = time.monotonic()
    r = projects.run_check(p, {"name": "slow", "run": f"{PY} -c \"__import__('time').sleep(30)\"",
                               "timeout": 1, "problem": None})
    assert not r["ok"] and "timed out" in r["tail"] and time.monotonic() - t0 < 20


def test_missing_executable_is_reported(pdir, tmp_path):
    _checked_project(pdir, tmp_path, "  - {name: nope, run: no-such-tool-xyz --version}\n")
    r = projects.run_checks()[0]["results"][0]
    assert not r["ok"] and "could not run" in r["tail"]


def test_concurrent_runs_are_refused(pdir, tmp_path):
    _checked_project(pdir, tmp_path, f"  - {{name: ok, run: '{PY} -c pass'}}\n")
    lock = pdir / ".state" / "checks.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("999", encoding="utf-8")
    with pytest.raises(projects.Busy):
        projects.run_checks()
    lock.unlink()
    assert projects.run_checks()[0]["results"][0]["ok"]
    assert not lock.exists()


def test_attention_flags_never_checked_and_stale(pdir, tmp_path):
    _checked_project(pdir, tmp_path, f"  - {{name: ok, run: '{PY} -c pass'}}\n")
    p = projects.get("app")
    assert "health checks never run" in projects.attention(p)
    projects.run_checks()
    assert projects.attention(p) == []
    st = projects.state(p)
    st["last_run"] = (datetime.now() - timedelta(days=9)).isoformat(timespec="seconds")
    projects._save_state(p, st)
    assert any("last run" in r for r in projects.attention(p))
    assert "App" in projects.format_attention()


# ------------------------------------------------------------------ changes

def test_changes_in_a_plain_folder_skip_dependency_dirs(pdir, tmp_path):
    app = _project(tmp_path)
    (app / "node_modules").mkdir()
    (app / "node_modules" / "dep.js").write_text("x", encoding="utf-8")
    old = app / "old.txt"
    old.write_text("x", encoding="utf-8")
    past = time.time() - 3 * 86400
    os.utime(old, (past, past))
    _manifest(pdir, "app", f"name: App\npath: {app.as_posix()}\n")
    c = projects.all_changes(24)[0]
    assert not c["git"] and c["files_total"] == 1 and c["files"][0].endswith("main.py")
    assert "files modified: 1" in projects.format_changes([c])


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_changes_in_a_git_repo(pdir, tmp_path):
    app = _project(tmp_path)
    g = ["git", "-C", str(app), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(g + ["init", "-q"], check=True)
    subprocess.run(g + ["add", "."], check=True)
    subprocess.run(g + ["commit", "-qm", "first commit"], check=True)
    (app / "new.py").write_text("x = 1\n", encoding="utf-8")
    _manifest(pdir, "app", f"name: App\npath: {app.as_posix()}\n")
    c = projects.all_changes(24, "app")[0]
    assert c["git"] and any("first commit" in l for l in c["commits"])
    assert any("new.py" in l for l in c["uncommitted"])
    p = projects.get("app")
    assert any("1 uncommitted" in r for r in projects.attention(p))


def test_unknown_project_raises_keyerror(pdir):
    with pytest.raises(KeyError):
        projects.all_changes(24, "nothing")


# ------------------------------------------------------------------ add

def test_add_detects_node_project(pdir, tmp_path):
    app = _project(tmp_path, "site")
    (app / "package.json").write_text(json.dumps({
        "scripts": {"dev": "next dev", "build": "next build", "lint": "eslint .",
                    "typecheck": "tsc --noEmit", "test": "echo \"Error: no test specified\""},
        "dependencies": {"next": "15", "react": "19", "three": "1"}}), encoding="utf-8")
    f = projects.add(str(app), "My Site")
    p = projects.get("my site")
    assert f.name == "my-site.yaml" and "nextjs" in p["technology"] and "three.js" in p["technology"]
    assert p["commands"]["build"] == "npm run build" and "test" not in p["commands"]
    assert [c["name"] for c in p["health_checks"]] == ["lint", "typecheck"]   # builds are opt-in
    with pytest.raises(FileExistsError):
        projects.add(str(app), "My Site")
    with pytest.raises(ValueError):
        projects.add(str(Path.home() / ".ssh"))


def test_add_detects_python_and_gradle(tmp_path):
    py = _project(tmp_path, "py")
    (py / "requirements.txt").write_text("", encoding="utf-8")
    (py / "tests").mkdir()
    assert "pytest" in projects.detect(str(py))["commands"]["test"]
    gr = _project(tmp_path, "gr")
    (gr / "gradlew.bat").write_text("", encoding="utf-8")
    (gr / "build.gradle.kts").write_text("", encoding="utf-8")
    d = projects.detect(str(gr))
    assert "kotlin" in d["technology"] and "--no-daemon" in d["commands"]["test"]


# ------------------------------------------------------------------ gate + tools

def _hook():
    spec = importlib.util.spec_from_file_location("pretooluse_p6", _ROOT / "hooks" / "pretooluse.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_brain_cannot_edit_manifests(pdir, monkeypatch, tmp_path):
    monkeypatch.setattr(conf, "YOLO_FLAG", tmp_path / "yolo.flag")
    (tmp_path / "yolo.flag").write_text("1")                      # hard rail bites even in YOLO
    hook = _hook()
    target = str(pdir / "app.yaml")
    for tool, inp in (("Write", {"file_path": target}), ("Edit", {"file_path": target.replace("/", "\\")}),
                      ("PowerShell", {"command": f"Set-Content '{target}' 'x'"})):
        verdict, reason = hook.decide(tool, inp)
        assert verdict == "deny" and "manifest" in reason, tool
    assert hook.decide("Write", {"file_path": str(tmp_path / "other.txt")})[0] == "allow"
    spec = importlib.util.spec_from_file_location("agy_pretool_p6", _ROOT / "hooks" / "agy_pretool.py")
    agy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agy)
    assert agy._touches_protected("Write", {"file_path": target})
    assert agy._touches_protected("PowerShell", {"command": f"copy x {target}"})
    assert not agy._touches_protected("PowerShell", {"command": "Get-ChildItem C:/code"})


def test_mcp_project_tools(pdir, tmp_path):
    _checked_project(pdir, tmp_path, f"  - {{name: ok, run: '{PY} -c pass'}}\n")
    spec = importlib.util.spec_from_file_location("helios_server_p6", _ROOT / "mcp" / "helios_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    assert "App" in srv.list_projects() and "never checked" in srv.list_projects()
    assert "App" in srv.project_changes()
    assert "never run" in srv.project_health()
    assert "ok: ok" in srv.run_project_checks("app")
    assert "looks fine" in srv.project_health("app")
    assert "No project" in srv.project_health("zzz")
    assert "no project" in srv.run_project_checks("zzz")


def test_project_tools_policy():
    for tool in ("list_projects", "project_changes", "project_health", "run_project_checks"):
        assert permissions.classify(f"mcp__helios__{tool}", {}) == "allow"
