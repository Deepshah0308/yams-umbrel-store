"""Fakes used by the tests: a small in-memory Docker Engine served over a Unix socket,
and strict fake Sonarr/Radarr/Prowlarr APIs that reject incomplete forms like the real apps."""
import copy
import json
import os
import socketserver
import struct
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# --------------------------------------------------------------------------- Docker
class FakeDocker:
    def __init__(self):
        self.containers = {}   # id -> container dict (inspect format)
        self.images = {}       # id -> image dict (inspect format)
        self.tags = {}         # "name:tag" -> image id
        self.registry = {}     # "name:tag" -> {"digest", "config"}  (what a pull would fetch)
        self.fail_start = set()  # image ids whose containers refuse to start
        self.pull_error = None
        self.calls = []
        self._n = 0
        self.dir = tempfile.mkdtemp()
        self.sock = os.path.join(self.dir, "docker.sock")

    # ---- setup helpers
    def _id(self, prefix):
        self._n += 1
        return f"{prefix}{self._n:04d}" + "0" * 56

    def add_image(self, ref, digest, config):
        image_id = "sha256:" + self._id("i")
        name = ref.split(":")[0]
        self.images[image_id] = {"Id": image_id, "RepoDigests": [f"{name}@{digest}"], "Config": config}
        self.tags[ref] = image_id
        return image_id

    def add_container(self, service, ref, config=None, host=None, networks=None, running=True):
        cid = self._id("c")
        image_id = self.tags[ref]
        img_cfg = self.images[image_id]["Config"]
        cfg = copy.deepcopy(config or {})
        # Like Docker: the container's config is the image's config plus what compose set.
        merged = {"Hostname": cid[:12], "Image": ref,
                  "Env": list(img_cfg.get("Env", [])) + cfg.pop("Env", []),
                  "Labels": {**img_cfg.get("Labels", {}), **cfg.pop("Labels", {}),
                             "com.docker.compose.project": "yams-media",
                             "com.docker.compose.service": service},
                  "Cmd": img_cfg.get("Cmd"), "Entrypoint": img_cfg.get("Entrypoint")}
        merged.update(cfg)
        self.containers[cid] = {
            "Id": cid, "Name": f"/yams-media_{service}_1", "Image": image_id,
            "Config": merged, "HostConfig": host or {"NetworkMode": "umbrel_main_network", "Binds": [f"/data/{service}:/config"]},
            "NetworkSettings": {"Networks": networks if networks is not None else
                                {"umbrel_main_network": {"Aliases": [service, f"yams-media_{service}_1", cid[:12]],
                                                         "IPAMConfig": None, "Links": None}}},
            "State": {"Running": running},
        }
        return cid

    def publish(self, ref, digest, config):
        self.registry[ref] = {"digest": digest, "config": config}

    def by_service(self, service):
        for c in self.containers.values():
            if c["Config"]["Labels"].get("com.docker.compose.service") == service and not c["Name"].endswith("-yams-old"):
                return c
        return None

    # ---- server
    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def address_string(self):
                return "docker"

            def reply(self, code, obj=b""):
                body = b"" if code in (204, 304) else (obj if isinstance(obj, bytes) else json.dumps(obj).encode())
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def do_GET(self):
                fake.calls.append(("GET", self.path))
                path, _, query = self.path.partition("?")
                q = urllib.parse.parse_qs(query)
                if path == "/containers/json":
                    return self.reply(200, [{"Id": c["Id"], "State": "running" if c["State"]["Running"] else "exited",
                                             "Status": "", "Labels": c["Config"]["Labels"]}
                                            for c in fake.containers.values()])
                if path.startswith("/containers/") and path.endswith("/json"):
                    c = fake.containers.get(path.split("/")[2])
                    return self.reply(200, c) if c else self.reply(404, {"message": "no such container"})
                if path.startswith("/images/") and path.endswith("/json"):
                    key = urllib.parse.unquote(path[len("/images/"):-len("/json")])
                    image_id = fake.tags.get(key, key)
                    img = fake.images.get(image_id)
                    return self.reply(200, img) if img else self.reply(404, {"message": "no such image"})
                if path.startswith("/distribution/"):
                    ref = urllib.parse.unquote(path[len("/distribution/"):-len("/json")])
                    entry = fake.registry.get(ref)
                    if entry:
                        return self.reply(200, {"Descriptor": {"digest": entry["digest"]}})
                    image_id = fake.tags.get(ref)
                    if image_id:
                        digest = fake.images[image_id]["RepoDigests"][0].split("@")[1]
                        return self.reply(200, {"Descriptor": {"digest": digest}})
                    return self.reply(404, {"message": "not found"})
                if path.endswith("/logs"):
                    text = b"hello\n"
                    return self.reply(200, bytes([1, 0, 0, 0]) + struct.pack(">I", len(text)) + text)
                return self.reply(404, {"message": "unknown"})

            def do_POST(self):
                fake.calls.append(("POST", self.path))
                path, _, query = self.path.partition("?")
                q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
                parts = path.split("/")
                if path == "/images/create":
                    ref = f"{q['fromImage']}:{q['tag']}"
                    if fake.pull_error:
                        return self.reply(200, json.dumps({"error": fake.pull_error}).encode() + b"\n")
                    entry = fake.registry.get(ref)
                    if entry and ref in fake.tags:
                        current = fake.images[fake.tags[ref]]["RepoDigests"][0].split("@")[1]
                        if current != entry["digest"]:
                            fake.add_image(ref, entry["digest"], entry["config"])
                    return self.reply(200, b'{"status":"Pulling"}\n{"status":"Done"}\n')
                if path == "/containers/create":
                    body = self.body()
                    name = q["name"]
                    if any(c["Name"] == "/" + name for c in fake.containers.values()):
                        return self.reply(409, {"message": "name in use"})
                    host = body.get("HostConfig") or {}
                    if (host.get("NetworkMode") or "").startswith("container:") and body.get("Hostname"):
                        return self.reply(400, {"message": "conflicting options: hostname and the network mode"})
                    ref = body["Image"]
                    image_id = fake.tags[ref]
                    img_cfg = fake.images[image_id]["Config"]
                    cid = fake._id("c")
                    cfg = {k: v for k, v in body.items() if k not in ("HostConfig", "NetworkingConfig")}
                    cfg["Env"] = list(img_cfg.get("Env", [])) + [e for e in cfg.get("Env") or [] if e not in img_cfg.get("Env", [])]
                    cfg["Labels"] = {**img_cfg.get("Labels", {}), **(cfg.get("Labels") or {})}
                    for key in ("Cmd", "Entrypoint"):
                        cfg.setdefault(key, img_cfg.get(key))
                    cfg["Hostname"] = cid[:12]
                    nets = {}
                    for net, ep in ((body.get("NetworkingConfig") or {}).get("EndpointsConfig") or {}).items():
                        nets[net] = {"Aliases": list(ep.get("Aliases") or []) + [cid[:12]], "IPAMConfig": None, "Links": None}
                    fake.containers[cid] = {"Id": cid, "Name": "/" + name, "Image": image_id, "Config": cfg,
                                            "HostConfig": host, "NetworkSettings": {"Networks": nets},
                                            "State": {"Running": False}, "CreateBody": body}
                    return self.reply(201, {"Id": cid})
                cid = parts[2] if len(parts) > 2 else ""
                c = fake.containers.get(cid)
                if not c:
                    return self.reply(404, {"message": "no such container"})
                action = parts[3]
                if action == "stop":
                    c["State"]["Running"] = False
                    return self.reply(204)
                if action == "start":
                    if c["Image"] in fake.fail_start:
                        return self.reply(500, {"message": "exec format error"})
                    c["State"]["Running"] = True
                    return self.reply(204)
                if action == "restart":
                    c["State"]["Running"] = True
                    return self.reply(204)
                if action == "rename":
                    c["Name"] = "/" + q["name"]
                    return self.reply(204)
                return self.reply(404, {"message": "unknown"})

            def do_DELETE(self):
                fake.calls.append(("DELETE", self.path))
                path = self.path.split("?")[0]
                parts = path.split("/")
                if parts[1] == "containers":
                    fake.containers.pop(parts[2], None)
                    return self.reply(204)
                if parts[1] == "images":
                    image_id = urllib.parse.unquote(parts[2])
                    if any(c["Image"] == image_id for c in fake.containers.values()):
                        return self.reply(409, {"message": "image in use"})
                    fake.images.pop(image_id, None)
                    return self.reply(200, [])
                return self.reply(404, {"message": "unknown"})

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

            def get_request(self):
                request, _ = super().get_request()
                return request, ("docker", 0)

        self.server = Server(self.sock, Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# --------------------------------------------------------------------------- Sonarr / Radarr / Prowlarr
ARR_FIELDS = {
    "sonarr": ["host", "port", "useSsl", "urlBase", "username", "password", "tvCategory", "tvImportedCategory",
               "recentTvPriority", "olderTvPriority", "initialState", "sequentialOrder", "firstAndLast", "contentLayout"],
    "radarr": ["host", "port", "useSsl", "urlBase", "username", "password", "movieCategory", "movieImportedCategory",
               "recentMoviePriority", "olderMoviePriority", "initialState", "sequentialOrder", "firstAndLast", "contentLayout"],
}
PROWLARR_FIELDS = ["prowlarrUrl", "baseUrl", "apiKey", "syncCategories", "animeSyncCategories", "syncRejectBlocklistedTorrentHashesWhileGrabbing"]


class FakeApps:
    """Sonarr on /sonarr, Radarr on /radarr and Prowlarr on /prowlarr, all strict about forms."""

    def __init__(self, keys):
        self.keys = keys  # {"sonarr": key, "radarr": key, "prowlarr": key}
        self.clients = {"sonarr": [], "radarr": []}
        self.roots = {"sonarr": [], "radarr": []}
        self.apps = []
        self.commands = []
        self.qbit_ok = True
        self.links_ok = True

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def route(self):
                parts = self.path.split("?")[0].strip("/").split("/")
                app = parts[0]
                if self.headers.get("X-Api-Key") != fake.keys.get(app):
                    return app, None
                return app, "/" + "/".join(parts[3:])  # strip /<app>/api/vN

            def do_GET(self):
                app, rest = self.route()
                if rest is None:
                    return self.reply(401, {})
                if rest == "/system/status":
                    return self.reply(200, {"version": "1"})
                if app in ("sonarr", "radarr"):
                    if rest == "/downloadclient/schema":
                        return self.reply(200, [{"implementation": "QBittorrent", "configContract": "QBittorrentSettings",
                                                 "protocol": "torrent", "presets": [],
                                                 "fields": [{"name": n, "value": None} for n in ARR_FIELDS[app]]}])
                    if rest == "/downloadclient":
                        return self.reply(200, copy.deepcopy(fake.clients[app]))
                    if rest == "/rootfolder":
                        return self.reply(200, fake.roots[app])
                if app == "prowlarr":
                    if rest == "/applications/schema":
                        return self.reply(200, [{"implementation": n, "configContract": f"{n}Settings", "presets": [],
                                                 "fields": [{"name": f, "value": None} for f in PROWLARR_FIELDS]}
                                                for n in ("Sonarr", "Radarr", "Lidarr")])
                    if rest == "/applications":
                        return self.reply(200, copy.deepcopy(fake.apps))
                    if rest == "/tag":
                        return self.reply(200, [{"id": 1, "label": "flaresolverr"}])
                    if rest == "/indexerProxy":
                        return self.reply(200, [{"implementation": "FlareSolverr"}])
                    if rest == "/indexer":
                        return self.reply(200, [{"id": 1}, {"id": 2}])
                return self.reply(404, {})

            def do_POST(self):
                app, rest = self.route()
                if rest is None:
                    return self.reply(401, {})
                body = self.body()
                if app in ("sonarr", "radarr"):
                    if rest == "/downloadclient/test":
                        return self.reply(200 if fake.qbit_ok else 400, [])
                    if rest == "/downloadclient":
                        names = {f["name"] for f in body.get("fields", [])}
                        if set(ARR_FIELDS[app]) - names:
                            return self.reply(400, [{"errorMessage": "missing fields"}])
                        body["id"] = len(fake.clients[app]) + 1
                        fake.clients[app].append(body)
                        return self.reply(201, body)
                    if rest == "/rootfolder":
                        fake.roots[app].append({"path": body["path"]})
                        return self.reply(201, body)
                    if rest == "/command":
                        fake.commands.append((app, body["name"]))
                        return self.reply(201, {})
                if app == "prowlarr":
                    if rest == "/applications/test":
                        return self.reply(200 if fake.links_ok else 400, [])
                    if rest == "/applications":
                        names = {f["name"] for f in body.get("fields", [])}
                        if set(PROWLARR_FIELDS) - names:
                            return self.reply(400, [{"errorMessage": "missing fields"}])
                        body["id"] = len(fake.apps) + 1
                        fake.apps.append(body)
                        return self.reply(201, body)
                    if rest == "/command":
                        fake.commands.append((app, body["name"]))
                        return self.reply(201, {})
                return self.reply(404, {})

            def do_PUT(self):
                app, rest = self.route()
                body = self.body()
                if app in ("sonarr", "radarr") and rest.startswith("/downloadclient/"):
                    fake.clients[app] = [body if c["id"] == body["id"] else c for c in fake.clients[app]]
                    return self.reply(202, body)
                if app == "prowlarr" and rest.startswith("/applications/"):
                    fake.apps = [body if a["id"] == body["id"] else a for a in fake.apps]
                    fake.links_ok = True
                    return self.reply(202, body)
                return self.reply(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def url(self, app):
        return f"http://127.0.0.1:{self.port}/{app}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
