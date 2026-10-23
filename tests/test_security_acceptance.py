"""Multimodal-spec phase 13 — security acceptance (spec §29 K) through the REAL gate, plus the new
surfaces added since the last security pass (perceive / locate, dashboard routes, browser actions,
public MCP), and a generic guard that every server route needs the token."""

from __future__ import annotations

import http.client
import importlib.util
import json
import re
from pathlib import Path

import pytest

from helios import conf, permissions

_ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home().as_posix()


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gate(monkeypatch):
    """The real PreToolUse gate with the Approve/Deny prompt unreachable (nobody to say yes)."""
    mod = _load("pretooluse_accept", "hooks/pretooluse.py")
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("app not running")))
    return mod


@pytest.fixture
def yolo(monkeypatch, tmp_path):
    f = tmp_path / "yolo.flag"
    f.write_text("on")
    monkeypatch.setattr(conf, "YOLO_FLAG", f)
    return f


# ------------------------------------------------------------------ K1: send a message automatically → BLOCKED

@pytest.mark.parametrize("tool,args", [
    ("mcp__helios__gmail_send_draft", {"n": 1}),
    ("mcp__helios__browser_click", {"ref": "e4", "confirm": True}),
    ("mcp__helios__browser_type", {"ref": "e2", "text": "hi", "submit": True, "confirm": True}),
])
def test_sending_never_happens_on_its_own(gate, yolo, monkeypatch, tool, args):
    assert gate.decide(tool, args)[0] == "deny"                       # YOLO can't auto-send; nobody approved
    monkeypatch.setenv("HELIOS_AGENT_ROLE", "side")
    v, why = gate.decide(tool, args)
    assert v == "deny" and "background agent" in why                  # background agents: hard no


def test_there_is_no_tool_that_messages_prospects_or_leads():
    src = (_ROOT / "mcp" / "helios_server.py").read_text(encoding="utf-8")
    names = set(re.findall(r"^def (\w+)\(", src, re.M))
    assert not [n for n in names if re.search(r"(whatsapp|send_(email|message|pitch)|contact_(lead|prospect))", n)]
    # SMS exists (owner + people the owner added with consent) — but no SMS tool can take a phone
    # number, sending is always the owner's Approve/Deny (never YOLO, never a background agent), and a
    # lead/prospect number can never become a recipient (tests/test_sms.py::test_business_leads_*).
    sms_tools = {n for n in names if "sms" in n}
    assert sms_tools == {"send_sms", "sms_status", "sms_log", "sms_off"}
    for n in sms_tools:
        sig = re.search(rf"^def {n}\(([^)]*)\)", src, re.M).group(1)
        assert not re.search(r"\b(to|number|phone|phone_number|recipient_number)\b\s*:", sig), n
    assert permissions.classify("mcp__helios__send_sms", {}) == "ask"
    assert permissions.is_outbound_send("mcp__helios__send_sms") and permissions.never_yolo("mcp__helios__send_sms", {})
    public = (_ROOT / "mcp" / "helios_public_server.py").read_text(encoding="utf-8")
    assert "gmail" not in public and "send_draft" not in public and "sms" not in public.lower()


# ------------------------------------------------------------------ K2: access an API key → BLOCKED

@pytest.mark.parametrize("tool,args", [
    ("Read", {"file_path": str(_ROOT / "config" / "secrets.toml")}),
    ("PowerShell", {"command": f"Get-Content {(_ROOT / 'config' / 'secrets.toml').as_posix()}"}),
    ("Read", {"file_path": "C:/code/app/.env"}),
    ("Glob", {"pattern": f"{HOME}/.aws/*"}),
])
def test_secrets_are_blocked_even_in_yolo(gate, yolo, tool, args):
    v, why = gate.decide(tool, args)
    assert v == "deny" and why.startswith("blocked")


def test_agy_native_tools_hit_the_same_secret_rail():
    agy = _load("agy_pretool_accept", "hooks/agy_pretool.py")
    assert agy.agy_decide("view_file", {"AbsolutePath": (_ROOT / "config" / "secrets.toml").as_posix()})[0] == "deny"


def test_secrets_never_reach_memory_or_logs():
    from helios import memory_store
    assert memory_store.looks_secret("my api key is AIzaSyA1234567890abcdefghijklmnop")
    conf.log("acceptance", "token ghp_" + "b" * 30)
    assert "ghp_bbb" not in (conf.LOGS_DIR / "acceptance.log").read_text(encoding="utf-8")


# ------------------------------------------------------------------ K3: destructive op without approval → policy

@pytest.mark.parametrize("cmd", ["Remove-Item -Recurse -Force D:/Projects/app",
                                 "git push --force origin main",
                                 # found by this suite: read-allowlist prefixes that hid destruction
                                 "Format-Volume -DriveLetter E", "format-volume -FileSystem NTFS",
                                 "find . -name '*.log' -delete", "find /c/work -exec rm {} +",
                                 "wmic useraccount where name='guest' delete", "wmic process where name='x' call terminate",
                                 "git branch -D main", "git branch --delete feature", "git branch -m old new",
                                 "git remote remove origin", "git remote set-url origin https://evil.example/x.git"])
def test_destructive_commands_need_approval(gate, cmd):
    assert permissions.classify("PowerShell", {"command": cmd}) == "ask", cmd
    v, why = gate.decide("PowerShell", {"command": cmd})
    assert v == "deny" and "unreachable" in why                       # asked, nobody answered → denied


@pytest.mark.parametrize("cmd", ["Format-Table -InputObject $p", "Format-List -InputObject $x", "format-hex file.bin",
                                 "find . -name '*.py'", "wmic cpu get name", "wmic os list brief",
                                 "git branch", "git branch -a", "git branch --list", "git remote -v",
                                 "git remote show origin"])
def test_read_only_commands_stay_autonomous(cmd):
    assert permissions.classify("PowerShell", {"command": cmd}) == "allow", cmd


# ------------------------------------------------------------------ new surfaces

def test_new_read_tools_are_allowed_and_not_outbound(gate):
    for t in ("perceive", "locate", "screen_context", "find_ui_element", "web_extract"):
        assert gate.decide(f"mcp__helios__{t}", {"query": "Save"})[0] == "allow", t
        assert not permissions.is_outbound_send(f"mcp__helios__{t}")


def test_prospect_approval_always_asks(gate, yolo):
    assert permissions.classify("mcp__helios__prospect_approve", {"prospect_id": 1}) == "ask"


def _routes():
    src = (_ROOT / "helios" / "server.py").read_text(encoding="utf-8")
    get_src, _, post_src = src.partition("def do_POST")
    get_paths = set(re.findall(r'path == "(/[\w/.-]*)"', get_src))
    post_paths = set(re.findall(r'path == "(/[\w/.-]*)"', post_src.split("def _dashboard_get")[0]))
    dash = set(re.findall(r'name == "(\w+)"', src))
    return get_paths, post_paths, dash


@pytest.fixture
def srv(tmp_path, monkeypatch):
    from helios import dashboard, server
    monkeypatch.setattr(conf, "PORT", 8814)
    monkeypatch.setattr(conf, "auth_token", lambda: "tok-accept")
    monkeypatch.setattr(dashboard, "engine_health", lambda force=False: {"engine": "x", "ok": None, "models": []})

    class Brain:
        def busy(self):
            return False

        def __getattr__(self, n):
            return lambda *a, **k: None
    s = server.start(Brain(), None, server.Hub())
    yield 8814
    s.shutdown()


def _status(port, method, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=b"{}" if method == "POST" else None,
              headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json", **(headers or {})})
    r = c.getresponse()
    r.read()
    return r.status


PUBLIC = {"/", "/index.html", "/orb", "/health"}


def test_every_route_needs_the_token(srv):
    get_paths, post_paths, dash = _routes()
    assert len(get_paths) > 10 and len(post_paths) > 10 and "summary" in dash
    for p in sorted(get_paths - PUBLIC) + [f"/dashboard/{d}" for d in sorted(dash)] + ["/model/abc", "/events"]:
        assert _status(srv, "GET", p) == 401, p
    for p in sorted(post_paths):
        assert _status(srv, "POST", p) == 401, p
        assert _status(srv, "POST", p, {"X-Auth-Token": "tok-accept", "Origin": "http://evil.example"}) == 403, p


def test_foreign_host_is_refused_before_anything(srv):
    assert _status(srv, "GET", "/health", {"Host": "evil.example"}) == 403
