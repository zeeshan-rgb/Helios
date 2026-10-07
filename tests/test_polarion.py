"""Polarion connector: GET-only by construction, bearer token from secrets (never shown), clear
errors for missing / rejected tokens, input ids validated, results flagged as untrusted data,
the token prompt writes only its own line of secrets.toml, and the tools are wired + allowed."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import urllib.error
from pathlib import Path

import pytest

from helios import conf, permissions, polarion

_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ6ZWVzaGFuIn0.c2lnbmF0dXJlLXRlc3Q"


@pytest.fixture
def pol(tmp_path, monkeypatch):
    c = {"local_url": "http://localhost", "local_token": TOKEN, "server_url": "", "server_token": ""}
    monkeypatch.setattr(polarion, "cfg", lambda: c)
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Opener:
        def open(self, req, timeout=None):
            calls.append(req)
            body = replies.get(req.full_url.split("?")[0].split("/rest/v1")[-1])
            if isinstance(body, Exception):
                raise body
            return Resp(json.dumps(body or {"data": []}).encode())
    replies: dict = {}
    monkeypatch.setattr(polarion.urllib.request, "build_opener", lambda *a: Opener())
    return type("P", (), {"cfg": c, "calls": calls, "replies": replies, "tmp": tmp_path})


def test_reads_projects_with_bearer_get(pol):
    pol.replies["/projects"] = {"data": [{"type": "projects", "id": "DEMO",
                                          "attributes": {"name": "Demo Project"}}]}
    assert polarion.projects("local") == [{"id": "DEMO", "name": "Demo Project"}]
    req = pol.calls[0]
    assert req.get_method() == "GET" and req.full_url.startswith("http://localhost/polarion/rest/v1/projects")
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"


def test_search_and_item(pol):
    pol.replies["/projects/DEMO/workitems"] = {"data": [{"id": "DEMO/NM-12", "attributes": {
        "title": "Login fails", "type": "defect", "status": "open", "priority": "90.0",
        "updated": "2026-09-30T10:11:12Z"}}]}
    items = polarion.search("local", "DEMO", "type:defect AND status:open")
    assert items[0]["id"] == "NM-12" and "query=type%3Adefect" in pol.calls[0].full_url
    text = polarion.format_items(items)
    assert "not instructions" in text and "NM-12 [defect/open] Login fails" in text
    pol.replies["/projects/DEMO/workitems/NM-12"] = {"data": {"id": "DEMO/NM-12", "attributes": {
        "title": "Login fails", "type": "defect", "status": "open",
        "description": {"type": "text/html", "value": "<p>Steps:&nbsp;open app. key sk-abcdefghijklmnopqrstu</p>"},
        "assignee": "zeeshan"}}}
    full = polarion.format_item(polarion.item("local", "DEMO", "DEMO/NM-12"))
    assert "Steps: open app." in full and "<p>" not in full and "[redacted]" in full


@pytest.mark.parametrize("project", ["../admin", "a b", "", "x/../../y"])
def test_ids_are_validated(pol, project):
    with pytest.raises(polarion.PolarionError):
        polarion.search("local", project)
    assert not pol.calls


def test_missing_and_rejected_tokens(pol):
    with pytest.raises(polarion.PolarionError, match="no URL"):
        polarion.projects("server")
    pol.cfg["server_url"] = "https://polarion.example.com/polarion/"
    with pytest.raises(polarion.PolarionError, match="helios polarion token server"):
        polarion.projects("server")
    pol.cfg["server_token"] = TOKEN
    pol.replies["/projects"] = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
    with pytest.raises(polarion.PolarionError, match="token rejected") as e:
        polarion.projects("server")
    assert TOKEN not in str(e.value)
    assert pol.calls[-1].full_url.startswith("https://polarion.example.com/polarion/rest/v1/")
    rows = {r["instance"]: r for r in polarion.status()}
    assert rows["server"]["ok"] is False and "token rejected" in rows["server"]["detail"]
    assert TOKEN not in polarion.format_status(polarion.status())


def test_module_is_get_only():
    src = (_ROOT / "helios" / "polarion.py").read_text(encoding="utf-8")
    assert re.findall(r'method="([A-Z]+)"', src) == ["GET"]
    for verb in ("POST", "PATCH", "PUT", "DELETE"):
        assert f'"{verb}"' not in src


def test_save_token_edits_only_its_line(tmp_path, monkeypatch):
    monkeypatch.setattr(conf, "CONFIG_DIR", tmp_path)
    sec = tmp_path / "secrets.toml"
    sec.write_text('[telegram]\nbot_token = "keep-me"\n\n[polarion]\nlocal_token = "old"\n', encoding="utf-8")
    polarion.save_token("local", TOKEN)
    polarion.save_token("server", TOKEN + "x")
    text = sec.read_text(encoding="utf-8")
    assert 'bot_token = "keep-me"' in text and '"old"' not in text
    import tomllib
    data = tomllib.loads(text)
    assert data["polarion"] == {"local_token": TOKEN, "server_token": TOKEN + "x"}
    with pytest.raises(ValueError):
        polarion.save_token("local", 'abc" \n[evil]')
    with pytest.raises(ValueError):
        polarion.save_token("prod", TOKEN)


def test_save_token_creates_section(tmp_path, monkeypatch):
    monkeypatch.setattr(conf, "CONFIG_DIR", tmp_path)
    polarion.save_token("local", TOKEN)
    import tomllib
    assert tomllib.loads((tmp_path / "secrets.toml").read_text(encoding="utf-8"))["polarion"]["local_token"] == TOKEN


def test_mcp_tools_wired_and_read_only(pol):
    pol.replies["/projects"] = {"data": [{"id": "DEMO", "attributes": {"name": "Demo"}}]}
    spec = importlib.util.spec_from_file_location("helios_server_pol", _ROOT / "mcp" / "helios_server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    assert "DEMO: Demo" in srv.polarion_projects()
    assert "invalid project id" in srv.polarion_search("../x")
    for t in ("polarion_projects", "polarion_search", "polarion_item"):
        assert permissions.classify(f"mcp__helios__{t}", {}) == "allow"
    assert not any(n.startswith("polarion_") and n not in ("polarion_projects", "polarion_search", "polarion_item")
                   for n in dir(srv))


def test_secrets_file_is_protected_from_the_ai():
    from helios import protected
    assert protected.check(str(conf.CONFIG_DIR / "secrets.toml"))


def test_security_flags_tokens_in_settings(tmp_path, monkeypatch):
    from helios import security
    monkeypatch.setattr(conf, "CONFIG_DIR", tmp_path)
    (tmp_path / "settings.toml").write_text('[polarion]\nlocal_url = "http://localhost"\nlocal_token = ""\n',
                                            encoding="utf-8")
    assert security._check_settings_secrets()[0] == "ok"
    (tmp_path / "settings.toml").write_text('[polarion]\nlocal_token = "abc123"\n', encoding="utf-8")
    status, detail = security._check_settings_secrets()
    assert status == "fail" and "[polarion] local_token" in detail and "abc123" not in detail
