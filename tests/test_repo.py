"""Checks on the package itself, so a release can't go out half-finished."""
import glob
import re
import os
import py_compile
import subprocess

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "yams-media")
HOOKS = os.path.join(APP, "hooks")
HOOK_NAMES = {"pre-install", "post-install", "pre-start", "post-start", "pre-stop", "post-stop",
              "pre-update", "post-update", "pre-uninstall", "post-uninstall"}


def load(path):
    with open(path) as fh:
        return yaml.safe_load(fh)


def test_store_and_app_ids_match():
    store = load(os.path.join(ROOT, "umbrel-app-store.yml"))
    manifest = load(os.path.join(APP, "umbrel-app.yml"))
    assert manifest["id"].startswith(store["id"] + "-")


def test_versions_match():
    manifest = load(os.path.join(APP, "umbrel-app.yml"))
    compose = load(os.path.join(APP, "docker-compose.yml"))
    assert compose["services"]["dashboard"]["environment"]["YAMS_VERSION"] == manifest["version"], \
        "Bump version in umbrel-app.yml and YAMS_VERSION in docker-compose.yml together"


def test_widget_endpoints_point_at_real_services():
    manifest = load(os.path.join(APP, "umbrel-app.yml"))
    services = load(os.path.join(APP, "docker-compose.yml"))["services"]
    for widget in manifest.get("widgets", []):
        assert widget["endpoint"].split(":")[0] in services


def test_media_apps_use_moving_tags():
    services = load(os.path.join(APP, "docker-compose.yml"))["services"]
    for name in ("jellyfin", "sonarr", "radarr", "prowlarr", "bazarr", "qbittorrent", "flaresolverr", "gluetun"):
        assert "@sha256:" not in services[name]["image"], f"{name} is pinned, so Update apps can't update it"


def test_qbittorrent_never_mounts_over_app():
    qbit = load(os.path.join(APP, "docker-compose.yml"))["services"]["qbittorrent"]
    assert not any(v.split(":")[1] == "/app" for v in qbit["volumes"]), "the qBittorrent image keeps its program in /app"
    assert qbit["network_mode"] == "service:gluetun", "qBittorrent must only ever use the VPN's network"


def test_nothing_in_hooks_is_named_like_a_hook():
    for path in glob.glob(os.path.join(HOOKS, "**", "*"), recursive=True):
        assert os.path.basename(path) not in HOOK_NAMES, f"umbrelOS would execute {path}"


def test_python_compiles():
    for path in glob.glob(os.path.join(HOOKS, "**", "*.py"), recursive=True):
        py_compile.compile(path, doraise=True)


def test_shell_scripts_parse():
    for path in glob.glob(os.path.join(HOOKS, "**", "*.sh"), recursive=True):
        subprocess.run(["sh", "-n", path], check=True)


def test_media_apps_are_not_on_the_shared_umbrel_network():
    services = load(os.path.join(APP, "docker-compose.yml"))["services"]
    for name in ("jellyfin", "sonarr", "radarr", "prowlarr", "bazarr", "flaresolverr", "gluetun", "init"):
        assert services[name].get("networks") == ["yams"], f"{name} must only be on the private yams network"
    assert set(services["web"]["networks"]) == {"default", "yams"}
    assert set(services["dashboard"]["networks"]) == {"default", "yams"}


def test_router_reaches_the_dashboard_only_through_its_socket():
    with open(os.path.join(HOOKS, "yams", "Caddyfile")) as fh:
        caddy = fh.read()
    assert "unix//sock/dashboard.sock" in caddy
    assert "dashboard_1:8000" not in caddy


# Things that must never be committed: YAMS-style passwords (four words and a number),
# WireGuard private keys, and 32-character API keys.
SECRET_PATTERNS = [
    re.compile(r"\b[a-z]{3,10}(?:-[a-z]{3,10}){3}-\d{2}\b"),
    re.compile(r"PrivateKey\s*=\s*[A-Za-z0-9+/]{42,43}="),
    re.compile(r"(?i)(api[_-]?key|apikey)[\"'>:= ]+[0-9a-f]{32}\b"),
]


def test_no_secrets_in_the_repository():
    found = []
    for path in glob.glob(os.path.join(ROOT, "**", "*"), recursive=True):
        if not os.path.isfile(path) or "/.git/" in path or "__pycache__" in path:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                found.append(f"{os.path.relpath(path, ROOT)}: {match.group(0)[:12]}…")
    assert not found, "Possible secrets committed:\n" + "\n".join(found)
