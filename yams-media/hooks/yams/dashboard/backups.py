"""
YAMS for Umbrel - backups, modelled on `yams backup`.

A backup is one .tar.gz holding every app's settings, the logins and API keys,
and the VPN file. Like the YAMS CLI, apps are paused while it's written so their
databases are saved cleanly, and caches and transcodes are left out.
Media files are never included; they stay in Downloads -> media.

Backups live in Downloads -> yams-backups so they can be downloaded or copied
from Umbrel's Files app. Restoring checks the archive first, saves a safety
backup of the current setup, then swaps the settings in and starts the apps.
"""
import io
import json
import os
import posixpath
import re
import shutil
import tarfile
import threading
import time
import traceback

DATA_DIR = os.environ.get("DATA_DIR", "/yams")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/backups")
SETTINGS_PATH = f"{DATA_DIR}/state/backup-settings.json"
PAUSED_PATH = f"{DATA_DIR}/state/paused-by-backup.json"
UID = GID = 1000

FORMAT = 1
MARKER = "yams-backup.json"
INCLUDE = ["config", "state", "vpn/wireguard"]
SKIP_DIRS = {"cache", "transcodes", "transcoding-temp", "logs", "MediaCover", "Backups", "__pycache__"}
SKIP_FILE = re.compile(r"(\.pid|\.tmp|\.lock|\.part)$")
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(tar\.gz|tgz)$")
LABEL_IN_NAME = re.compile(r"-(manual|auto|pre-restore|pre-update|uploaded)\.tar\.gz$")
LABELS = {"manual": "Manual", "auto": "Automatic", "pre-restore": "Before restore",
          "pre-update": "Before update", "uploaded": "Uploaded"}

# Set by server.py: stop(reason, include_vpn) -> list of stopped services,
# start(services), after_restore(), version string.
hooks = {"stop": None, "start": None, "after_restore": None, "version": ""}

job_lock = threading.Lock()
job = {"active": False, "kind": None, "step": None, "message": None, "error": None, "finished_at": None}


class BackupError(Exception):
    pass


# --------------------------------------------------------------------------- helpers
def _read_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _chown_tree(path):
    if not os.path.exists(path):
        return
    os.chown(path, UID, GID)
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.lchown(os.path.join(root, name), UID, GID)
            except OSError:
                pass


def ensure_backup_dir():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    try:
        os.chown(BACKUP_DIR, UID, GID)
    except OSError:
        pass


def settings():
    s = _read_json(SETTINGS_PATH, {})
    s.setdefault("schedule", "off")
    s.setdefault("keep", 5)
    s.setdefault("last_auto", 0)
    return s


def update_settings(schedule=None, keep=None):
    s = settings()
    if schedule is not None:
        if schedule not in ("off", "daily", "weekly"):
            raise BackupError("Unknown schedule.")
        s["schedule"] = schedule
    if keep is not None:
        if int(keep) not in (3, 5, 10):
            raise BackupError("Choose 3, 5 or 10.")
        s["keep"] = int(keep)
    _write_json(SETTINGS_PATH, s)
    return s


def safe_name(name):
    name = os.path.basename(name or "")
    if not NAME_OK.match(name):
        raise BackupError("That isn't a backup file name YAMS recognises.")
    path = os.path.join(BACKUP_DIR, name)
    if not os.path.isfile(path):
        raise BackupError("That backup no longer exists.")
    return path


def list_backups():
    try:
        names = os.listdir(BACKUP_DIR)
    except FileNotFoundError:
        return []
    items = []
    for name in names:
        if name.startswith(".") or not NAME_OK.match(name):
            continue
        path = os.path.join(BACKUP_DIR, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        match = LABEL_IN_NAME.search(name)
        label = match.group(1) if match else "uploaded"
        items.append({"name": name, "size": st.st_size, "created": int(st.st_mtime),
                      "label": label, "label_text": LABELS.get(label, "Backup")})
    items.sort(key=lambda x: x["created"], reverse=True)
    return items


def status():
    recent = job["finished_at"] and time.time() - job["finished_at"] < 900
    return {
        "job": {
            "active": job["active"], "kind": job["kind"], "step": job["step"],
            "message": job["message"] if recent else None,
            "error": job["error"] if recent else None,
        },
        "settings": {k: v for k, v in settings().items() if k in ("schedule", "keep", "last_auto")},
        "items": list_backups(),
    }


# --------------------------------------------------------------------------- job tracking
def _begin(kind):
    with job_lock:
        if job["active"]:
            raise BackupError("A backup or restore is already running.")
        job.update(active=True, kind=kind, step="Getting ready\u2026", message=None, error=None, finished_at=None)


def _step(text):
    job["step"] = text
    print(f"[yams] {text}", flush=True)


def _finish(message=None, error=None):
    job.update(active=False, step=None, message=message, error=error, finished_at=time.time())
    print(f"[yams] {error or message}", flush=True)


# --------------------------------------------------------------------------- pause / resume
def _pause(reason, include_vpn=False):
    stopped = hooks["stop"](reason, include_vpn) if hooks["stop"] else []
    _write_json(PAUSED_PATH, {"services": stopped, "at": int(time.time())})
    return stopped


def _resume(stopped):
    try:
        if hooks["start"]:
            hooks["start"](stopped)
    finally:
        try:
            os.remove(PAUSED_PATH)
        except FileNotFoundError:
            pass


def resume_after_crash():
    """If the dashboard died mid-backup, start the apps it had paused."""
    paused = _read_json(PAUSED_PATH, None)
    if paused and paused.get("services"):
        print("[yams] Starting apps that were paused by an interrupted backup", flush=True)
        _resume(paused["services"])


# --------------------------------------------------------------------------- writing backups
def _tar_filter(info):
    parts = info.name.split("/")
    if any(p in SKIP_DIRS for p in parts[1:]):
        return None
    if not (info.isfile() or info.isdir()):
        return None  # skip symlinks, sockets, devices
    if info.isfile() and (SKIP_FILE.search(info.name) or info.name == "state/paused-by-backup.json"):
        return None
    info.uid, info.gid, info.uname, info.gname = UID, GID, "", ""
    return info


def _write_archive(label):
    ensure_backup_dir()
    stamp = time.strftime("%Y-%m-%d-%H%M%S")
    name = f"yams-umbrel-backup-{stamp}-{label}.tar.gz"
    final = os.path.join(BACKUP_DIR, name)
    part = os.path.join(BACKUP_DIR, f".{name}.part")
    includes = [rel for rel in INCLUDE if os.path.exists(os.path.join(DATA_DIR, rel))]
    meta = {"format": FORMAT, "created": int(time.time()), "label": label,
            "yams_version": hooks.get("version") or "", "includes": includes}
    try:
        with tarfile.open(part, "w:gz", compresslevel=6) as tar:
            data = json.dumps(meta, indent=2).encode()
            info = tarfile.TarInfo(MARKER)
            info.size, info.mtime, info.mode = len(data), time.time(), 0o600
            tar.addfile(info, io.BytesIO(data))
            for rel in includes:
                tar.add(os.path.join(DATA_DIR, rel), arcname=rel, filter=_tar_filter)
        os.replace(part, final)
        os.chmod(final, 0o600)
        os.chown(final, UID, GID)
    finally:
        if os.path.exists(part):
            os.remove(part)
    return name


def prune_auto(keep):
    autos = [b for b in list_backups() if b["label"] == "auto"]
    for old in autos[keep:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old["name"]))
        except OSError:
            pass


def run_backup(label="manual"):
    """Blocking. Use start_backup() from request handlers."""
    stopped = []
    try:
        _step("Pausing apps so their databases are saved cleanly\u2026")
        stopped = _pause("Paused for backup\u2026")
        _step("Writing the backup\u2026")
        name = _write_archive(label)
    except Exception as err:
        traceback.print_exc()
        _step("Starting apps again\u2026")
        _resume(stopped)
        _finish(error=f"The backup didn't finish: {err}")
        return None
    _step("Starting apps again\u2026")
    _resume(stopped)
    if label == "auto":
        prune_auto(settings()["keep"])
    _finish(message=f"Backup saved as {name}.")
    return name


def start_backup(label="manual"):
    _begin("backup")
    threading.Thread(target=run_backup, args=(label,), daemon=True).start()


# --------------------------------------------------------------------------- checking archives
def inspect_archive(path):
    """Return the backup's metadata, or raise BackupError explaining what's wrong."""
    try:
        tar = tarfile.open(path, "r:gz")
    except (tarfile.TarError, OSError, EOFError):
        raise BackupError("That file isn't a readable .tar.gz backup.")
    meta = None
    with tar:
        try:
            members = tar.getmembers()
        except (tarfile.TarError, OSError, EOFError):
            raise BackupError("That backup file is damaged or incomplete.")
        for m in members:
            name = posixpath.normpath(m.name)
            if name.startswith("/") or name == ".." or name.startswith("../"):
                raise BackupError("That backup contains unsafe paths, so YAMS won't use it.")
            if name == MARKER:
                try:
                    meta = json.loads(tar.extractfile(m).read())
                except Exception:
                    raise BackupError("That backup's description file is damaged.")
                continue
            if not any(name == p or name.startswith(p + "/") for p in INCLUDE):
                raise BackupError("That archive contains files a YAMS backup never has, so YAMS won't use it.")
            if not (m.isfile() or m.isdir()):
                raise BackupError("That backup contains links or special files, so YAMS won't use it.")
    if not isinstance(meta, dict):
        raise BackupError("That isn't a YAMS for Umbrel backup.")
    if int(meta.get("format", 0)) > FORMAT:
        raise BackupError("That backup was made by a newer YAMS. Update YAMS in Umbrel, then try again.")
    meta.setdefault("includes", [p for p in INCLUDE])
    return meta


# --------------------------------------------------------------------------- restoring
def run_restore(path):
    stopped = []
    staged = os.path.join(DATA_DIR, ".restore-new")
    previous = os.path.join(DATA_DIR, ".restore-old")
    swapped = []
    try:
        _step("Checking the backup\u2026")
        meta = inspect_archive(path)
        _step("Pausing apps\u2026")
        stopped = _pause("Paused for restore\u2026", include_vpn=True)
        _step("Saving a safety copy of your current setup\u2026")
        safety = _write_archive("pre-restore")

        _step("Unpacking the backup\u2026")
        for d in (staged, previous):
            shutil.rmtree(d, ignore_errors=True)
        os.makedirs(staged)
        with tarfile.open(path, "r:gz") as tar:
            members = [m for m in tar.getmembers() if posixpath.normpath(m.name) != MARKER]
            tar.extractall(staged, members=members, filter="data")

        _step("Putting the restored settings in place\u2026")
        for rel in INCLUDE:
            src = os.path.join(staged, rel)
            if not os.path.exists(src):
                continue
            dst = os.path.join(DATA_DIR, rel)
            if os.path.exists(dst):
                os.makedirs(os.path.dirname(os.path.join(previous, rel)), exist_ok=True)
                os.replace(dst, os.path.join(previous, rel))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)
            swapped.append(rel)
            _chown_tree(dst)
        shutil.rmtree(previous, ignore_errors=True)
        shutil.rmtree(staged, ignore_errors=True)
    except Exception as err:
        if not isinstance(err, BackupError):
            traceback.print_exc()
        # Put back anything already swapped.
        for rel in swapped:
            dst, old = os.path.join(DATA_DIR, rel), os.path.join(previous, rel)
            if os.path.exists(old):
                shutil.rmtree(dst, ignore_errors=True)
                os.replace(old, dst)
        shutil.rmtree(staged, ignore_errors=True)
        shutil.rmtree(previous, ignore_errors=True)
        if stopped:
            _step("Starting apps again\u2026")
            _resume(stopped)
        _finish(error=str(err) if isinstance(err, BackupError) else f"The restore didn't finish, and nothing was changed: {err}")
        return False

    if hooks["after_restore"]:
        hooks["after_restore"]()
    _step("Starting apps with the restored settings\u2026")
    _resume(stopped)
    created = time.strftime("%d %b %Y, %H:%M", time.localtime(meta.get("created", 0)))
    _finish(message=f"Restored the backup from {created}. A safety copy of the previous setup was saved as {safety}.")
    return True


def start_restore(name):
    path = safe_name(name)
    inspect_archive(path)  # fail fast with a clear message before anything stops
    _begin("restore")
    threading.Thread(target=run_restore, args=(path,), daemon=True).start()


def delete_backup(name):
    if job["active"]:
        raise BackupError("Wait for the current backup or restore to finish.")
    os.remove(safe_name(name))


# --------------------------------------------------------------------------- uploads
def receive_upload(stream, length, filename):
    ensure_backup_dir()
    if length <= 0:
        raise BackupError("That file is empty.")
    free = shutil.disk_usage(BACKUP_DIR).free
    if length + 512 * 1024 * 1024 > free:
        raise BackupError("There isn't enough free space on your Umbrel for that backup.")
    stamp = time.strftime("%Y-%m-%d-%H%M%S")
    part = os.path.join(BACKUP_DIR, f".upload-{stamp}.part")
    remaining = length
    try:
        with open(part, "wb") as fh:
            while remaining > 0:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise BackupError("The upload stopped before the whole file arrived.")
                fh.write(chunk)
                remaining -= len(chunk)
        inspect_archive(part)
        base = os.path.basename(filename or "")
        if NAME_OK.match(base) and base.startswith("yams-umbrel-backup-") and not os.path.exists(os.path.join(BACKUP_DIR, base)):
            name = base
        else:
            name = f"yams-umbrel-backup-{stamp}-uploaded.tar.gz"
        os.replace(part, os.path.join(BACKUP_DIR, name))
        os.chown(os.path.join(BACKUP_DIR, name), UID, GID)
        return name
    finally:
        if os.path.exists(part):
            os.remove(part)


# --------------------------------------------------------------------------- schedule
def due(s, now=None):
    now = now or time.time()
    interval = {"daily": 86400, "weekly": 7 * 86400}.get(s["schedule"])
    if not interval:
        return False
    since = now - s.get("last_auto", 0)
    in_window = 3 <= time.localtime(now).tm_hour < 5
    # Normally run in the quiet 3-5 AM window; if that keeps being missed, run anyway.
    return (since >= interval - 3600 and in_window) or since >= interval + 86400


def scheduler():
    while True:
        time.sleep(600)
        try:
            s = settings()
            if due(s) and not job["active"]:
                _begin("backup")
                s["last_auto"] = int(time.time())
                _write_json(SETTINGS_PATH, s)
                run_backup("auto")
        except BackupError:
            pass
        except Exception:
            traceback.print_exc()
