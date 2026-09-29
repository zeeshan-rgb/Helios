"""Gemini CLI engine: the permission-gate translation, fail-closed hook behaviour, per-run
settings, prompt escaping, model mapping and the stream-json turn loop (against a fake CLI).

Safe to run while Helios is live: the policy's HTTP ask is stubbed, flag files point at tmp, and
the end-to-end hook runs only use cases that never reach /permission/ask.
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
_HOOK = _ROOT / "hooks" / "gemini_pretool.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load(_HOOK, "gemini_pretool_under_test")
policy = _load(_ROOT / "hooks" / "pretooluse.py", "pretooluse_for_gemini_tests")

from helios import gemini_cli  # noqa: E402
from helios.gemini_brain import GeminiBrain, claude_style_name  # noqa: E402


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(policy.conf, "YOLO_FLAG", tmp_path / "yolo.flag")
    monkeypatch.setattr(policy.conf, "ABORT_FLAG", tmp_path / "abort.flag")
    monkeypatch.setattr(policy.conf, "SCREEN_LOCK", tmp_path / "screen.lock")

    def _boom(*a, **k):
        raise OSError("no app reachable in tests")
    monkeypatch.setattr(policy.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(gate, "_policy", policy)
    monkeypatch.delenv("GEMINI_CLI_HELIOS_DENY_ALL", raising=False)
    return tmp_path


# ---------------------------------------------------------------- translation
@pytest.mark.parametrize("gname,ginp,cname,key,val", [
    ("run_shell_command", {"command": "dir"}, "PowerShell", "command", "dir"),
    ("write_file", {"file_path": "C:/t/a.txt", "content": "x"}, "Write", "file_path", "C:/t/a.txt"),
    ("replace", {"file_path": "C:/t/a.txt", "old_string": "a", "new_string": "b"},
     "Edit", "file_path", "C:/t/a.txt"),
    ("read_file", {"file_path": "C:/t/a.txt"}, "Read", "file_path", "C:/t/a.txt"),
    ("list_directory", {"dir_path": "C:/t"}, "LS", "path", "C:/t"),
    ("grep_search", {"pattern": "x", "dir_path": "C:/t"}, "Grep", "pattern", "x"),
    ("google_web_search", {"query": "news"}, "WebSearch", "query", "news"),
])
def test_translate_builtin_tools(gname, ginp, cname, key, val):
    name, inp = gate.translate(gname, ginp)
    assert name == cname and inp[key] == val


def test_translate_mcp_uses_context_then_name():
    assert gate.translate("mcp_helios_open_app", {"name": "x"},
                          {"server_name": "helios", "tool_name": "open_app"})[0] == "mcp__helios__open_app"
    assert gate.translate("mcp_computer_click_element", {})[0] == "mcp__computer__click_element"


def test_translate_web_fetch_extracts_urls():
    name, inp = gate.translate("web_fetch", {"prompt": "summarize https://a.com/x and http://b.org"})
    assert name == "WebFetch" and inp["url"] == "https://a.com/x" and len(inp["urls"]) == 2


# ---------------------------------------------------------------- decisions
def test_benign_and_reads_allowed(isolated):
    assert gate.gemini_decide("write_todos", {"todos": []})[0] == "allow"
    assert gate.gemini_decide("read_file", {"file_path": "C:/Temp/notes.txt"})[0] == "allow"


def test_mutating_shell_is_not_auto_allowed(isolated):
    assert gate.gemini_decide("run_shell_command", {"command": "Remove-Item C:/x.txt"})[0] == "deny"


def test_hard_rails(isolated):
    assert gate.gemini_decide("save_memory", {"fact": "x"})[0] == "deny"
    home = str(Path.home()).replace("\\", "/")
    assert gate.gemini_decide("write_file", {"file_path": f"{home}/.gemini/settings.json"})[0] == "deny"
    assert gate.gemini_decide("web_fetch", {"prompt": "get http://127.0.0.1:8769/x"})[0] == "deny"
    assert gate.gemini_decide("web_fetch",
                              {"prompt": "https://ok.com then http://169.254.169.254/"})[0] == "deny"


def test_ssrf_and_gemini_config_survive_yolo(isolated):
    (isolated / "yolo.flag").write_text("on", encoding="utf-8")
    assert gate.gemini_decide("run_shell_command", {"command": "Remove-Item C:/x.txt"})[0] == "allow"
    assert gate.gemini_decide("web_fetch", {"prompt": "http://localhost/"})[0] == "deny"
    assert gate.gemini_decide("save_memory", {})[0] == "deny"


def test_panic_blocks_computer_use(isolated):
    (isolated / "abort.flag").write_text("stop", encoding="utf-8")
    assert gate.gemini_decide("mcp_computer_click_element", {"element_index": 1},
                              mcp_context={"server_name": "computer",
                                           "tool_name": "click_element"})[0] == "deny"


def test_deny_all_env(isolated, monkeypatch):
    monkeypatch.setenv("GEMINI_CLI_HELIOS_DENY_ALL", "1")
    assert gate.gemini_decide("read_file", {"file_path": "C:/Temp/a.txt"})[0] == "deny"


# ---------------------------------------------------------------- hook as Gemini runs it
def _run_hook(payload, env_extra=None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("GEMINI_CLI_HELIOS_DENY_ALL", None)
    env.update(env_extra or {})
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return subprocess.run([sys.executable, str(_HOOK)], input=data, capture_output=True,
                          env=env, timeout=60)


def test_hook_process_allow_prints_decision_exit_0():
    p = _run_hook({"tool_name": "write_todos", "tool_input": {}, "session_id": "t"})
    assert p.returncode == 0
    assert json.loads(p.stdout)["decision"] == "allow"


def test_hook_process_deny_exits_2():
    p = _run_hook({"tool_name": "save_memory", "tool_input": {}})
    assert p.returncode == 2
    assert json.loads(p.stdout)["decision"] == "deny"


def test_hook_process_fails_closed_on_garbage():
    p = _run_hook(b"{not json")
    assert p.returncode == 2
    assert json.loads(p.stdout)["decision"] == "deny"


def test_hook_process_single_json_document():
    """Gemini parses the whole stdout as one JSON value; nothing else may be printed."""
    p = _run_hook({"tool_name": "web_fetch", "tool_input": {"prompt": "http://127.0.0.1/"}})
    out = json.loads(p.stdout)
    assert out["decision"] == "deny" and p.returncode == 2


# ---------------------------------------------------------------- plumbing
def test_escape_prompt_and_imports():
    assert gemini_cli.escape_prompt("read @C:/x and mail a@b.com") == \
        "read \\@C:/x and mail a\\@b.com"
    assert gemini_cli.escape_prompt("already \\@ok") == "already \\@ok"
    assert gemini_cli.neutralize_imports("see @./secret.md\n@notes x@y") == \
        "see @\u200b./secret.md\n@\u200bnotes x@y"


def test_model_mapping(monkeypatch):
    monkeypatch.setitem(gemini_cli.conf.SETTINGS, "gemini", {})
    assert gemini_cli.model_for("light", "haiku") == "flash-lite"
    assert gemini_cli.model_for("heavy", "opus") == "pro"
    assert gemini_cli.model_for(model="sonnet") == "flash"
    assert gemini_cli.model_for("medium", "claude-sonnet-5") == "flash"
    assert gemini_cli.model_for("fixed", "gemini-3.8-flash") == "gemini-3.8-flash"
    assert gemini_cli.model_for("fixed", "") == "auto"
    monkeypatch.setitem(gemini_cli.conf.SETTINGS, "gemini", {"heavy": "gemini-3.1-pro-preview"})
    assert gemini_cli.model_for("heavy", "opus") == "gemini-3.1-pro-preview"



@pytest.fixture
def gem_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(gemini_cli, "HOME", tmp_path / "home")
    monkeypatch.setattr(gemini_cli, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(gemini_cli, "WORKSPACE", tmp_path / "ws")
    monkeypatch.setattr(gemini_cli, "mcp_servers", lambda: {"helios": {"command": "py", "args": []}})
    return tmp_path


def test_settings_wire_gate_marker_and_keep_cli_keys(gem_dirs):
    sf = gemini_cli.settings_file()
    sf.parent.mkdir(parents=True)
    sf.write_text(json.dumps({"security": {"auth": {"selectedType": "oauth-personal"}},
                              "model": {"name": "keep-me"}}), encoding="utf-8")
    gemini_cli.ensure_settings()
    s = json.loads(sf.read_text(encoding="utf-8"))
    assert s["security"]["auth"]["selectedType"] == "oauth-personal"     # CLI-owned key kept
    assert s["model"]["name"] == "keep-me" and s["model"]["maxSessionTurns"] >= 1
    gate = s["hooks"]["BeforeTool"][0]
    assert gate["matcher"] == ".*" and s["hooksConfig"]["enabled"] is True
    assert "gemini_pretool.py" in gate["hooks"][0]["command"]
    assert gate["hooks"][0]["timeout"] > 125_000                          # outlasts the ask wait
    assert gemini_cli.MARKER_ENV in s["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert s["context"]["fileName"] == ["HELIOS.md"]
    assert "save_memory" in s["tools"]["exclude"]
    assert s["mcpServers"] == {"helios": {"command": "py", "args": []}}


def test_live_run_uses_stable_workspace(gem_dirs):
    run = gemini_cli.prepare_run("persona @./x.md", tools=True)
    assert run["cwd"] == gemini_cli.WORKSPACE and not run["isolated"]
    ctx = gemini_cli.WORKSPACE / "HELIOS.md"
    assert "@\u200b./x.md" in ctx.read_text(encoding="utf-8")
    a = gemini_cli.args(run, "flash", resume="abc")
    assert a[a.index("--approval-mode") + 1] == "yolo" and a[-2:] == ["--resume", "abc"]
    assert a[a.index("--allowed-mcp-server-names") + 1] == "helios"
    assert a[a.index("--output-format") + 1] == "stream-json"
    e = gemini_cli.env(run, {"HELIOS_PERM_SINK": "tg:1"})
    assert e["GEMINI_CLI_HOME"] == str(gemini_cli.HOME) and e["HELIOS_PERM_SINK"] == "tg:1"
    assert e[gemini_cli.MARKER_ENV] == str(run["marker"])
    assert gemini_cli.DENY_ALL_ENV not in e and "GEMINI_CLI_SYSTEM_SETTINGS_PATH" not in e
    Path(run["marker"]).write_text("ok")
    assert gemini_cli.gate_loaded(run)
    gemini_cli.cleanup(run)
    assert not ctx.exists() and not Path(run["marker"]).exists()
    assert gemini_cli.WORKSPACE.exists()                                   # sessions live on


def test_isolated_toolless_run(gem_dirs):
    run = gemini_cli.prepare_run("sys", tools=False, isolated=True)
    assert run["cwd"].parent == gemini_cli.RUNS_DIR and (run["cwd"] / "HELIOS.md").exists()
    a = gemini_cli.args(run, "flash-lite")
    assert "--include-directories" not in a
    assert a[a.index("--allowed-mcp-server-names") + 1] == "helios-none"
    assert gemini_cli.env(run)[gemini_cli.DENY_ALL_ENV] == "1"
    gemini_cli.cleanup(run)
    assert not run["cwd"].exists()


def test_convert_mcp_server_entries(tmp_path):
    assert gemini_cli._convert_server({"command": str(tmp_path / "missing.exe")}) is None
    assert gemini_cli._convert_server({"type": "http", "url": "https://x/mcp"}) == {"httpUrl": "https://x/mcp"}
    assert gemini_cli._convert_server({"type": "sse", "url": "https://x/sse"}) == {"url": "https://x/sse"}


def test_claude_style_names():
    servers = ["computer", "helios", "composio"]
    assert claude_style_name("mcp_computer_get_window_state", servers) == "mcp__computer__get_window_state"
    assert claude_style_name("mcp_helios_open_app", servers) == "mcp__helios__open_app"
    assert claude_style_name("read_file", servers) == "read_file"


# ---------------------------------------------------------------- full turns against a fake CLI
# Mimics the real CLI: the SessionStart hook writes the gate marker before `init` (unless
# FAKE_NO_GATE simulates settings that failed to load), then streams a reply with one tool call.
_FAKE_CLI = r"""
const fs = require('fs');
const args = process.argv.slice(2);
const input = fs.readFileSync(0, 'utf8');
fs.writeFileSync(process.env.FAKE_LOG, JSON.stringify({args, input, cwd: process.cwd(),
  home: process.env.GEMINI_CLI_HOME, deny: process.env.GEMINI_CLI_HELIOS_DENY_ALL || ''}));
const out = (o) => process.stdout.write(JSON.stringify(o) + '\n');
if (args.includes('--resume') && args[args.indexOf('--resume') + 1] === 'stale') {
  process.stderr.write('Error resuming session: not found\n'); process.exit(42);
}
if (!process.env.FAKE_NO_GATE) fs.writeFileSync(process.env.GEMINI_CLI_HELIOS_MARKER, 'ok');
out({type: 'init', session_id: 'sess-123', model: 'flash'});
out({type: 'message', role: 'user', content: input});
out({type: 'message', role: 'assistant', content: 'Hello ', delta: true});
out({type: 'tool_use', tool_name: 'mcp_helios_open_app', tool_id: 't1', parameters: {}});
if (process.env.FAKE_NO_GATE) fs.writeFileSync(process.env.FAKE_LOG + '.tool', 'ran');
out({type: 'tool_result', tool_id: 't1', status: 'success', output: 'ok'});
out({type: 'message', role: 'assistant', content: 'sir.', delta: true});
out({type: 'result', status: 'success', stats: {input_tokens: 11, output_tokens: 3, duration_ms: 42}});
"""


@pytest.fixture
def fake_cli(gem_dirs, monkeypatch):
    import shutil
    if not shutil.which("node"):
        pytest.skip("node not installed")
    script = gem_dirs / "fake_gemini.js"
    script.write_text(_FAKE_CLI, encoding="utf-8")
    log = gem_dirs / "call.json"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.delenv("FAKE_NO_GATE", raising=False)
    monkeypatch.setitem(gemini_cli.conf.SETTINGS, "gemini", {"bin": str(script)})
    import helios.gemini_brain as gb
    monkeypatch.setattr(gb.memory, "build_digest", lambda p: "MEMORY: none")
    monkeypatch.setattr(gb.agents, "orchestrator_brief", lambda: "")
    monkeypatch.setattr(gb.db, "get_state", lambda k: None)
    return log


def _brain():
    events = []
    b = GeminiBrain(emit=lambda kind, data: events.append((kind, data)))
    return b, events


def test_turn_streams_tokens_tools_usage_and_session(fake_cli):
    b, events = _brain()
    text = b._turn("say hi @C:/secret.txt", record=False, interactive=False,
                   session_hold={"id": None})
    assert text == "Hello sir."
    assert [d for k, d in events if k == "token"] == ["Hello ", "sir."]
    assert ("tool", {"name": "mcp__helios__open_app"}) in events
    assert events[-1] == ("done", {"text": "Hello sir."})
    call = json.loads(fake_cli.read_text(encoding="utf-8"))
    assert "\\@C:/secret.txt" in call["input"]                      # @path neutralized
    assert Path(call["cwd"]) == gemini_cli.WORKSPACE
    assert call["home"] == str(gemini_cli.HOME) and call["deny"] == ""
    assert not (gemini_cli.WORKSPACE / "HELIOS.md").exists()          # per-turn context cleaned


def test_turn_killed_when_gate_did_not_load(fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_NO_GATE", "1")
    b, events = _brain()
    text = b._turn("hello there", record=False, interactive=False, session_hold={"id": None})
    assert text == ""
    assert not [d for k, d in events if k in ("token", "tool")]
    assert any(k == "error" and "permission gate" in d for k, d in events)


def test_interactive_turn_keeps_session_and_resumes(fake_cli, monkeypatch):
    b, events = _brain()
    monkeypatch.setattr(b, "_writeback", lambda *a: None)
    b._turn("first", record=False, interactive=True)
    assert b.session_id == "sess-123"
    usage = [d for k, d in events if k == "usage"][0]
    assert usage["in"] == 11 and usage["out"] == 3 and usage["tools"] == ["mcp__helios__open_app"]
    b._turn("second", record=False, interactive=True)
    call = json.loads(fake_cli.read_text(encoding="utf-8"))
    assert call["args"][call["args"].index("--resume") + 1] == "sess-123"


def test_stale_resume_retries_fresh(fake_cli, monkeypatch):
    b, events = _brain()
    monkeypatch.setattr(b, "_writeback", lambda *a: None)
    b.session_id = "stale"
    text = b._turn("hello", record=False, interactive=True)
    assert text == "Hello sir." and b.session_id == "sess-123"
    call = json.loads(fake_cli.read_text(encoding="utf-8"))
    assert "--resume" not in call["args"]


def test_side_agent_and_helper_paths(fake_cli, monkeypatch):
    assert gemini_cli.run_agent("do it", "persona", model="flash", label="t") == "Hello sir."
    call = json.loads(fake_cli.read_text(encoding="utf-8"))
    assert Path(call["cwd"]).parent == gemini_cli.RUNS_DIR and not Path(call["cwd"]).exists()
    res = gemini_cli.complete_json("x", model="haiku")
    assert res["text"] == "Hello sir."
    call = json.loads(fake_cli.read_text(encoding="utf-8"))
    assert call["deny"] == "1" and call["args"][call["args"].index("--model") + 1] == "flash-lite"
    monkeypatch.setenv("FAKE_NO_GATE", "1")
    assert gemini_cli.run_agent("do it", "persona", model="flash", label="t") is None
    assert gemini_cli.complete_json("x", model="haiku") is None
