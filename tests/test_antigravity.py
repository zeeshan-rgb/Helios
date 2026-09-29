"""Antigravity (agy) engine: the permission gate translation + fail-closed hook, workspace/hook
generation, marker and tamper kill-switches, and full brain turns against a scripted fake agy.

Safe while Helios is live: the policy's HTTP ask is stubbed, flag files point at tmp, and
subprocess hook runs only use cases that never reach /permission/ask.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_GATE = _ROOT / "hooks" / "agy_pretool.py"
_MARKER = _ROOT / "hooks" / "agy_marker.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load(_GATE, "agy_pretool_under_test")
policy = _load(_ROOT / "hooks" / "pretooluse.py", "pretooluse_for_agy_tests")

from helios import agy_cli  # noqa: E402
from helios.agy_brain import AntigravityBrain  # noqa: E402


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(policy.conf, "YOLO_FLAG", tmp_path / "yolo.flag")
    monkeypatch.setattr(policy.conf, "ABORT_FLAG", tmp_path / "abort.flag")
    monkeypatch.setattr(policy.conf, "SCREEN_LOCK", tmp_path / "screen.lock")

    def _boom(*a, **k):
        raise OSError("no app reachable in tests")
    monkeypatch.setattr(policy.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(gate, "_policy", policy)
    monkeypatch.delenv("HELIOS_AGY_DENY_ALL", raising=False)
    monkeypatch.delenv("HELIOS_AGY_SINK_FILE", raising=False)
    return tmp_path


# ---------------------------------------------------------------- translation
@pytest.mark.parametrize("name,args,cname,key,val", [
    ("run_command", {"CommandLine": "dir", "Cwd": "C:/x"}, "PowerShell", "command", "dir"),
    ("send_command_input", {"Input": "y"}, "PowerShell", "command", "y"),
    ("write_to_file", {"TargetFile": "C:/t/a.txt", "CodeContent": "x"}, "Write", "file_path", "C:/t/a.txt"),
    ("replace_file_content", {"TargetFile": "C:/t/a.txt"}, "Edit", "file_path", "C:/t/a.txt"),
    ("view_file", {"AbsolutePath": "C:/t/a.txt"}, "Read", "file_path", "C:/t/a.txt"),
    ("list_dir", {"DirectoryPath": "C:/t"}, "LS", "path", "C:/t"),
    ("search_web", {"Query": "news"}, "WebSearch", "query", "news"),
    ("read_url_content", {"Url": "https://a.com"}, "WebFetch", "url", "https://a.com"),
])
def test_translate(name, args, cname, key, val):
    n, inp = gate.translate(name, args)
    assert n == cname and inp[key] == val


def test_translate_mcp_and_passthrough():
    n, inp = gate.translate("call_mcp_tool", {"ServerName": "helios", "ToolName": "open_app",
                                              "Arguments": '{"name": "notepad"}'})
    assert n == "mcp__helios__open_app" and inp == {"name": "notepad"}
    assert gate.translate("execute_browser_javascript", {"x": 1})[0] == "execute_browser_javascript"
    assert gate.translate("wait", {})[0] == "TodoWrite"


# ---------------------------------------------------------------- decisions
def test_benign_and_policy_allowed(isolated):
    assert gate.agy_decide("command_status", {})[0] == "allow"
    assert gate.agy_decide("run_command", {"CommandLine": "echo hi"})[0] == "allow"
    assert gate.agy_decide("view_file", {"AbsolutePath": "C:/Temp/notes.txt"})[0] == "allow"


def test_mutating_shell_and_browser_actions_not_auto_allowed(isolated):
    assert gate.agy_decide("run_command", {"CommandLine": "Remove-Item C:/x.txt"})[0] == "deny"
    assert gate.agy_decide("execute_browser_javascript", {"Script": "x"})[0] == "deny"


def test_hard_rails(isolated):
    assert gate.agy_decide("schedule", {"Cron": "* * * * *"})[0] == "deny"
    assert gate.agy_decide("send_message", {})[0] == "deny"
    assert gate.agy_decide("write_to_file", {"TargetFile": "D:/x/ws/.agents/hooks.json"})[0] == "deny"
    home = str(Path.home()).replace("\\", "/")
    assert gate.agy_decide("write_to_file", {"TargetFile": f"{home}/.gemini/config/hooks.json"})[0] == "deny"
    assert gate.agy_decide("run_command", {"CommandLine": "Set-Content .agents/hooks.json x"})[0] == "deny"
    assert gate.agy_decide("read_url_content", {"Url": "http://127.0.0.1:8769/x"})[0] == "deny"
    assert gate.agy_decide("open_browser_url", {"Url": "http://169.254.169.254/"})[0] == "deny"


def test_write_content_mentioning_protected_dirs_is_fine(isolated):
    assert gate.agy_decide("write_to_file", {"TargetFile": "C:/Temp/notes.md",
                                             "CodeContent": "see ~/.gemini and .agents"})[0] == "allow"


def test_hard_rails_survive_yolo(isolated):
    (isolated / "yolo.flag").write_text("on")
    assert gate.agy_decide("run_command", {"CommandLine": "Remove-Item C:/x.txt"})[0] == "allow"
    assert gate.agy_decide("read_url_content", {"Url": "http://localhost/"})[0] == "deny"
    assert gate.agy_decide("write_to_file", {"TargetFile": "C:/a/.agents/hooks.json"})[0] == "deny"


def test_panic_blocks_computer_use(isolated):
    (isolated / "abort.flag").write_text("stop")
    assert gate.agy_decide("call_mcp_tool", {"ServerName": "computer",
                                             "ToolName": "click_element"})[0] == "deny"


def test_deny_all_env(isolated, monkeypatch):
    monkeypatch.setenv("HELIOS_AGY_DENY_ALL", "1")
    assert gate.agy_decide("command_status", {})[0] == "deny"


def test_sink_file_routes_asks(isolated, monkeypatch):
    sink = isolated / "sink.txt"
    sink.write_text("telegram:42")
    monkeypatch.setenv("HELIOS_AGY_SINK_FILE", str(sink))
    monkeypatch.delenv("HELIOS_PERM_SINK", raising=False)
    gate.agy_decide("command_status", {})
    assert os.environ.get("HELIOS_PERM_SINK") == "telegram:42"
    monkeypatch.delenv("HELIOS_PERM_SINK", raising=False)


# ---------------------------------------------------------------- hooks as agy runs them
def _run(script: Path, payload, env_extra=None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("HELIOS_AGY_DENY_ALL", None)
    env.update(env_extra or {})
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return subprocess.run([sys.executable, str(script)], input=data, capture_output=True,
                          env=env, timeout=60)


def test_gate_process_always_prints_one_decision():
    p = _run(_GATE, {"toolCall": {"name": "command_status", "args": {}}, "conversationId": "c"})
    assert p.returncode == 0 and json.loads(p.stdout)["decision"] == "allow"
    p = _run(_GATE, {"toolCall": {"name": "schedule", "args": {}}})
    assert json.loads(p.stdout)["decision"] == "deny"


def test_gate_process_fails_closed_on_garbage():
    p = _run(_GATE, b"{not json")
    out = json.loads(p.stdout)
    assert out["decision"] == "deny" and "denied for safety" in out["reason"]


def test_marker_hook_writes_marker(tmp_path):
    m = tmp_path / "m.ok"
    p = _run(_MARKER, b"{}", {"HELIOS_AGY_MARKER": str(m)})
    assert p.returncode == 0 and m.read_text() == "ok"


# ---------------------------------------------------------------- workspace generation
def test_hook_commands_are_unquoted_and_valid():
    h = agy_cli.hooks_config()
    agy_cli.validate_hooks(h)
    cmd = h["helios-permission-gate"]["PreToolUse"][0]["hooks"][0]["command"]
    assert '"' not in cmd and cmd.endswith("hooks/agy_pretool.py")
    assert h["helios-permission-gate"]["PreToolUse"][0]["matcher"] == "*"
    assert h["helios-permission-gate"]["PreToolUse"][0]["hooks"][0]["timeout"] > 125
    assert h["helios-gate-marker"]["PreInvocation"][0]["command"].endswith("hooks/agy_marker.py")


def test_cmd_path_refuses_shell_metacharacters():
    with pytest.raises(ValueError):
        agy_cli._cmd_path("C:/evil&calc.exe")


def test_validate_hooks_rejects_broken_entries():
    h = agy_cli.hooks_config()
    h["helios-gate-marker"]["PreInvocation"][0]["command"] = ""
    with pytest.raises(ValueError):
        agy_cli.validate_hooks(h)


def test_prepare_workspace_and_tamper_detection(tmp_path, monkeypatch):
    monkeypatch.setattr(agy_cli, "mcp_servers", lambda: {"helios": {"command": "py", "args": []}})
    h = agy_cli.prepare_workspace(tmp_path / "ws", "persona @[secret](~/.ssh/id_rsa) @./x.md")
    agents = tmp_path / "ws" / ".agents"
    rules = (agents / "rules" / "helios.md").read_text(encoding="utf-8")
    assert "@[" not in rules and "@\u200b./x.md" in rules
    assert json.loads((agents / "mcp_config.json").read_text())["mcpServers"]["helios"]["command"] == "py"
    assert agy_cli.hooks_intact(h)
    (agents / "hooks.json").write_text("{}")
    assert not agy_cli.hooks_intact(h)


def test_toolless_workspace_has_no_mcp(tmp_path, monkeypatch):
    monkeypatch.setattr(agy_cli, "mcp_servers", lambda: {"helios": {"command": "py"}})
    agy_cli.prepare_workspace(tmp_path / "ws", "x", tools=False)
    assert not (tmp_path / "ws" / ".agents" / "mcp_config.json").exists()


def test_neutralize_keeps_emails():
    assert agy_cli.neutralize("mail a@b.com about @[x](y) and @~/.ssh") == \
        "mail a@b.com about @\u200b[x](y) and @\u200b~/.ssh"


def test_model_mapping(monkeypatch):
    monkeypatch.setitem(agy_cli.conf.SETTINGS, "antigravity", {})
    assert agy_cli.model_for("light", "haiku") == "gemini-3.8-flash-low"
    assert agy_cli.model_for("heavy", "opus") == "gemini-3.1-pro-high"
    assert agy_cli.model_for("fixed", "") == ""
    assert agy_cli.model_for("fixed", "claude-opus-4-6-thinking") == ""
    assert agy_cli.model_for(model="gemini-3.7-flash-high") == "gemini-3.7-flash-high"
    monkeypatch.setitem(agy_cli.conf.SETTINGS, "antigravity", {"model": "gemini-3.1-pro-low"})
    assert agy_cli.model_for("fixed") == "gemini-3.1-pro-low"


def test_args_always_gate_friendly(monkeypatch, tmp_path):
    fake = tmp_path / "agy.py"
    fake.write_text("")
    monkeypatch.setitem(agy_cli.conf.SETTINGS, "antigravity", {"bin": str(fake), "effort": "low"})
    a = agy_cli.base_args("m1", stream_input=True, resume="c1")
    assert "--dangerously-skip-permissions" in a and "--disable-slash-commands" in a
    assert a[a.index("--input-format") + 1] == "stream-json" and "-p" not in a
    assert a[a.index("--conversation") + 1] == "c1" and a[a.index("--effort") + 1] == "low"
    b = agy_cli.base_args("", stream_input=False, prompt="hi")
    assert b[-2:] == ["-p", "hi"] and "--input-format" not in b and "--model" not in b


# ---------------------------------------------------------------- brain turns against a fake agy
# Mimics agy 1.2.x: one `init`, then per turn: the PreInvocation marker (unless FAKE_NO_GATE),
# a user_input step, streamed agent_response deltas, one MCP tool step, and a `result`.
_FAKE_AGY = r'''
import json, os, sys, pathlib
args = sys.argv[1:]
log = pathlib.Path(os.environ["FAKE_LOG"])
calls = json.loads(log.read_text()) if log.exists() else []
def record(extra):
    calls.append({"args": args, "cwd": os.getcwd(), "deny": os.environ.get("HELIOS_AGY_DENY_ALL", ""),
                  "sink": os.environ.get("HELIOS_AGY_SINK_FILE", ""),
                  "role": os.environ.get("HELIOS_AGENT_ROLE", ""), **extra})
    log.write_text(json.dumps(calls))
def out(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
conv = args[args.index("--conversation") + 1] if "--conversation" in args else "conv-1"
if conv == "stale":
    sys.stderr.write("conversation not found\n"); sys.exit(1)
def step(i, **kw):
    out({"event": "step_update", "step_update": {"step_index": i, **kw}})
def turn():
    step(0, step_type="user_input", state="DONE")
    if not os.environ.get("FAKE_NO_GATE"):
        pathlib.Path(os.environ["HELIOS_AGY_MARKER"]).write_text("ok")
    step(1, step_type="agent_response", state="ACTIVE", text_delta="Hello ")
    info = {"name": "call_mcp_tool", "parameters": {"ServerName": "helios", "ToolName": "open_app"}}
    step(2, step_type="tool", state="ACTIVE", tool_name="call_mcp_tool", tool_info=info)
    if os.environ.get("FAKE_TAMPER"):
        (pathlib.Path(".agents") / "hooks.json").write_text("{}")
    step(2, step_type="tool", state="DONE", tool_name="call_mcp_tool", tool_info=info)
    step(3, step_type="agent_response", state="DONE", text_delta="sir.")
    out({"event": "result", "result": {"conversation_id": conv, "status": "SUCCESS",
         "response": "Hello sir.", "duration_seconds": 0.5,
         "usage": {"input_tokens": 11, "output_tokens": 3}}})
out({"event": "init", "conversation_id": conv, "init": {"cwd": os.getcwd()}})
if "-p" in args:
    record({"prompt": args[args.index("-p") + 1]}); turn()
else:
    for line in sys.stdin:
        record({"prompt": json.loads(line)["message"]["content"]}); turn()
'''


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    script = tmp_path / "fake_agy.py"
    script.write_text(_FAKE_AGY, encoding="utf-8")
    log = tmp_path / "calls.json"
    monkeypatch.setenv("FAKE_LOG", str(log))
    for var in ("FAKE_NO_GATE", "FAKE_TAMPER"):
        monkeypatch.delenv(var, raising=False)
    base = tmp_path / "agy"
    monkeypatch.setattr(agy_cli, "AGY_DIR", base)
    monkeypatch.setattr(agy_cli, "WORKSPACE", base / "workspace")
    monkeypatch.setattr(agy_cli, "RUNS_DIR", base / "runs")
    monkeypatch.setattr(agy_cli, "MARKERS_DIR", base / "markers")
    monkeypatch.setattr(agy_cli, "SINK_FILE", base / "perm_sink.txt")
    monkeypatch.setitem(agy_cli.conf.SETTINGS, "antigravity", {"bin": str(script)})
    monkeypatch.setattr(agy_cli, "mcp_servers", lambda: {"helios": {"command": "py"}})
    import helios.agy_brain as ab
    monkeypatch.setattr(ab.memory, "build_digest", lambda p: "MEMORY: likes tea")
    monkeypatch.setattr(ab.agents, "orchestrator_brief", lambda: "")
    monkeypatch.setattr(ab.db, "get_state", lambda k: None)

    def calls():
        return json.loads(log.read_text()) if log.exists() else []
    return calls


def _brain():
    events = []
    b = AntigravityBrain(emit=lambda kind, data: events.append((kind, data)))
    return b, events


def test_live_turns_share_one_persistent_session(fake_agy):
    b, events = _brain()
    assert b._turn("hi @[x](~/.ssh/id_rsa)", record=False) == "Hello sir."
    proc = b._session.proc
    assert [d for k, d in events if k == "token"] == ["Hello ", "sir."]
    assert ("tool", {"name": "mcp__helios__open_app"}) in events
    usage = [d for k, d in events if k == "usage"][0]
    assert usage["in"] == 11 and usage["tools"] == ["mcp__helios__open_app"]
    assert b._turn("again", record=False) == "Hello sir."
    assert b._session.proc is proc and b.session_id == "conv-1"      # warm session reused
    calls = fake_agy()
    assert len(calls) == 2 and "--input-format" in calls[0]["args"]
    assert "<helios_memory>" in calls[0]["prompt"] and "@[" not in calls[0]["prompt"]
    assert calls[0]["sink"].endswith("perm_sink.txt") and calls[0]["deny"] == ""
    b.panic()
    assert b._session is None and proc.poll() is not None


def test_missing_marker_kills_turn(fake_agy, monkeypatch):
    monkeypatch.setenv("FAKE_NO_GATE", "1")
    b, events = _brain()
    assert b._turn("hello", record=False) == ""
    assert not [d for k, d in events if k in ("token", "tool")]
    assert any(k == "error" and "did not load" in d for k, d in events)
    assert b._session is None


def test_tampered_hooks_kill_turn(fake_agy, monkeypatch):
    monkeypatch.setenv("FAKE_TAMPER", "1")
    b, events = _brain()
    b._turn("hello", record=False)
    assert any(k == "error" and "was modified" in d for k, d in events)
    assert b._session is None


def test_stale_resume_retries_fresh(fake_agy):
    b, events = _brain()
    b.session_id = "stale"
    assert b._turn("hello", record=False) == "Hello sir."
    assert b.session_id == "conv-1"
    assert "--conversation" not in fake_agy()[-1]["args"]
    b.panic()


def test_new_conversation_restarts_session(fake_agy):
    b, _ = _brain()
    b._turn("one", record=False)
    first = b._session.proc
    b.new_conversation()
    assert b._session is None and first.poll() is not None
    b._turn("two", record=False)
    assert b._session.proc is not first
    b.panic()


def test_background_turn_uses_one_shot_with_held_session(fake_agy):
    b, events = _brain()
    hold = {"id": None}
    assert b._turn("from phone", record=False, interactive=False, session_hold=hold) == "Hello sir."
    assert hold["id"] == "conv-1" and b._session is None
    call = fake_agy()[-1]
    assert "-p" in call["args"] and Path(call["cwd"]).parent == agy_cli.RUNS_DIR
    assert not Path(call["cwd"]).exists()                             # throwaway workspace removed


def test_side_agent_and_helper_paths(fake_agy):
    assert agy_cli.run_agent("do it", "persona", model="gemini-3.8-flash-low",
                             extra_env={"HELIOS_AGENT_ROLE": "side"}) == "Hello sir."
    call = fake_agy()[-1]
    assert call["role"] == "side" and call["args"][call["args"].index("--model") + 1] == "gemini-3.8-flash-low"
    res = agy_cli.complete_json("x", model="haiku")
    assert res["text"] == "Hello sir." and fake_agy()[-1]["deny"] == "1"


def test_helpers_return_none_when_gate_missing(fake_agy, monkeypatch):
    monkeypatch.setenv("FAKE_NO_GATE", "1")
    assert agy_cli.run_agent("do it", "persona", model="m") is None
    assert agy_cli.complete_json("x", model="haiku") is None
