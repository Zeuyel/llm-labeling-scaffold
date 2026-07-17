from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

from llm_labeling_scaffold import panel
from llm_labeling_scaffold.auth import build_panel_authenticator


@contextmanager
def _panel_server(static_dir: Path):
    old = {
        "static_dir": panel._Handler.static_dir,
        "authenticator": panel._Handler.authenticator,
        "authorization_service": panel._Handler.authorization_service,
        "authorization_ready": panel._Handler.authorization_ready,
    }
    panel._Handler.static_dir = static_dir
    panel._Handler.authenticator = build_panel_authenticator(
        mode="basic_dev",
        basic_user="admin",
        basic_password="secret",
    )
    panel._Handler.authorization_service = None
    panel._Handler.authorization_ready = False
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), panel._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        for key, value in old.items():
            setattr(panel._Handler, key, value)


def test_frontend_is_public_but_api_requires_login(tmp_path: Path):
    static_dir = tmp_path / "frontend"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<title>登录</title>", encoding="utf-8")

    with _panel_server(static_dir) as base_url:
        with urllib.request.urlopen(base_url + "/", timeout=5) as response:
            assert response.status == 200
            assert "登录" in response.read().decode("utf-8")

        try:
            urllib.request.urlopen(base_url + "/api/session", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
            assert "WWW-Authenticate" not in exc.headers
            assert json.loads(exc.read().decode("utf-8"))["code"] == "missing_basic_credentials"
        else:
            raise AssertionError("未认证请求不应访问 /api/session")
