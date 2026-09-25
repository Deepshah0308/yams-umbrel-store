"""
YAMS for Umbrel - dashboard + auto-configurator.

Pure standard-library Python so it runs on the stock python:alpine image.
  * Serves the setup dashboard (index.html) and a small JSON API.
  * In the background, wires every app together through their APIs:
      Sonarr/Radarr -> qBittorrent + library folders + file renaming
      Prowlarr      -> Sonarr, Radarr, FlareSolverr
      Jellyfin      -> first-run wizard, admin user, Movies & Shows libraries
  * Accepts a pasted WireGuard file and turns it into something gluetun accepts.
  * Talks to Docker (only this app's containers) for status, logs and restarts.
  * Serves the Umbrel home-screen widgets.
  * Backs up and restores every app's settings (see backups.py).
"""
import configparser
import http.client as httpclient
import ipaddress
import json
import os
import re
import socket
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import backups

PREFIX = os.environ.get("APP_PREFIX", "yams-media")
JELLYFIN_PORT = os.environ.get("JELLYFIN_PORT", "8097")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("STATE_DIR", "/yams/state")
WG_PATH = os.environ.get("WG_PATH", "/yams/vpn/wireguard/wg0.conf")
YAMS_VERSION = os.environ.get("YAMS_VERSION", "")
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
SECRETS_PATH = f"{STATE_DIR}/secrets.json"
SETUP_PATH = f"{STATE_DIR}/setup.json"


def host(service):
    return f"{PREFIX}_{service}_1"


SONARR = f"http://{host('sonarr')}:8989/sonarr"
RADARR = f"http://{host('radarr')}:7878/radarr"
PROWLARR = f"http://{host('prowlarr')}:9696/prowlarr"
BAZARR = f"http://{host('bazarr')}:6767/bazarr"
JELLYFIN = f"http://{host('jellyfin')}:8096"
QBIT_HOST = host("gluetun")  # qBittorrent shares gluetun's network
GLUETUN = f"http://{host('gluetun')}:8000"
FLARESOLVERR = f"http://{host('flaresolverr')}:8191/"
JELLYFIN_AUTH = 'MediaBrowser Client="YAMS", Device="YAMS dashboard", DeviceId="yams-dashboard", Version="1.0"'


# --------------------------------------------------------------------------- helpers
def read_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def secrets():
    return read_json(SECRETS_PATH, {})


def http(method, url, body=None, headers=None, timeout=8):
    """Returns (status, parsed_body). status is 0 when the server can't be reached."""
    data = None
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as err:
        raw = err.read()
        status = err.code
    except Exception:
        return 0, None
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode(errors="replace")


def ok(status):
    return 200 <= status < 300


# --------------------------------------------------------------------------- setup log
setup_lock = threading.Lock()


def setup_state():
    return read_json(SETUP_PATH, {"done": {}, "message": "Getting everything ready…"})


def mark(task, message=None):
    with setup_lock:
        state = setup_state()
        state["done"][task] = True
        if message:
            state["message"] = message
        write_json(SETUP_PATH, state)


def say(message):
    with setup_lock:
        state = setup_state()
        state["message"] = message
        write_json(SETUP_PATH, state)
    print(f"[yams] {message}", flush=True)


def is_done(task):
    return setup_state()["done"].get(task, False)


def wait_for(url, headers=None, label="app", timeout=600):
    start = time.time()
    while time.time() - start < timeout:
        status, _ = http("GET", url, headers=headers, timeout=5)
        if ok(status):
            return True
        time.sleep(5)
    say(f"{label} is taking a while to start. YAMS will keep trying.")
    return False


# --------------------------------------------------------------------------- Sonarr / Radarr
ARR = {
    "sonarr": {"title": "Sonarr", "base": SONARR, "key": "sonarr_key", "root": "/data/media/tv",
               "category_field": "tvCategory", "category": "tv", "rename_field": "renameEpisodes"},
    "radarr": {"title": "Radarr", "base": RADARR, "key": "radarr_key", "root": "/data/media/movies",
               "category_field": "movieCategory", "category": "movies", "rename_field": "renameMovies"},
}
CLIENT_NAME = "qBittorrent (VPN)"
# kind -> {"state": "connected" | "unreachable" | "missing" | "unknown", "at": time}
client_status = {"sonarr": {"state": "unknown", "at": 0}, "radarr": {"state": "unknown", "at": 0}}


def arr_api(kind):
    cfg = ARR[kind]
    return f"{cfg['base']}/api/v3", {"X-Api-Key": secrets().get(cfg["key"], "")}


def ensure_root_folder(kind):
    api, h = arr_api(kind)
    root = ARR[kind]["root"]
    status, folders = http("GET", f"{api}/rootfolder", headers=h)
    if not ok(status):
        return False
    if any(f.get("path", "").rstrip("/") == root for f in folders or []):
        return True
    status, _ = http("POST", f"{api}/rootfolder", {"path": root}, headers=h)
    return ok(status)


def _client_values(kind):
    s = secrets()
    cfg = ARR[kind]
    return {
        "host": QBIT_HOST,
        "port": 8080,
        "useSsl": False,
        "urlBase": "",
        "username": s.get("username", ""),
        "password": s.get("password", ""),
        cfg["category_field"]: cfg["category"],
    }


def _apply_values(fields, values):
    for field in fields:
        if field.get("name") in values:
            field["value"] = values[field["name"]]
    have = {f.get("name") for f in fields}
    for name, value in values.items():
        if name not in have:
            fields.append({"name": name, "value": value})
    return fields


def ensure_download_client(kind):
    """Make sure Sonarr/Radarr has a working qBittorrent entry. Adds it, or repairs one
    that points somewhere else or was switched off. Returns True when it's in place."""
    api, h = arr_api(kind)
    values = _client_values(kind)
    status, clients = http("GET", f"{api}/downloadclient", headers=h)
    if not ok(status) or not isinstance(clients, list):
        return False
    existing = [c for c in clients if c.get("implementation") == "QBittorrent"]
    if existing:
        client = existing[0]
        current = {f.get("name"): f.get("value") for f in client.get("fields", [])}
        wrong = (current.get("host") != QBIT_HOST or str(current.get("port")) != "8080"
                 or current.get("username") != values["username"] or not client.get("enable"))
        if not wrong:
            return True
        client["enable"] = True
        _apply_values(client.setdefault("fields", []), values)
        status, _ = http("PUT", f"{api}/downloadclient/{client['id']}?forceSave=true", client, headers=h)
        if ok(status):
            print(f"[yams] Repaired the qBittorrent connection in {ARR[kind]['title']}", flush=True)
        return ok(status)

    # Start from the app's own blank qBittorrent form so every field it expects is present.
    status, schema = http("GET", f"{api}/downloadclient/schema", headers=h)
    template = None
    if ok(status) and isinstance(schema, list):
        template = next((x for x in schema if x.get("implementation") == "QBittorrent"), None)
    if template:
        body = {k: v for k, v in template.items() if k not in ("id", "presets")}
        body["fields"] = _apply_values(body.get("fields", []), values)
    else:
        body = {"implementation": "QBittorrent", "configContract": "QBittorrentSettings",
                "protocol": "torrent", "fields": _apply_values([], values)}
    body.update(name=CLIENT_NAME, enable=True, priority=1, tags=[],
                removeCompletedDownloads=True, removeFailedDownloads=True)
    # forceSave: qBittorrent may still be waiting for the VPN; the entry is saved anyway.
    status, result = http("POST", f"{api}/downloadclient?forceSave=true", body, headers=h)
    if ok(status):
        print(f"[yams] Added qBittorrent to {ARR[kind]['title']}", flush=True)
        return True
    print(f"[yams] {ARR[kind]['title']} didn't accept the qBittorrent entry ({status}): {str(result)[:300]}", flush=True)
    return False


def test_download_client(kind):
    """Ask Sonarr/Radarr to test its qBittorrent entry, the same as its Test button."""
    api, h = arr_api(kind)
    status, clients = http("GET", f"{api}/downloadclient", headers=h)
    client = next((c for c in clients or [] if c.get("implementation") == "QBittorrent"), None) if ok(status) else None
    if not client:
        state = "missing"
    else:
        status, _ = http("POST", f"{api}/downloadclient/test", client, headers=h, timeout=20)
        state = "connected" if ok(status) else "unreachable"
    client_status[kind] = {"state": state, "at": time.time()}
    return state


def wire_arr(kind):
    cfg = ARR[kind]
    api, h = arr_api(kind)
    if is_done(kind):
        return True
    say(f"Waiting for {cfg['title']} to start\u2026")
    if not wait_for(f"{api}/system/status", h, cfg["title"]):
        return False
    say(f"Connecting {cfg['title']} to your library and qBittorrent\u2026")
    if not ensure_root_folder(kind) or not ensure_download_client(kind):
        say(f"{cfg['title']} isn't fully connected yet. YAMS will try again shortly.")
        return False

    # Tidy file names (the default is off)
    status, naming = http("GET", f"{api}/config/naming", headers=h)
    if ok(status) and isinstance(naming, dict) and not naming.get(cfg["rename_field"]):
        naming[cfg["rename_field"]] = True
        http("PUT", f"{api}/config/naming/{naming.get('id', 1)}", naming, headers=h)

    mark(kind, f"{cfg['title']} is connected.")
    return True


def connection_watch():
    """Every 10 minutes: re-check that Sonarr and Radarr still reach qBittorrent, and
    repair their settings if needed (this also fixes installs made by older versions)."""
    time.sleep(30)
    while True:
        for kind in ARR:
            try:
                api, h = arr_api(kind)
                status, _ = http("GET", f"{api}/system/status", headers=h, timeout=5)
                if not ok(status):
                    continue
                ensure_root_folder(kind)
                ensure_download_client(kind)
                vpn_up = (status_cache["data"] or {}).get("answering", {}).get("qbittorrent")
                if vpn_up:
                    test_download_client(kind)
                else:
                    client_status[kind] = {"state": "waiting", "at": time.time()}
            except Exception:
                traceback.print_exc()
        time.sleep(600)


# --------------------------------------------------------------------------- Prowlarr
def wire_prowlarr():
    if is_done("prowlarr"):
        return True
    s = secrets()
    api = f"{PROWLARR}/api/v1"
    h = {"X-Api-Key": s["prowlarr_key"]}
    say("Waiting for Prowlarr to start…")
    if not wait_for(f"{api}/system/status", h, "Prowlarr"):
        return False
    say("Connecting Prowlarr to Sonarr, Radarr and FlareSolverr…")

    # FlareSolverr, used by any indexer tagged "flaresolverr"
    status, tags = http("GET", f"{api}/tag", headers=h)
    tag_id = None
    if ok(status):
        for tag in tags or []:
            if tag.get("label") == "flaresolverr":
                tag_id = tag["id"]
        if tag_id is None:
            status, created = http("POST", f"{api}/tag", {"label": "flaresolverr"}, headers=h)
            if ok(status):
                tag_id = created["id"]
    status, proxies = http("GET", f"{api}/indexerProxy", headers=h)
    if ok(status) and tag_id is not None and not any(
        p.get("implementation") == "FlareSolverr" for p in proxies or []
    ):
        http(
            "POST",
            f"{api}/indexerProxy?forceSave=true",
            {
                "name": "FlareSolverr",
                "implementation": "FlareSolverr",
                "configContract": "FlareSolverrSettings",
                "tags": [tag_id],
                "fields": [
                    {"name": "host", "value": FLARESOLVERR},
                    {"name": "requestTimeout", "value": 60},
                ],
            },
            headers=h,
        )

    # Sync indexers to Sonarr and Radarr
    status, apps = http("GET", f"{api}/applications", headers=h)
    if ok(status):
        existing = {a.get("implementation") for a in apps or []}
        targets = [
            ("Sonarr", SONARR, s["sonarr_key"],
             [5000, 5010, 5020, 5030, 5040, 5045, 5050, 5090], [5070]),
            ("Radarr", RADARR, s["radarr_key"],
             [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090], None),
        ]
        for name, url, key, cats, anime in targets:
            if name in existing:
                continue
            fields = [
                {"name": "prowlarrUrl", "value": PROWLARR},
                {"name": "baseUrl", "value": url},
                {"name": "apiKey", "value": key},
                {"name": "syncCategories", "value": cats},
            ]
            if anime:
                fields.append({"name": "animeSyncCategories", "value": anime})
            http(
                "POST",
                f"{api}/applications?forceSave=true",
                {
                    "name": name,
                    "implementation": name,
                    "configContract": f"{name}Settings",
                    "syncLevel": "fullSync",
                    "tags": [],
                    "fields": fields,
                },
                headers=h,
            )
    mark("prowlarr", "Prowlarr is connected.")
    return True


# --------------------------------------------------------------------------- Jellyfin
def jellyfin_token(s):
    status, auth = http(
        "POST",
        f"{JELLYFIN}/Users/AuthenticateByName",
        {"Username": s["username"], "Pw": s["password"]},
        headers={"Authorization": JELLYFIN_AUTH},
    )
    if ok(status) and isinstance(auth, dict):
        return auth.get("AccessToken")
    return None


def wire_jellyfin():
    if is_done("jellyfin"):
        return True
    s = secrets()
    say("Waiting for Jellyfin to start…")
    if not wait_for(f"{JELLYFIN}/System/Info/Public", label="Jellyfin"):
        return False
    _, info = http("GET", f"{JELLYFIN}/System/Info/Public")
    if isinstance(info, dict) and not info.get("StartupWizardCompleted"):
        say("Setting up Jellyfin…")
        http("POST", f"{JELLYFIN}/Startup/Configuration", {
            "UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en",
        })
        http("GET", f"{JELLYFIN}/Startup/User")
        http("POST", f"{JELLYFIN}/Startup/User", {"Name": s["username"], "Password": s["password"]})
        http("POST", f"{JELLYFIN}/Startup/RemoteAccess", {
            "EnableRemoteAccess": True, "EnableAutomaticPortMapping": False,
        })
        http("POST", f"{JELLYFIN}/Startup/Complete")
        time.sleep(3)

    token = jellyfin_token(s)
    if not token:
        say("Jellyfin was already set up by hand, so YAMS left it alone.")
        mark("jellyfin")
        return True
    h = {"Authorization": f'{JELLYFIN_AUTH}, Token="{token}"'}
    status, folders = http("GET", f"{JELLYFIN}/Library/VirtualFolders", headers=h)
    names = {f.get("Name") for f in folders or []} if ok(status) else set()
    for name, ctype, path in (("Movies", "movies", "/data/media/movies"),
                              ("Shows", "tvshows", "/data/media/tv")):
        if name in names:
            continue
        query = urllib.parse.urlencode({"name": name, "collectionType": ctype, "refreshLibrary": "true"})
        http("POST", f"{JELLYFIN}/Library/VirtualFolders?{query}", {
            "LibraryOptions": {
                "EnableRealtimeMonitor": True,
                "PathInfos": [{"Path": path}],
            }
        }, headers=h, timeout=30)
    mark("jellyfin", "Jellyfin is ready with Movies and Shows libraries.")
    return True


def configurator():
    tasks = [("sonarr", lambda: wire_arr("sonarr")),
             ("radarr", lambda: wire_arr("radarr")),
             ("prowlarr", wire_prowlarr),
             ("jellyfin", wire_jellyfin)]
    while True:
        pending = False
        for name, task in tasks:
            try:
                if not task():
                    pending = True
            except Exception:
                pending = True
                traceback.print_exc()
        if not pending:
            say("All set. Everything is connected.")
            return
        time.sleep(20)


# --------------------------------------------------------------------------- WireGuard
class VpnError(ValueError):
    pass


def clean_wireguard(text):
    """Turn a provider's WireGuard file into the IPv4-only, IP-endpoint form gluetun needs."""
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(text.strip() + "\n")
    except configparser.Error:
        raise VpnError("That doesn't look like a WireGuard file. It should contain [Interface] and [Peer] sections.")
    if not parser.has_section("Interface") or not parser.has_section("Peer"):
        raise VpnError("The file needs both an [Interface] and a [Peer] section.")
    iface, peer = parser["Interface"], parser["Peer"]

    private_key = iface.get("PrivateKey", "").strip()
    public_key = peer.get("PublicKey", "").strip()
    endpoint = peer.get("Endpoint", "").strip()
    if not private_key:
        raise VpnError("PrivateKey is missing under [Interface].")
    if not public_key:
        raise VpnError("PublicKey is missing under [Peer].")
    if not endpoint or ":" not in endpoint:
        raise VpnError("Endpoint under [Peer] should look like server.example.com:51820.")

    addresses = []
    for part in iface.get("Address", "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if ipaddress.ip_interface(part).version == 4:
                addresses.append(part)
        except ValueError:
            raise VpnError(f"Couldn't read the address {part!r}.")
    if not addresses:
        raise VpnError("The file needs an IPv4 Address under [Interface].")

    ep_host, _, ep_port = endpoint.rpartition(":")
    ep_host = ep_host.strip("[]")
    if not ep_port.isdigit():
        raise VpnError("The Endpoint port should be a number.")
    try:
        ipaddress.ip_address(ep_host)
        ep_ip = ep_host
    except ValueError:
        try:
            ep_ip = socket.getaddrinfo(ep_host, None, socket.AF_INET)[0][4][0]
        except socket.gaierror:
            raise VpnError(f"Couldn't look up the VPN server {ep_host}. Check your internet connection or pick another server.")
    if ipaddress.ip_address(ep_ip).version != 4:
        raise VpnError("Please use a VPN server with an IPv4 address.")

    lines = ["[Interface]", f"PrivateKey = {private_key}", f"Address = {', '.join(addresses)}", "",
             "[Peer]", f"PublicKey = {public_key}"]
    psk = peer.get("PresharedKey", "").strip()
    if psk:
        lines.append(f"PresharedKey = {psk}")
    lines += [f"Endpoint = {ep_ip}:{ep_port}", "AllowedIPs = 0.0.0.0/0", ""]
    return "\n".join(lines), ep_host


# --------------------------------------------------------------------------- Docker
# Only this app's containers are ever touched: every call is scoped to the
# compose project label, and restarts are limited to the list below.
SERVICES = [
    # (service, display name, what it does, open link)
    ("jellyfin", "Jellyfin", "Watch everything, on any screen", "jellyfin"),
    ("sonarr", "Sonarr", "TV shows: follows series and grabs new episodes", "/sonarr/"),
    ("radarr", "Radarr", "Movies: finds and organises films", "/radarr/"),
    ("prowlarr", "Prowlarr", "Indexers: where YAMS searches", "/prowlarr/"),
    ("bazarr", "Bazarr", "Subtitles. Pick your languages once in Settings \u2192 Languages", "/bazarr/"),
    ("qbittorrent", "qBittorrent", "Downloads, only ever through your VPN", "/qbittorrent/"),
    ("gluetun", "VPN", "gluetun: the tunnel qBittorrent downloads through", None),
    ("flaresolverr", "FlareSolverr", "Helps Prowlarr past Cloudflare checks", None),
]
LOG_SERVICES = [s[0] for s in SERVICES] + ["dashboard", "web", "init"]
LOG_LABELS = {s[0]: s[1] for s in SERVICES}
LOG_LABELS.update({"dashboard": "YAMS dashboard", "web": "Router (Caddy)", "init": "First-boot setup"})
RESTARTABLE = {s[0] for s in SERVICES}


class _DockerConnection(httpclient.HTTPConnection):
    def __init__(self, timeout):
        super().__init__("localhost", timeout=timeout)

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(DOCKER_SOCK)
        self.sock = sock


def docker(method, path, timeout=15):
    conn = _DockerConnection(timeout)
    try:
        conn.request(method, path, headers={"Host": "docker"})
        resp = conn.getresponse()
        return resp.status, resp.read()
    except (OSError, httpclient.HTTPException):
        return 0, b""
    finally:
        conn.close()


def docker_available():
    return os.path.exists(DOCKER_SOCK)


def project_containers():
    """{service: {id, state, status}} for this app's containers, or None if Docker is unreachable."""
    filters = urllib.parse.quote(json.dumps({"label": [f"com.docker.compose.project={PREFIX}"]}))
    status, body = docker("GET", f"/containers/json?all=1&filters={filters}")
    if status != 200:
        return None
    result = {}
    for c in json.loads(body):
        service = (c.get("Labels") or {}).get("com.docker.compose.service")
        if service:
            result[service] = {"id": c["Id"], "state": c.get("State", ""), "status": c.get("Status", "")}
    return result


def inspect(container_id):
    status, body = docker("GET", f"/containers/{container_id}/json")
    return json.loads(body) if status == 200 else None


def demux(raw):
    """Docker multiplexes stdout/stderr into 8-byte-header frames when there's no TTY."""
    if len(raw) >= 8 and raw[0] in (0, 1, 2) and raw[1:4] == b"\x00\x00\x00":
        out, i = [], 0
        while i + 8 <= len(raw):
            size = int.from_bytes(raw[i + 4:i + 8], "big")
            out.append(raw[i + 8:i + 8 + size])
            i += 8 + size
        raw = b"".join(out)
    return raw.decode("utf-8", errors="replace")


def container_logs(service, tail=400):
    containers = project_containers()
    if containers is None:
        return None, "YAMS can't reach Docker right now."
    c = containers.get(service)
    if not c:
        return None, "That app's container doesn't exist yet."
    status, raw = docker("GET", f"/containers/{c['id']}/logs?stdout=1&stderr=1&timestamps=1&tail={int(tail)}", timeout=20)
    if status != 200:
        return None, "Docker didn't return logs for that app."
    return demux(raw), None


def last_log_line(container_id):
    status, raw = docker("GET", f"/containers/{container_id}/logs?stdout=1&stderr=1&tail=3", timeout=5)
    if status != 200:
        return None
    lines = [ln.strip() for ln in demux(raw).splitlines() if ln.strip()]
    return lines[-1][:220] if lines else None


busy = {}  # service -> message while an action runs
busy_lock = threading.Lock()


def set_busy(service, message):
    with busy_lock:
        if message:
            busy[service] = message
        else:
            busy.pop(service, None)


def restart_service(service):
    containers = project_containers() or {}
    c = containers.get(service)
    if not c:
        return
    set_busy(service, "Restarting\u2026")
    try:
        docker("POST", f"/containers/{c['id']}/restart?t=15", timeout=60)
        # qBittorrent lives inside gluetun's network: it must be restarted after it.
        if service == "gluetun" and "qbittorrent" in containers:
            set_busy("qbittorrent", "Restarting with the VPN\u2026")
            time.sleep(3)
            docker("POST", f"/containers/{containers['qbittorrent']['id']}/restart?t=15", timeout=60)
            set_busy("qbittorrent", None)
    finally:
        set_busy(service, None)


def restart_all():
    set_busy("_all", "Restarting all apps\u2026")
    try:
        restart_service("gluetun")  # also restarts qBittorrent
        for service in ("flaresolverr", "prowlarr", "sonarr", "radarr", "bazarr", "jellyfin"):
            restart_service(service)
    finally:
        set_busy("_all", None)


# Order matters: qBittorrent stops before the VPN and starts after it.
PAUSE_ORDER = ["qbittorrent", "sonarr", "radarr", "prowlarr", "bazarr", "jellyfin"]


def pause_apps(reason, include_vpn=False):
    """Stop the apps (for a clean backup or restore). Returns the ones that were running."""
    containers = project_containers() or {}
    order = PAUSE_ORDER + (["gluetun"] if include_vpn else [])
    stopped = []
    for service in order:
        c = containers.get(service)
        if c and c["state"] in ("running", "restarting"):
            set_busy(service, reason)
            docker("POST", f"/containers/{c['id']}/stop?t=30", timeout=90)
            stopped.append(service)
    return stopped


def resume_apps(services):
    containers = project_containers() or {}
    order = ["gluetun", "jellyfin", "prowlarr", "sonarr", "radarr", "bazarr", "qbittorrent"]
    for service in [s for s in order if s in services]:
        c = containers.get(service)
        if c:
            docker("POST", f"/containers/{c['id']}/start", timeout=60)
            if service == "gluetun":
                time.sleep(3)
        set_busy(service, None)


def after_restore():
    # Logins may have changed with the restored settings.
    qb_state.update(sid=None, blocked_until=0)
    updates_cache.update(data=None, at=0)


# --------------------------------------------------------------------------- qBittorrent API
qb_state = {"sid": None, "blocked_until": 0}
qb_lock = threading.Lock()
QBIT = f"http://{QBIT_HOST}:8080"


def qb_login():
    # qBittorrent bans an address after repeated failed logins, so back off hard on failure.
    if time.time() < qb_state["blocked_until"]:
        return False
    s = secrets()
    data = urllib.parse.urlencode({"username": s.get("username", ""), "password": s.get("password", "")}).encode()
    req = urllib.request.Request(f"{QBIT}/api/v2/auth/login", data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode(errors="replace")
            cookie = resp.headers.get("Set-Cookie", "")
    except Exception:
        return False
    match = re.search(r"SID=([^;]+)", cookie)
    if body.strip().lower().startswith("ok") and match:
        qb_state["sid"] = match.group(1)
        return True
    qb_state["blocked_until"] = time.time() + 600
    return False


def qb_get(path):
    with qb_lock:
        for _ in range(2):
            if not qb_state["sid"] and not qb_login():
                return None
            req = urllib.request.Request(f"{QBIT}{path}", headers={"Cookie": f"SID={qb_state['sid']}"})
            try:
                with urllib.request.urlopen(req, timeout=6) as resp:
                    return json.loads(resp.read() or b"null")
            except urllib.error.HTTPError as err:
                if err.code == 403:
                    qb_state["sid"] = None
                    continue
                return None
            except Exception:
                return None
    return None


DOWNLOADING = {"downloading", "metaDL", "forcedDL", "stalledDL", "queuedDL", "checkingDL", "allocating", "forcedMetaDL"}


def human_speed(bps):
    if not bps:
        return "0 KB/s"
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024 or unit == "GB/s":
            return f"{bps:.0f} {unit}" if unit in ("B/s", "KB/s") else f"{bps:.1f} {unit}"
        bps /= 1024


def human_eta(seconds):
    if not seconds or seconds >= 8640000:
        return ""
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min left"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min left"
    return f"{seconds // 86400} days left"


def downloads():
    torrents = qb_get("/api/v2/torrents/info?sort=added_on&reverse=true")
    if not isinstance(torrents, list):
        return None
    active = [t for t in torrents if t.get("state") in DOWNLOADING]
    return {
        "downloading": len(active),
        "seeding": sum(1 for t in torrents if t.get("state") in ("uploading", "stalledUP", "forcedUP", "queuedUP")),
        "speed": sum(t.get("dlspeed", 0) for t in active),
        "items": [{
            "name": t.get("name", "")[:120],
            "progress": round(float(t.get("progress", 0)), 4),
            "speed": human_speed(t.get("dlspeed", 0)),
            "eta": human_eta(t.get("eta", 0)),
            "state": t.get("state"),
        } for t in active[:8]],
    }


# --------------------------------------------------------------------------- VPN check
def check_vpn():
    """Like `yams check-vpn`: compare your home address with the one downloads use."""
    s = secrets()
    result = {"home_ip": None, "vpn_ip": None, "country": None, "qbit_ip": None,
              "qbit_in_vpn": None, "protected": False, "checked_at": int(time.time())}
    try:
        req = urllib.request.Request("https://api.ipify.org?format=json", headers={"User-Agent": "yams-umbrel"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            result["home_ip"] = json.loads(resp.read()).get("ip")
    except Exception:
        pass
    status, ipinfo = http("GET", f"{GLUETUN}/v1/publicip/ip",
                          headers={"X-API-Key": s.get("gluetun_key", "")}, timeout=5)
    if ok(status) and isinstance(ipinfo, dict):
        result["vpn_ip"] = ipinfo.get("public_ip") or None
        result["country"] = ipinfo.get("country")
    containers = project_containers() or {}
    if "qbittorrent" in containers and "gluetun" in containers:
        info = inspect(containers["qbittorrent"]["id"]) or {}
        mode = (info.get("HostConfig") or {}).get("NetworkMode", "")
        result["qbit_in_vpn"] = mode in (f"container:{containers['gluetun']['id']}",) or mode.endswith("gluetun_1")
    transfer = qb_get("/api/v2/transfer/info")
    if isinstance(transfer, dict):
        result["qbit_ip"] = transfer.get("last_external_address_v4") or None
    result["protected"] = bool(
        result["vpn_ip"] and result["qbit_in_vpn"]
        and (not result["home_ip"] or result["home_ip"] != result["vpn_ip"])
        and (not result["qbit_ip"] or result["qbit_ip"] != result["home_ip"])
    )
    return result


# --------------------------------------------------------------------------- updates
# Where each app's newest release is published.
UPSTREAM = {
    "jellyfin": ("jellyfin/jellyfin", "releases"),
    "sonarr": ("Sonarr/Sonarr", "releases"),
    "radarr": ("Radarr/Radarr", "releases"),
    "prowlarr": ("Prowlarr/Prowlarr", "releases"),
    "bazarr": ("morpheus65535/bazarr", "releases"),
    "qbittorrent": ("qbittorrent/qBittorrent", "tags"),
    "gluetun": ("qdm12/gluetun", "releases"),
    "flaresolverr": ("FlareSolverr/FlareSolverr", "releases"),
}
updates_cache = {"data": None, "at": 0}
updates_lock = threading.Lock()


def version_tuple(text):
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(x or 0) for x in match.groups()) if match else None


def github_json(path):
    req = urllib.request.Request(f"https://api.github.com{path}",
                                 headers={"User-Agent": "yams-umbrel", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def latest_version(repo, kind):
    if kind == "releases":
        return github_json(f"/repos/{repo}/releases/latest").get("tag_name")
    best = None
    for tag in github_json(f"/repos/{repo}/tags?per_page=40"):
        name = tag.get("name", "")
        if not re.fullmatch(r"release-\d+\.\d+\.\d+", name):
            continue
        if best is None or version_tuple(name) > version_tuple(best):
            best = name
    return best


def check_updates(force=False):
    with updates_lock:
        if not force and updates_cache["data"] and time.time() - updates_cache["at"] < 6 * 3600:
            return updates_cache["data"]
        containers = project_containers() or {}
        rows = []
        for service, name, _, _ in SERVICES:
            row = {"service": service, "name": name, "installed": None, "latest": None, "status": "unknown"}
            c = containers.get(service)
            if c:
                image = ((inspect(c["id"]) or {}).get("Config") or {}).get("Image", "")
                tag = image.split("@")[0].rsplit(":", 1)[-1] if ":" in image.split("@")[0] else ""
                row["installed"] = ".".join(map(str, version_tuple(tag))) if version_tuple(tag) else tag or None
            try:
                latest = latest_version(*UPSTREAM[service])
                row["latest"] = ".".join(map(str, version_tuple(latest))) if version_tuple(latest) else latest
            except Exception:
                row["status"] = "unreachable"
            inst, late = version_tuple(row["installed"]), version_tuple(row["latest"])
            if inst and late:
                row["status"] = "update" if late > inst else "current"
            rows.append(row)
        data = {"checked_at": int(time.time()), "yams_version": YAMS_VERSION, "apps": rows}
        updates_cache.update(data=data, at=time.time())
        return data


# --------------------------------------------------------------------------- status
status_cache = {"data": None}


def service_up(url, headers=None):
    status, _ = http("GET", url, headers=headers, timeout=4)
    return status != 0 and status < 500


def count(url, headers):
    status, items = http("GET", url, headers=headers, timeout=8)
    return len(items) if ok(status) and isinstance(items, list) else None


def describe(service, container, answering):
    """Turn Docker's state plus an HTTP check into one plain-English status."""
    if busy.get(service):
        return "busy", busy[service]
    if not container:
        return "missing", "Not created"
    state, status = container["state"], container["status"]
    if state == "running":
        if answering:
            return "up", "Running"
        if service == "qbittorrent":
            return "wait", "Waiting for the VPN"
        return "wait", "Starting"
    if state == "restarting":
        match = re.search(r"\((\d+)\)", status)
        return "down", "Keeps restarting" if match and int(match.group(1)) > 0 else "Restarting"
    if state == "exited":
        return "down", "Stopped"
    return "wait", state.capitalize() or "Starting"


def gather_status():
    s = secrets()
    vpn_meta = read_json(f"{STATE_DIR}/vpn.json", {})
    vpn = {"configured": os.path.exists(WG_PATH) and os.path.getsize(WG_PATH) > 0,
           "connected": False, "ip": None, "country": None,
           "server": vpn_meta.get("server"), "saved_at": vpn_meta.get("saved_at")}
    if vpn["configured"]:
        status, ipinfo = http("GET", f"{GLUETUN}/v1/publicip/ip",
                              headers={"X-API-Key": s.get("gluetun_key", "")}, timeout=4)
        if ok(status) and isinstance(ipinfo, dict) and ipinfo.get("public_ip"):
            vpn.update(connected=True, ip=ipinfo.get("public_ip"), country=ipinfo.get("country"))

    sh = {"X-Api-Key": s.get("sonarr_key", "")}
    rh = {"X-Api-Key": s.get("radarr_key", "")}
    ph = {"X-Api-Key": s.get("prowlarr_key", "")}
    answering = {
        "jellyfin": service_up(f"{JELLYFIN}/health"),
        "sonarr": service_up(f"{SONARR}/ping"),
        "radarr": service_up(f"{RADARR}/ping"),
        "prowlarr": service_up(f"{PROWLARR}/ping"),
        "bazarr": service_up(f"{BAZARR}/"),
        "qbittorrent": vpn["connected"] and service_up(f"{QBIT}/api/v2/app/version"),
        "gluetun": vpn["connected"],
        "flaresolverr": service_up(f"http://{host('flaresolverr')}:8191/health"),
    }
    containers = project_containers() if docker_available() else None
    apps = []
    for service, name, what, link in SERVICES:
        c = (containers or {}).get(service)
        if containers is None:
            tone, label = ("up", "Running") if answering[service] else ("wait", "Starting")
        else:
            tone, label = describe(service, c, answering[service])
        if service == "gluetun" and not vpn["configured"]:
            tone, label = "wait", "Waiting for your VPN file"
        detail = None
        if tone in ("wait", "down") and c and not busy.get(service):
            detail = last_log_line(c["id"])
        note = None
        if service in ARR and tone == "up":
            note = {
                "missing": "qBittorrent isn't set up here yet. YAMS is adding it now.",
                "unreachable": "Can't reach qBittorrent right now. YAMS checks again every 10 minutes.",
            }.get(client_status[service]["state"])
        apps.append({"service": service, "name": name, "what": what, "link": link,
                     "tone": tone, "label": label, "detail": detail, "note": note,
                     "can_restart": service in RESTARTABLE and bool(c)})

    indexers = count(f"{PROWLARR}/api/v1/indexer", ph) if answering["prowlarr"] else None
    series = count(f"{SONARR}/api/v3/series", sh) if answering["sonarr"] else None
    movies = count(f"{RADARR}/api/v3/movie", rh) if answering["radarr"] else None
    dl = downloads() if answering["qbittorrent"] else None
    setup = setup_state()
    return {
        "version": YAMS_VERSION,
        "docker": containers is not None,
        "vpn": vpn,
        "apps": apps,
        "answering": answering,
        "downloads": dl,
        "indexers": indexers,
        "series": series,
        "movies": movies,
        "busy_all": busy.get("_all") or (backups.job["step"] if backups.job["active"] else None),
        "backup": backups.status(),
        "setup": {"message": setup.get("message"), "done": setup.get("done", {})},
        "login": {"username": s.get("username"), "password": s.get("password")},
        "jellyfin_port": JELLYFIN_PORT,
        "log_services": [{"service": x, "name": LOG_LABELS[x]} for x in LOG_SERVICES],
        "updated": int(time.time()),
    }


def status_loop():
    while True:
        try:
            status_cache["data"] = gather_status()
        except Exception:
            traceback.print_exc()
        time.sleep(4)


# --------------------------------------------------------------------------- widgets
def widget_stats():
    data = status_cache["data"] or gather_status()
    vpn, dl = data["vpn"], data.get("downloads") or {}
    return {
        "type": "four-stats",
        "link": "",
        "items": [
            {"title": "VPN", "text": "On" if vpn["connected"] else "Off",
             "subtext": vpn.get("country") or ("downloads paused" if not vpn["connected"] else "")},
            {"title": "Downloading", "text": str(dl.get("downloading", 0)),
             "subtext": human_speed(dl.get("speed", 0)) if dl else "paused"},
            {"title": "Shows", "text": str(data.get("series") or 0), "subtext": "followed"},
            {"title": "Movies", "text": str(data.get("movies") or 0), "subtext": "in library"},
        ],
    }


def widget_downloads():
    data = status_cache["data"] or gather_status()
    dl = data.get("downloads")
    if not data["vpn"]["connected"]:
        items = [{"text": "Downloads paused", "subtext": "Connect a VPN in YAMS"}]
    elif not dl or not dl["items"]:
        items = [{"text": "Nothing downloading", "subtext": "Add a show or movie in YAMS"}]
    else:
        items = [{"text": f"{int(t['progress'] * 100)}% \u2022 {t['name']}",
                  "subtext": " \u2022 ".join(x for x in ("\u2193 " + t["speed"], t["eta"]) if x)}
                 for t in dl["items"][:5]]
    return {"type": "list", "link": "", "items": items}


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 64 * 1024:
            raise ValueError("too big")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path == "/api/status":
            return self.send(200, status_cache["data"] or gather_status())
        if path == "/api/logs":
            service = (query.get("service") or [""])[0]
            if service not in LOG_SERVICES:
                return self.send(400, {"error": "Unknown app."})
            try:
                tail = max(50, min(2000, int((query.get("tail") or ["400"])[0])))
            except ValueError:
                tail = 400
            text, error = container_logs(service, tail)
            if error:
                return self.send(503, {"error": error})
            return self.send(200, {"service": service, "logs": text})
        if path == "/api/updates":
            force = (query.get("refresh") or ["0"])[0] == "1"
            return self.send(200, check_updates(force))
        if path == "/api/backups/download":
            try:
                file_path = backups.safe_name((query.get("name") or [""])[0])
            except backups.BackupError as err:
                return self.send(404, {"error": str(err)})
            size = os.path.getsize(file_path)
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(file_path)}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(file_path, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return None
        if path == "/widgets/stats":
            return self.send(200, widget_stats())
        if path == "/widgets/downloads":
            return self.send(200, widget_downloads())
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as fh:
                return self.send(200, fh.read(), "text/html; charset=utf-8")
        return self.send(404, {"error": "Not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/backups/upload":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                name = backups.receive_upload(self.rfile, length, urllib.parse.unquote(self.headers.get("X-Filename", "")))
            except backups.BackupError as err:
                return self.send(400, {"error": str(err)})
            except ValueError:
                return self.send(400, {"error": "Couldn't read that upload."})
            return self.send(200, {"ok": True, "name": name})
        try:
            body = self.read_body()
        except ValueError:
            return self.send(400, {"error": "Couldn't read that request."})

        if path in ("/api/restart", "/api/restart-all") and backups.job["active"]:
            return self.send(409, {"error": "Wait for the backup or restore to finish."})
        if path == "/api/restart":
            service = body.get("service")
            if service not in RESTARTABLE:
                return self.send(400, {"error": "That app can't be restarted from here."})
            if busy.get(service) or busy.get("_all"):
                return self.send(409, {"error": "Already restarting."})
            threading.Thread(target=restart_service, args=(service,), daemon=True).start()
            return self.send(202, {"ok": True})
        if path == "/api/restart-all":
            if busy.get("_all"):
                return self.send(409, {"error": "Already restarting."})
            threading.Thread(target=restart_all, daemon=True).start()
            return self.send(202, {"ok": True})
        if path == "/api/backups/create":
            try:
                backups.start_backup("manual")
            except backups.BackupError as err:
                return self.send(409, {"error": str(err)})
            return self.send(202, {"ok": True})
        if path == "/api/backups/restore":
            if busy.get("_all"):
                return self.send(409, {"error": "Wait for the restart to finish."})
            try:
                backups.start_restore(body.get("name"))
            except backups.BackupError as err:
                return self.send(400, {"error": str(err)})
            return self.send(202, {"ok": True})
        if path == "/api/backups/delete":
            try:
                backups.delete_backup(body.get("name"))
            except backups.BackupError as err:
                return self.send(400, {"error": str(err)})
            return self.send(200, {"ok": True})
        if path == "/api/backups/settings":
            try:
                return self.send(200, backups.update_settings(body.get("schedule"), body.get("keep")))
            except (backups.BackupError, ValueError, TypeError) as err:
                return self.send(400, {"error": str(err)})
        if path == "/api/vpn-check":
            return self.send(200, check_vpn())
        if path == "/api/vpn":
            try:
                conf, server_name = clean_wireguard(body.get("config", ""))
            except VpnError as err:
                return self.send(400, {"error": str(err)})
            tmp = WG_PATH + ".tmp"
            with open(tmp, "w") as fh:
                fh.write(conf)
            os.chmod(tmp, 0o600)
            os.replace(tmp, WG_PATH)
            write_json(f"{STATE_DIR}/vpn.json", {"server": server_name, "saved_at": int(time.time())})
            return self.send(200, {"ok": True, "server": server_name})
        return self.send(404, {"error": "Not found"})


def main():
    backups.hooks.update(stop=pause_apps, start=resume_apps, after_restore=after_restore, version=YAMS_VERSION)
    backups.ensure_backup_dir()
    try:
        backups.resume_after_crash()
    except Exception:
        traceback.print_exc()
    threading.Thread(target=backups.scheduler, daemon=True).start()
    if not os.path.exists(SETUP_PATH):
        write_json(SETUP_PATH, {"done": {}, "message": "Getting everything ready\u2026"})
    threading.Thread(target=configurator, daemon=True).start()
    threading.Thread(target=status_loop, daemon=True).start()
    threading.Thread(target=connection_watch, daemon=True).start()
    print("[yams] dashboard listening on :8000", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()


if __name__ == "__main__":
    main()
