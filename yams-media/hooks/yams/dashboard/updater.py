"""
YAMS for Umbrel - app updates, without waiting for a new YAMS release.

The apps use moving tags (for example linuxserver/sonarr:latest), so "update" means:
  1. ask Docker Hub whether that tag now points to a newer image (no download),
  2. back up the settings,
  3. download the new images while the apps keep running,
  4. recreate each changed container with exactly the same settings, rolling back to
     the old container if the new one won't start.

Recreated containers keep their Docker Compose labels, so umbrelOS still treats them
as part of the YAMS app, and it reuses the newer image on its next restart.
"""
import json
import os
import re
import threading
import time
import traceback
import urllib.parse

# Set by server.py
hooks = {"docker": None, "containers": None, "inspect": None, "backup": None, "set_busy": None}

STATE_DIR = os.environ.get("STATE_DIR", "/yams/state")
SETTINGS_PATH = f"{STATE_DIR}/update-settings.json"

# gluetun first: qBittorrent lives inside its network and is rebuilt after it.
APPS = ["gluetun", "qbittorrent", "jellyfin", "sonarr", "radarr", "prowlarr", "bazarr", "flaresolverr"]
NAMES = {"gluetun": "VPN", "qbittorrent": "qBittorrent", "jellyfin": "Jellyfin", "sonarr": "Sonarr",
         "radarr": "Radarr", "prowlarr": "Prowlarr", "bazarr": "Bazarr", "flaresolverr": "FlareSolverr"}

job_lock = threading.Lock()
job = {"active": False, "step": None, "message": None, "error": None, "finished_at": None}
check_cache = {"data": None, "at": 0}


class UpdateError(Exception):
    pass


def _docker(*args, **kwargs):
    return hooks["docker"](*args, **kwargs)


def _json(raw):
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- settings
def settings():
    try:
        with open(SETTINGS_PATH) as fh:
            s = json.load(fh)
    except (FileNotFoundError, ValueError):
        s = {}
    s.setdefault("auto", "off")
    s.setdefault("last_auto", 0)
    return s


def update_settings(auto):
    if auto not in ("off", "weekly"):
        raise UpdateError("Choose Off or Every week.")
    s = settings()
    s["auto"] = auto
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(s, fh, indent=2)
    os.replace(tmp, SETTINGS_PATH)
    return s


# --------------------------------------------------------------------------- image helpers
def split_ref(ref):
    """'linuxserver/sonarr:latest' -> ('linuxserver/sonarr', 'latest'); drops any @digest."""
    ref = ref.split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = ref.rsplit(":", 1)
        return name, tag
    return ref, "latest"


def is_pinned(ref):
    return "@sha256:" in ref


def image_info(image):
    status, raw = _docker("GET", f"/images/{urllib.parse.quote(image, safe='')}/json")
    return _json(raw) if status == 200 else None


def remote_digest(ref):
    """The digest the registry currently serves for this tag, without downloading it."""
    status, raw = _docker("GET", f"/distribution/{urllib.parse.quote(ref, safe='')}/json", timeout=30)
    data = _json(raw) if status == 200 else None
    return ((data or {}).get("Descriptor") or {}).get("digest")


def image_version(img):
    labels = ((img or {}).get("Config") or {}).get("Labels") or {}
    for key in ("org.opencontainers.image.version", "build_version", "version"):
        value = labels.get(key)
        if value:
            match = re.search(r"\d+(?:\.\d+)+", value)
            return match.group(0) if match else value[:40]
    return None


# --------------------------------------------------------------------------- checking
def check(force=False):
    if not force and check_cache["data"] and time.time() - check_cache["at"] < 1800:
        return check_cache["data"]
    containers = hooks["containers"]() or {}
    rows = []
    for service in APPS:
        row = {"service": service, "name": NAMES[service], "version": None, "status": "unknown"}
        c = containers.get(service)
        info = hooks["inspect"](c["id"]) if c else None
        if not info:
            row["status"] = "missing"
            rows.append(row)
            continue
        ref = (info.get("Config") or {}).get("Image", "")
        img = image_info(info.get("Image", ""))
        row["version"] = image_version(img)
        if is_pinned(ref):
            row["status"] = "pinned"
        else:
            digest = remote_digest(ref)
            local = {d.split("@", 1)[-1] for d in (img or {}).get("RepoDigests") or []}
            if not digest:
                row["status"] = "unreachable"
            else:
                row["status"] = "current" if digest in local else "update"
        rows.append(row)
    data = {"checked_at": int(time.time()), "apps": rows,
            "available": [r["service"] for r in rows if r["status"] == "update"]}
    check_cache.update(data=data, at=time.time())
    return data


# --------------------------------------------------------------------------- recreating
def _strip_image_defaults(config, image_config, ref):
    """Keep only what the container set on top of its image, so the new image's own
    defaults (environment, labels, command) take effect. This is how Watchtower does it."""
    out = dict(config)
    img = image_config or {}
    for key in ("Cmd", "Entrypoint", "WorkingDir", "User", "Healthcheck", "StopSignal", "Shell", "OnBuild"):
        if key in out and out.get(key) == img.get(key):
            out.pop(key)
    img_env = set(img.get("Env") or [])
    out["Env"] = [e for e in out.get("Env") or [] if e not in img_env]
    img_labels = img.get("Labels") or {}
    out["Labels"] = {k: v for k, v in (out.get("Labels") or {}).items() if img_labels.get(k) != v}
    img_ports = img.get("ExposedPorts") or {}
    out["ExposedPorts"] = {k: v for k, v in (out.get("ExposedPorts") or {}).items() if k not in img_ports} or None
    img_vols = img.get("Volumes") or {}
    out["Volumes"] = {k: v for k, v in (out.get("Volumes") or {}).items() if k not in img_vols} or None
    for key in ("Hostname", "Domainname", "Image"):
        out.pop(key, None)
    out["Image"] = ref
    return out


def _create_body(info, old_image_config, network_mode=None):
    ref = info["Config"]["Image"]
    config = _strip_image_defaults(info["Config"], old_image_config, ref)
    host = dict(info.get("HostConfig") or {})
    if network_mode:
        host["NetworkMode"] = network_mode
    body = dict(config)
    body["HostConfig"] = host
    if (host.get("NetworkMode") or "").startswith("container:"):
        body.pop("ExposedPorts", None)
        body["NetworkingConfig"] = {"EndpointsConfig": {}}
    else:
        short_id = info["Id"][:12]
        endpoints = {}
        for net, ep in ((info.get("NetworkSettings") or {}).get("Networks") or {}).items():
            endpoints[net] = {
                "Aliases": [a for a in (ep.get("Aliases") or []) if a != short_id],
                "IPAMConfig": ep.get("IPAMConfig"),
                "Links": ep.get("Links"),
            }
        body["NetworkingConfig"] = {"EndpointsConfig": endpoints}
    return body


def recreate(service, info, network_mode=None):
    """Replace a container with one built from its (newly pulled) image. Returns the new
    container id. Puts the old container back if anything goes wrong."""
    old_id = info["Id"]
    name = info["Name"].lstrip("/")
    old_image = image_info(info.get("Image", "")) or {}
    body = _create_body(info, old_image.get("Config"), network_mode)
    was_running = (info.get("State") or {}).get("Running")
    backup_name = f"{name}-yams-old"

    _docker("POST", f"/containers/{old_id}/stop?t=30", timeout=90)
    status, _ = _docker("POST", f"/containers/{old_id}/rename?name={urllib.parse.quote(backup_name)}")
    if status not in (200, 204):
        if was_running:
            _docker("POST", f"/containers/{old_id}/start", timeout=60)
        raise UpdateError(f"Docker wouldn't rename the old {NAMES[service]} container.")

    new_id = None
    try:
        status, raw = _docker("POST", f"/containers/create?name={urllib.parse.quote(name)}", timeout=60, body=body)
        created = _json(raw) or {}
        if status != 201 or not created.get("Id"):
            raise UpdateError(f"Docker couldn't create the new {NAMES[service]} container: {created.get('message', status)}")
        new_id = created["Id"]
        if was_running:
            status, raw = _docker("POST", f"/containers/{new_id}/start", timeout=60)
            if status not in (204, 304):
                raise UpdateError(f"The new {NAMES[service]} container wouldn't start: {(_json(raw) or {}).get('message', status)}")
    except Exception:
        # Roll back: remove the new container and bring the old one back.
        if new_id:
            _docker("DELETE", f"/containers/{new_id}?force=1", timeout=60)
        _docker("POST", f"/containers/{old_id}/rename?name={urllib.parse.quote(name)}")
        if was_running:
            _docker("POST", f"/containers/{old_id}/start", timeout=60)
        raise

    _docker("DELETE", f"/containers/{old_id}?force=1", timeout=60)
    if old_image.get("Id"):
        _docker("DELETE", f"/images/{old_image['Id']}", timeout=60)  # fails harmlessly if still used
    return new_id


def pull(ref):
    name, tag = split_ref(ref)
    query = urllib.parse.urlencode({"fromImage": name, "tag": tag})
    status, raw = _docker("POST", f"/images/create?{query}", timeout=1800)
    if status != 200:
        raise UpdateError(f"Docker couldn't download {name}:{tag} ({status}).")
    for line in raw.decode(errors="replace").splitlines():
        message = (_json(line) or {}).get("error")
        if message:
            raise UpdateError(f"Downloading {name}:{tag} failed: {message}")


# --------------------------------------------------------------------------- the update job
def _begin():
    with job_lock:
        if job["active"]:
            raise UpdateError("An update is already running.")
        job.update(active=True, step="Getting ready\u2026", message=None, error=None, finished_at=None)


def _step(text):
    job["step"] = text
    print(f"[yams] {text}", flush=True)


def _finish(message=None, error=None):
    job.update(active=False, step=None, message=message, error=error, finished_at=time.time())
    print(f"[yams] {error or message}", flush=True)


def run_update(services=None, backup=True):
    set_busy = hooks["set_busy"] or (lambda *_: None)
    try:
        containers = hooks["containers"]() or {}
        wanted = [s for s in APPS if s in containers and (services is None or s in services)]
        infos = {s: hooks["inspect"](containers[s]["id"]) for s in wanted}
        refs = {s: infos[s]["Config"]["Image"] for s in wanted if infos.get(s)}
        refs = {s: r for s, r in refs.items() if not is_pinned(r)}
        if not refs:
            _finish(message="Nothing to update.")
            return

        if backup and hooks["backup"]:
            _step("Backing up your settings first\u2026")
            if not hooks["backup"]():
                _finish(error="The safety backup didn't finish, so nothing was updated. See the Backups section.")
                return

        changed = []
        for s, ref in refs.items():
            _step(f"Downloading the newest {NAMES[s]}\u2026")
            pull(ref)
            new_image = image_info(ref) or {}
            if new_image.get("Id") and new_image["Id"] != infos[s].get("Image"):
                changed.append(s)
        if not changed:
            check_cache.update(data=None, at=0)
            _finish(message="Every app is already on its newest version.")
            return

        # qBittorrent lives inside the VPN container's network, so it must be rebuilt
        # whenever the VPN container is replaced, even if only the VPN was selected.
        if "gluetun" in changed and "qbittorrent" in containers and "qbittorrent" not in changed:
            changed.append("qbittorrent")
        changed.sort(key=APPS.index)

        new_ids, done = {}, []
        for s in changed:
            _step(f"Updating {NAMES[s]}\u2026")
            set_busy(s, "Updating\u2026")
            try:
                info = hooks["inspect"](containers[s]["id"])
                network_mode = None
                if s == "qbittorrent" and "gluetun" in new_ids:
                    network_mode = f"container:{new_ids['gluetun']}"
                new_ids[s] = recreate(s, info, network_mode)
                done.append(NAMES[s])
                if s == "gluetun":
                    time.sleep(5)
            finally:
                set_busy(s, None)
        check_cache.update(data=None, at=0)
        _finish(message=f"Updated {', '.join(done)}. Everything else was already up to date.")
    except Exception as err:
        if not isinstance(err, UpdateError):
            traceback.print_exc()
        check_cache.update(data=None, at=0)
        _finish(error=f"The update stopped: {err} Apps that weren't updated are unchanged.")


def start_update(services=None):
    _begin()
    threading.Thread(target=run_update, args=(services,), daemon=True).start()


def status():
    recent = job["finished_at"] and time.time() - job["finished_at"] < 900
    return {
        "job": {"active": job["active"], "step": job["step"],
                "message": job["message"] if recent else None, "error": job["error"] if recent else None},
        "settings": {k: v for k, v in settings().items() if k in ("auto", "last_auto")},
        "check": check_cache["data"],
    }


# --------------------------------------------------------------------------- schedule
def due(s, now=None):
    now = now or time.time()
    if s.get("auto") != "weekly":
        return False
    since = now - s.get("last_auto", 0)
    hour = time.localtime(now).tm_hour
    # Runs in the 5-6 AM window, after any automatic backup (3-5 AM).
    return (since >= 7 * 86400 - 3600 and 5 <= hour < 6) or since >= 8 * 86400


def scheduler():
    while True:
        time.sleep(600)
        try:
            s = settings()
            if due(s) and not job["active"]:
                s["last_auto"] = int(time.time())
                with open(SETTINGS_PATH, "w") as fh:
                    json.dump(s, fh, indent=2)
                _begin()
                run_update()
        except UpdateError:
            pass
        except Exception:
            traceback.print_exc()
