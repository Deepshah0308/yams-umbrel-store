"""
YAMS for Umbrel - dashboard + auto-configurator.

Pure standard-library Python so it runs on the stock python:alpine image.
  * Serves the setup dashboard (index.html) and a small JSON API.
  * In the background, wires every app together through their APIs:
      Sonarr/Radarr -> qBittorrent + library folders + file renaming
      Prowlarr      -> Sonarr, Radarr, FlareSolverr
      Jellyfin      -> first-run wizard, admin user, Movies & Shows libraries
  * Accepts a pasted WireGuard file and turns it into something gluetun accepts.
"""
import configparser
import ipaddress
import json
import os
import socket
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = os.environ.get("APP_PREFIX", "yams-media")
JELLYFIN_PORT = os.environ.get("JELLYFIN_PORT", "8097")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("STATE_DIR", "/state")
WG_PATH = os.environ.get("WG_PATH", "/vpn/wg0.conf")
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
def wire_arr(kind):
    s = secrets()
    if kind == "sonarr":
        base, key, title = SONARR, s["sonarr_key"], "Sonarr"
        root, category_field, category = "/data/media/tv", "tvCategory", "tv"
        rename_field = "renameEpisodes"
    else:
        base, key, title = RADARR, s["radarr_key"], "Radarr"
        root, category_field, category = "/data/media/movies", "movieCategory", "movies"
        rename_field = "renameMovies"
    api = f"{base}/api/v3"
    h = {"X-Api-Key": key}
    if is_done(kind):
        return True
    say(f"Waiting for {title} to start…")
    if not wait_for(f"{api}/system/status", h, title):
        return False
    say(f"Connecting {title} to your library and qBittorrent…")

    # Library folder
    status, folders = http("GET", f"{api}/rootfolder", headers=h)
    if ok(status) and not any(f.get("path", "").rstrip("/") == root for f in folders or []):
        http("POST", f"{api}/rootfolder", {"path": root}, headers=h)

    # Download client
    status, clients = http("GET", f"{api}/downloadclient", headers=h)
    if ok(status) and not any(c.get("implementation") == "QBittorrent" for c in clients or []):
        body = {
            "enable": True,
            "protocol": "torrent",
            "priority": 1,
            "removeCompletedDownloads": True,
            "removeFailedDownloads": True,
            "name": "qBittorrent (VPN)",
            "implementation": "QBittorrent",
            "configContract": "QBittorrentSettings",
            "tags": [],
            "fields": [
                {"name": "host", "value": QBIT_HOST},
                {"name": "port", "value": 8080},
                {"name": "useSsl", "value": False},
                {"name": "username", "value": s["username"]},
                {"name": "password", "value": s["password"]},
                {"name": category_field, "value": category},
            ],
        }
        # forceSave: qBittorrent may still be waiting on the VPN, which is fine.
        http("POST", f"{api}/downloadclient?forceSave=true", body, headers=h)

    # Tidy file names (the default is off)
    status, naming = http("GET", f"{api}/config/naming", headers=h)
    if ok(status) and isinstance(naming, dict) and not naming.get(rename_field):
        naming[rename_field] = True
        http("PUT", f"{api}/config/naming/{naming.get('id', 1)}", naming, headers=h)

    mark(kind, f"{title} is connected.")
    return True


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


# --------------------------------------------------------------------------- status
status_cache = {"data": None}


def service_up(url, headers=None):
    status, _ = http("GET", url, headers=headers, timeout=4)
    return status != 0 and status < 500


def count(url, headers):
    status, items = http("GET", url, headers=headers, timeout=8)
    return len(items) if ok(status) and isinstance(items, list) else None


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
    apps = {
        "jellyfin": service_up(f"{JELLYFIN}/health"),
        "sonarr": service_up(f"{SONARR}/ping"),
        "radarr": service_up(f"{RADARR}/ping"),
        "prowlarr": service_up(f"{PROWLARR}/ping"),
        "bazarr": service_up(f"{BAZARR}/"),
        "qbittorrent": vpn["connected"] and service_up(f"http://{QBIT_HOST}:8080/api/v2/app/version"),
    }
    indexers = count(f"{PROWLARR}/api/v1/indexer", ph) if apps["prowlarr"] else None
    series = count(f"{SONARR}/api/v3/series", sh) if apps["sonarr"] else None
    movies = count(f"{RADARR}/api/v3/movie", rh) if apps["radarr"] else None
    setup = setup_state()
    return {
        "vpn": vpn,
        "apps": apps,
        "indexers": indexers,
        "series": series,
        "movies": movies,
        "setup": {"message": setup.get("message"), "done": setup.get("done", {})},
        "login": {"username": s.get("username"), "password": s.get("password")},
        "jellyfin_port": JELLYFIN_PORT,
        "updated": int(time.time()),
    }


def status_loop():
    while True:
        try:
            status_cache["data"] = gather_status()
        except Exception:
            traceback.print_exc()
        time.sleep(4)


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

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/status":
            data = status_cache["data"] or gather_status()
            return self.send(200, data)
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as fh:
                return self.send(200, fh.read(), "text/html; charset=utf-8")
        return self.send(404, {"error": "Not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/api/vpn":
            return self.send(404, {"error": "Not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 64 * 1024:
            return self.send(413, {"error": "That file is too big to be a WireGuard config."})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            conf, server_name = clean_wireguard(body.get("config", ""))
        except VpnError as err:
            return self.send(400, {"error": str(err)})
        except ValueError:
            return self.send(400, {"error": "Couldn't read that request."})
        tmp = WG_PATH + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(conf)
        os.chmod(tmp, 0o600)
        os.replace(tmp, WG_PATH)
        write_json(f"{STATE_DIR}/vpn.json", {"server": server_name, "saved_at": int(time.time())})
        return self.send(200, {"ok": True, "server": server_name})


def main():
    if not os.path.exists(SETUP_PATH):
        write_json(SETUP_PATH, {"done": {}, "message": "Getting everything ready…"})
    threading.Thread(target=configurator, daemon=True).start()
    threading.Thread(target=status_loop, daemon=True).start()
    print("[yams] dashboard listening on :8000", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()


if __name__ == "__main__":
    main()
