"""Security properties that must never regress."""
import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import server
from conftest import KEYS
from fakes import FakeApps


def serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def call(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


@pytest.fixture
def quiet_status(monkeypatch):
    monkeypatch.setattr(server, "status_cache", {"data": {
        "vpn": {"connected": True, "country": "Canada"}, "downloads": None, "series": 1, "movies": 0}})


def test_network_port_serves_widgets_only(secrets_file, quiet_status):
    httpd, url = serve(server.WidgetHandler)
    try:
        assert call(f"{url}/widgets/stats")[0] == 200
        assert call(f"{url}/widgets/downloads")[0] == 200
        for path in ("/api/status", "/api/logs?service=sonarr", "/api/updates", "/", "/api/backups/download?name=x.tar.gz"):
            assert call(f"{url}{path}")[0] == 404, path
        for path in ("/api/vpn", "/api/restart", "/api/password", "/api/backups/restore"):
            assert call(f"{url}{path}", "POST", {}, {"X-Yams-Request": "1"})[0] == 404, path
    finally:
        httpd.shutdown()


def test_widgets_never_include_the_password(secrets_file, quiet_status):
    httpd, url = serve(server.WidgetHandler)
    try:
        for path in ("/widgets/stats", "/widgets/downloads"):
            assert "not-a-real-password" not in json.dumps(call(f"{url}{path}")[1])
    finally:
        httpd.shutdown()


@pytest.mark.parametrize("headers", [
    {},                                                     # no dashboard header
    {"X-Yams-Request": "1", "Sec-Fetch-Site": "cross-site"},  # another website
    {"X-Yams-Request": "1", "Sec-Fetch-Site": "same-site"},   # another app on the same host
])
def test_commands_must_come_from_the_dashboard(secrets_file, headers):
    httpd, url = serve(server.Handler)
    try:
        for path in ("/api/vpn", "/api/restart", "/api/password", "/api/fix", "/api/updates/apply", "/api/backups/restore"):
            status, body = call(f"{url}{path}", "POST", {"config": "x"}, {"Content-Type": "application/json", **headers})
            assert status == 403, path
    finally:
        httpd.shutdown()


# --------------------------------------------------------------------------- password change
class FakeJellyfin(BaseHTTPRequestHandler):
    password = None

    def log_message(self, *a):
        pass

    def reply(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/Users/AuthenticateByName":
            if body.get("Pw") == FakeJellyfin.password:
                return self.reply(200, {"AccessToken": "t", "User": {"Id": "u1"}})
            return self.reply(401, {})
        if self.path == "/Users/u1/Password" and body.get("CurrentPw") == FakeJellyfin.password:
            FakeJellyfin.password = body["NewPw"]
            return self.reply(204)
        return self.reply(403, {})


@pytest.fixture
def rotation(secrets_file, monkeypatch):
    apps = FakeApps(KEYS).start()
    monkeypatch.setitem(server.ARR["sonarr"], "base", apps.url("sonarr"))
    monkeypatch.setitem(server.ARR["radarr"], "base", apps.url("radarr"))
    FakeJellyfin.password = "not-a-real-password"
    jelly, jelly_url = serve(FakeJellyfin)
    monkeypatch.setattr(server, "JELLYFIN", jelly_url)
    conf = os.path.join(tempfile.mkdtemp(), "qBittorrent.conf")
    with open(conf, "w") as fh:
        fh.write('[Preferences]\nWebUI\\Port=8080\nWebUI\\Password_PBKDF2="@ByteArray(old:old)"\n')
    monkeypatch.setattr(server, "QBIT_CONF", conf)
    monkeypatch.setattr(server, "project_containers", lambda: {})   # qBittorrent not running
    for kind in ("sonarr", "radarr"):
        server.ensure_download_client(kind)
    yield apps, conf
    apps.stop()
    jelly.shutdown()


def test_change_password_updates_every_app(rotation):
    apps, conf = rotation
    good, message = server.change_password()
    new = server.secrets()["password"]
    assert good, message
    assert new != "not-a-real-password" and re.fullmatch(r"[a-z]+(-[a-z]+){3}-\d{2}", new)
    assert FakeJellyfin.password == new
    assert "@ByteArray(old:old)" not in open(conf).read()
    for kind in ("sonarr", "radarr"):
        fields = {f["name"]: f.get("value") for f in apps.clients[kind][0]["fields"]}
        assert fields["password"] == new
    assert oct(os.stat(os.path.join(os.environ["STATE_DIR"], "secrets.json")).st_mode)[-3:] == "600"


def test_password_change_reports_what_it_could_not_do(rotation):
    FakeJellyfin.password = "changed-by-hand"
    good, message = server.change_password()
    assert not good and "Jellyfin" in message and "qBittorrent" in message
