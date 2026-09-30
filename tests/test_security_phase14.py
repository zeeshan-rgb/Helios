"""Phase 14 security: credential stores and secrets are hard-denied to every caller (even YOLO and
background agents) unless explicitly allowed in [security] allow_protected; legitimate work isn't
caught; secrets are redacted from logs; and the protections the blueprint says to keep still hold."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from helios import conf, projects, protected

_ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home().as_posix()


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gate(monkeypatch):
    mod = _load("pretooluse_p14", "hooks/pretooluse.py")
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("app not running")))
    return mod


@pytest.fixture
def allow(monkeypatch):
    sec = {"allow_protected": []}
    real = conf._section
    monkeypatch.setattr(conf, "_section", lambda name: sec if name == "security" else real(name))
    return sec


# ------------------------------------------------------------------ categories

@pytest.mark.parametrize("path,cat", [
    (f"{HOME}/.ssh/id_rsa", "SSH keys"),
    (f"{HOME}/.ssh", "SSH keys"),
    ("D:/backup/id_ed25519.pub", "SSH keys"),
    (f"{HOME}/Documents/Passwords.kdbx", "password manager data"),
    (f"{HOME}/AppData/Roaming/Bitwarden/data.json", "password manager data"),
    (f"{HOME}/AppData/Local/Google/Chrome/User Data/Default/Login Data", "browser credential store"),
    (f"{HOME}/AppData/Local/Microsoft/Edge/User Data/Local State", "browser credential store"),
    (f"{HOME}/AppData/Roaming/Mozilla/Firefox/Profiles/x.default/logins.json", "browser credential store"),
    (f"{HOME}/AppData/Roaming/Microsoft/Protect/S-1-5", "Windows credential store"),
    ("C:/code/app/.env", "API credential file"),
    ("C:/code/app/.env.local", "API credential file"),
    (f"{HOME}/.npmrc", "API credential file"),
    ("C:/keys/server.pem", "API credential file"),
    ("C:/dl/client_secret_123.apps.json", "API credential file"),
    (f"{HOME}/.aws/credentials", "cloud credentials"),
    (f"{HOME}/.kube/config", "cloud credentials"),
    (f"{HOME}/.docker/config.json", "cloud credentials"),
    (f"{HOME}/AppData/Roaming/gnupg/private-keys-v1.d", "GPG keys"),
    ("C:/Users/x/Desktop/Claude/Helios/helios/config/secrets.toml", "Helios/assistant secrets"),
    (f"{HOME}/.claude/.credentials.json", "Helios/assistant secrets"),
])
def test_protected_categories(path, cat):
    assert protected.check(path) == cat


@pytest.mark.parametrize("path", [
    "C:/Users/x/Desktop/Claude/Helios/helios/helios/app.py",
    "C:/code/app/.venv/Scripts/python.exe",
    "C:/code/app/environment.yml",
    f"{HOME}/Documents/Keynote notes.txt",
    f"{HOME}/Documents/report.pdf",
    "D:/Helios/data/memory/Daily/2026-09-30.md",
    "C:/code/app/src/password_reset.py",       # code ABOUT passwords is not a credential store
])
def test_normal_files_are_not_protected(path):
    assert protected.check(path) is None


def test_tool_inputs():
    assert protected.tool_check("Read", {"file_path": f"{HOME}/.ssh/id_rsa"}) == "SSH keys"
    assert protected.tool_check("LS", {"path": f"{HOME}/.aws"}) == "cloud credentials"
    assert protected.tool_check("Glob", {"pattern": f"{HOME}/.ssh/*"}) == "SSH keys"
    assert protected.tool_check("Write", {"file_path": "C:/app/.env"}) == "API credential file"
    assert protected.tool_check("Grep", {"pattern": "password", "path": "C:/code/app"}) is None
    assert protected.tool_check("PowerShell", {"command": "type $env:USERPROFILE\\.ssh\\id_rsa"}) == "SSH keys"
    assert protected.tool_check("PowerShell", {"command": "cmdkey /list"}) == "Windows credential store"
    assert protected.tool_check("PowerShell", {"command": "python -m venv .venv"}) is None
    assert protected.tool_check("PowerShell", {"command": "git status"}) is None
    assert protected.tool_check("WebSearch", {"query": ".ssh keys"}) is None      # not a file tool


# ------------------------------------------------------------------ the gate rail

def test_gate_hard_denies_even_in_yolo_and_for_background_agents(gate, monkeypatch, tmp_path):
    yolo = tmp_path / "yolo.flag"
    yolo.write_text("on")
    monkeypatch.setattr(conf, "YOLO_FLAG", yolo)
    verdict, reason = gate.decide("Read", {"file_path": f"{HOME}/.ssh/id_rsa"})
    assert verdict == "deny" and "SSH keys" in reason and "allow_protected" in reason
    monkeypatch.setenv("HELIOS_AGENT_ROLE", "side")
    assert gate.decide("Glob", {"pattern": f"{HOME}/.aws/**"})[0] == "deny"
    log = (conf.LOGS_DIR / "security.log").read_text(encoding="utf-8")
    assert "DENY Read: protected (SSH keys)" in log and "[background agent]" in log


def test_explicit_allowlist_falls_back_to_the_normal_asking_policy(gate, allow):
    allow["allow_protected"] = [f"{HOME}/.ssh/config"]
    verdict, reason = gate.decide("Read", {"file_path": f"{HOME}/.ssh/config"})
    assert "blocked: that's" not in reason                     # not the protected rail...
    assert verdict == "deny" and "unreachable" in reason       # ...the normal ask (no app here)
    assert gate.decide("Read", {"file_path": f"{HOME}/.ssh/id_rsa"})[1].startswith("blocked")


def test_antigravity_tool_calls_hit_the_same_rail():
    agy = _load("agy_pretool_p14", "hooks/agy_pretool.py")
    assert agy.agy_decide("view_file", {"AbsolutePath": f"{HOME}/.ssh/id_rsa"})[0] == "deny"
    assert agy.agy_decide("run_command", {"CommandLine": "type %USERPROFILE%\\.aws\\credentials"})[0] == "deny"
    assert agy.agy_decide("list_dir", {"DirectoryPath": f"{HOME}/AppData/Local/Google/Chrome/User Data/Default/Login Data"})[0] == "deny"


def test_projects_cannot_point_at_protected_folders():
    assert projects.path_problem(f"{HOME}/AppData/Roaming/gnupg")
    assert projects.path_problem(f"{HOME}/.kube")


# ------------------------------------------------------------------ secret isolation

def test_log_lines_are_redacted():
    conf.log("sectest", "key sk-abcdefghijklmnopqrstuvwxyz0123 and AKIAABCDEFGHIJKLMNOP and ghp_" + "a" * 30)
    line = (conf.LOGS_DIR / "sectest.log").read_text(encoding="utf-8")
    assert "sk-abc" not in line and "AKIA" not in line and "ghp_aaa" not in line
    assert line.count("[redacted]") == 3


def test_memory_uses_the_same_secret_pattern():
    from helios import memory
    assert memory._SECRET_RE is conf.SECRET_RE


# ------------------------------------------------------------------ preserved protections

def test_preserved_protections(gate, monkeypatch, tmp_path):
    flag = tmp_path / "abort.flag"
    flag.write_text("stop")
    monkeypatch.setattr(conf, "ABORT_FLAG", flag)
    assert gate.decide("mcp__computer__click", {"x": 1, "y": 1})[0] == "deny"          # panic stop
    flag.unlink()
    assert gate.decide("WebFetch", {"url": "http://192.168.1.1/admin"})[0] == "deny"  # SSRF
    assert gate.decide("Write", {"file_path": f"{HOME}/.claude/x.md"})[0] == "deny"   # ~/.claude
    assert gate.decide("mcp__computer__hotkey", {"keys": ["win", "l"]})[0] == "deny"  # lock-screen combo
    # an "ask" nobody can answer is a deny (tool approval / denial on policy failure)
    assert gate.decide("PowerShell", {"command": "Remove-Item C:/tmp/x"})[0] == "deny"


def test_gate_crash_means_deny(monkeypatch, capsys):
    mod = _load("pretooluse_crash", "hooks/pretooluse.py")
    monkeypatch.setattr(mod, "decide", lambda *a: 1 / 0)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps({"tool_name": "Read"})))
    with pytest.raises(SystemExit):
        mod.main()
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_agy_gate_process_tolerates_a_bom_and_still_denies_protected(tmp_path):
    import subprocess
    import sys
    payload = json.dumps({"toolCall": {"name": "view_file", "args": {"AbsolutePath": f"{HOME}/.ssh/id_rsa"}}})
    r = subprocess.run([sys.executable, str(_ROOT / "hooks" / "agy_pretool.py")],
                       input=b"\xef\xbb\xbf" + payload.encode(), capture_output=True, timeout=60)
    out = json.loads(r.stdout.decode())
    assert out["decision"] == "deny" and "SSH keys" in out["reason"]     # parsed, then the rail


# ------------------------------------------------------------------ self-check

def test_security_self_check_runs_clean_on_this_repo(monkeypatch):
    from helios import security
    checks = {n: (s, d) for n, s, d in security.self_check()}
    assert checks["permission gate"][0] == "ok"
    assert checks["git hygiene"][0] == "ok"
    assert checks["protected paths"][0] == "ok"
    assert all(s in ("ok", "warn", "fail") for s, _ in checks.values())
