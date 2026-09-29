"""server routes for the 3D viewer: /models (scan), /model/<id> (gated GLB bytes),
POST /model/show (register + model3d SSE). Runs a real helios.server on a random port
with a stub brain — ported from Helios-main tests/test_model_routes.py."""
import json
import queue
import socket
import urllib.error
import urllib.request

import pytest

trimesh = pytest.importorskip("trimesh")

from helios import conf, models3d, permissions, server  # noqa: E402


class _StubBrain:
    def busy(self):
        return False

    def run_turn(self, *a, **k):
        pass

    def panic(self):
        pass

    def new_conversation(self):
        pass


class _Hub(server.Hub):
    """Real Hub + a capture queue so tests can assert published SSE events."""

    def __init__(self):
        super().__init__()
        self.captured = queue.Queue()

    def publish(self, kind, data):
        self.captured.put((kind, data))
        super().publish(kind, data)


@pytest.fixture
def live(tmp_path, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(conf, "PORT", port)
    monkeypatch.setattr(conf, "BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(conf, "AUTH_TOKEN_FILE", tmp_path / ".session_token")
    monkeypatch.setattr(conf, "_auth_token_cache", None)
    monkeypatch.setattr(models3d, "MODELS_ROOT", tmp_path / "work")
    monkeypatch.setattr(models3d, "_REG", {})
    hub = _Hub()
    httpd = server.start(_StubBrain(), permissions.PendingRegistry(hub.publish), hub)
    tok = conf.auth_token()
    yield {"port": port, "token": tok, "hub": hub, "tmp": tmp_path}
    httpd.shutdown()
    httpd.server_close()


def _http(port, method, path, token=None, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    if token:
        req.add_header("X-Auth-Token", token)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _make_glb(folder, name="model.glb"):
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    trimesh.creation.box(extents=(0.1, 0.1, 0.1)).export(str(p))
    return p


def test_models_scan_lists_glbs(live):
    _make_glb(live["tmp"] / "work" / "coaster")
    code, body, _ = _http(live["port"], "GET", "/models", token=live["token"])
    items = json.loads(body)["items"]
    assert code == 200 and len(items) == 1
    assert items[0]["name"] == "coaster"        # stem 'model' -> folder name
    assert items[0]["url"].startswith("/model/")


def test_models_requires_token(live):
    code, _, _ = _http(live["port"], "GET", "/models")
    assert code == 401


def test_model_bytes_roundtrip_and_404(live):
    p = _make_glb(live["tmp"] / "work" / "coaster")
    code, body, _ = _http(live["port"], "GET", "/models", token=live["token"])
    url = json.loads(body)["items"][0]["url"]
    code, blob, headers = _http(live["port"], "GET", url, token=live["token"])
    assert code == 200 and blob == p.read_bytes()
    assert headers.get("Content-Type") == "model/gltf-binary"
    assert headers.get("Cache-Control") == "no-store"
    code, _, _ = _http(live["port"], "GET", "/model/deadbeef00000000", token=live["token"])
    assert code == 404
    code, _, _ = _http(live["port"], "GET", url)          # no token
    assert code == 401


def test_model_show_registers_and_publishes(live):
    p = _make_glb(live["tmp"] / "work" / "stand")
    code, body, _ = _http(live["port"], "POST", "/model/show", token=live["token"],
                          body={"path": str(p), "verts": 8, "faces": 12,
                                "png": "data:image/png;base64,aGk="})
    entry = json.loads(body)
    assert code == 200 and entry["name"] == "stand"
    kind, data = live["hub"].captured.get(timeout=2)
    assert kind == "model3d"
    assert data["url"] == entry["url"] and data["verts"] == 8 and data["faces"] == 12
    assert data["png"].startswith("data:image/png;base64,")


def test_model_show_rejects_bad_path_and_bad_png(live):
    code, _, _ = _http(live["port"], "POST", "/model/show", token=live["token"],
                       body={"path": str(live["tmp"] / "nope.glb")})
    assert code == 400
    p = _make_glb(live["tmp"] / "work" / "x")
    code, body, _ = _http(live["port"], "POST", "/model/show", token=live["token"],
                          body={"path": str(p), "png": "javascript:alert(1)"})
    assert code == 200
    kind, data = live["hub"].captured.get(timeout=2)
    assert data["png"] == ""                     # non-data-URI png is stripped
