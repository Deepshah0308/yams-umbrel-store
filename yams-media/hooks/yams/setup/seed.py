"""
YAMS for Umbrel - first-boot seeder.

Runs once per start (as root) before the media apps boot. It never overwrites a
config that already exists; it only fills in what's missing, so re-running is safe.

  /yams  -> ${APP_DATA_DIR}/data
  /data  -> Umbrel's Downloads folder (shared "data" root for hardlinks)
"""
import base64
import hashlib
import json
import os
import re
import secrets

PREFIX = os.environ.get("APP_PREFIX", "yams-media")
ROOT = "/yams"
CFG = f"{ROOT}/config"
STATE = f"{ROOT}/state"
VPN = f"{ROOT}/vpn"
DATA = "/data"
UID = GID = 1000

WORDS = (
    "amber anchor apple arrow aspen atlas bamboo basil beacon birch bison blossom breeze brook "
    "cactus canyon cedar cherry cinder clover cobalt comet coral cotton crane crystal cypress daisy "
    "delta dune eagle ember falcon fern fjord flint forest fox galaxy garnet ginger glacier granite "
    "harbor hazel heron honey indigo iris island ivory jade jasper juniper kelp kiwi lagoon lantern "
    "lemon lilac linen lotus lunar maple marble meadow meteor mint mist moss nectar nova oak ocean "
    "olive onyx orbit orchid otter panda pebble pepper pine planet plum polar poppy prairie quartz "
    "quill rain raven reef river robin ruby saffron sage salmon sand sierra silver slate sparrow "
    "spruce stone summit sunset swift thistle thunder tiger topaz tulip tundra valley velvet violet "
    "walnut willow winter wren yarrow zephyr"
).split()


def friendly_password():
    rng = secrets.SystemRandom()
    return "-".join(rng.choice(WORDS) for _ in range(4)) + "-" + str(rng.randint(10, 99))


def chown_tree(path):
    for dirpath, dirnames, filenames in os.walk(path):
        os.chown(dirpath, UID, GID)
        for name in filenames:
            try:
                os.chown(os.path.join(dirpath, name), UID, GID)
            except FileNotFoundError:
                pass


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    os.chown(path, UID, GID)


def write_file(path, content, mode=0o644):
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(content)
    os.chmod(tmp, mode)
    os.chown(tmp, UID, GID)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- secrets
def load_secrets():
    path = f"{STATE}/secrets.json"
    data = {}
    if os.path.exists(path):
        with open(path) as fh:
            data = json.load(fh)
    changed = False
    defaults = {
        "username": lambda: "admin",
        "password": friendly_password,
        "sonarr_key": lambda: secrets.token_hex(16),
        "radarr_key": lambda: secrets.token_hex(16),
        "prowlarr_key": lambda: secrets.token_hex(16),
        "bazarr_key": lambda: secrets.token_hex(16),
        "gluetun_key": lambda: secrets.token_urlsafe(24),
    }
    for key, make in defaults.items():
        if not data.get(key):
            data[key] = make()
            changed = True
    return data, changed, path


# --------------------------------------------------------------------------- *arr apps
ARR_APPS = {
    "sonarr": ("Sonarr", 8989),
    "radarr": ("Radarr", 7878),
    "prowlarr": ("Prowlarr", 9696),
}


def arr_config(name, title, port, key):
    return f"""<Config>
  <BindAddress>*</BindAddress>
  <Port>{port}</Port>
  <EnableSsl>False</EnableSsl>
  <LaunchBrowser>False</LaunchBrowser>
  <ApiKey>{key}</ApiKey>
  <AuthenticationMethod>External</AuthenticationMethod>
  <AuthenticationRequired>DisabledForLocalAddresses</AuthenticationRequired>
  <LogLevel>info</LogLevel>
  <UrlBase>/{name}</UrlBase>
  <InstanceName>{title}</InstanceName>
  <UpdateMechanism>Docker</UpdateMechanism>
  <AnalyticsEnabled>False</AnalyticsEnabled>
</Config>
"""


def seed_arr(name, secrets_data):
    title, port = ARR_APPS[name]
    path = f"{CFG}/{name}/config.xml"
    key_name = f"{name}_key"
    if not os.path.exists(path):
        write_file(path, arr_config(name, title, port, secrets_data[key_name]))
        print(f"[yams] wrote starter config for {title}")
        return False
    # Existing install: adopt its API key and make sure the URL base matches our router.
    with open(path) as fh:
        xml = fh.read()
    changed = False
    match = re.search(r"<ApiKey>([^<]+)</ApiKey>", xml)
    if match and match.group(1) != secrets_data[key_name]:
        secrets_data[key_name] = match.group(1)
        changed = True
    if re.search(r"<UrlBase>[^<]*</UrlBase>", xml):
        new_xml = re.sub(r"<UrlBase>[^<]*</UrlBase>", f"<UrlBase>/{name}</UrlBase>", xml)
    else:
        new_xml = xml.replace("</Config>", f"  <UrlBase>/{name}</UrlBase>\n</Config>")
    if new_xml != xml:
        write_file(path, new_xml)
        print(f"[yams] fixed URL base for {title}")
    return changed


# --------------------------------------------------------------------------- Bazarr
def seed_bazarr(s):
    path = f"{CFG}/bazarr/config/config.yaml"
    if os.path.exists(path):
        return
    host = lambda svc: f"{PREFIX}_{svc}_1"
    write_file(
        path,
        f"""general:
  base_url: /bazarr
  use_sonarr: true
  use_radarr: true
auth:
  apikey: {s['bazarr_key']}
  type: null
sonarr:
  ip: {host('sonarr')}
  port: 8989
  base_url: /sonarr
  ssl: false
  apikey: {s['sonarr_key']}
radarr:
  ip: {host('radarr')}
  port: 7878
  base_url: /radarr
  ssl: false
  apikey: {s['radarr_key']}
""",
    )
    print("[yams] wrote starter config for Bazarr (already linked to Sonarr and Radarr)")


# --------------------------------------------------------------------------- qBittorrent
def qbit_hash(password):
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, 100000, 64)
    return f"@ByteArray({base64.b64encode(salt).decode()}:{base64.b64encode(derived).decode()})"


def seed_qbittorrent(s):
    path = f"{CFG}/qbittorrent/qBittorrent/qBittorrent.conf"
    if os.path.exists(path):
        return
    write_file(
        path,
        f"""[LegalNotice]
Accepted=true

[BitTorrent]
Session\\DefaultSavePath=/data/torrents
Session\\TempPathEnabled=false
Session\\AddExtensionToIncompleteFiles=true

[Preferences]
Downloads\\SavePath=/data/torrents/
WebUI\\Port=8080
WebUI\\Username={s['username']}
WebUI\\Password_PBKDF2="{qbit_hash(s['password'])}"
WebUI\\LocalHostAuth=false
WebUI\\HostHeaderValidation=false
WebUI\\CSRFProtection=false
General\\Locale=en
""",
    )
    print("[yams] wrote starter config for qBittorrent")


# --------------------------------------------------------------------------- gluetun
def seed_gluetun(s):
    ensure_dir(f"{VPN}/wireguard")
    write_file(
        f"{VPN}/auth/config.toml",
        f"""# Written by YAMS. Lets the dashboard read VPN status and nothing else.
[[roles]]
name = "yams-dashboard"
routes = ["GET /v1/publicip/ip", "GET /v1/vpn/status"]
auth = "apikey"
apikey = "{s['gluetun_key']}"
""",
        mode=0o600,
    )


# --------------------------------------------------------------------------- folders
def seed_folders():
    # TRaSH-guides layout: one /data root so downloads can be hardlinked into the library.
    for sub in ("torrents/tv", "torrents/movies", "media/tv", "media/movies"):
        ensure_dir(f"{DATA}/{sub}")
    ensure_dir(f"{DATA}/torrents")
    ensure_dir(f"{DATA}/media")


def main():
    for d in (CFG, STATE, VPN):
        ensure_dir(d)
    s, changed, secrets_path = load_secrets()
    for name in ARR_APPS:
        changed = seed_arr(name, s) or changed
    seed_bazarr(s)
    seed_qbittorrent(s)
    seed_gluetun(s)
    seed_folders()
    if changed or not os.path.exists(secrets_path):
        write_file(secrets_path, json.dumps(s, indent=2), mode=0o600)
    for sub in ("sonarr", "radarr", "prowlarr", "bazarr", "qbittorrent", "jellyfin"):
        chown_tree(f"{CFG}/{sub}")
    chown_tree(STATE)
    chown_tree(f"{VPN}/wireguard")
    print("[yams] setup files are ready")


if __name__ == "__main__":
    main()
