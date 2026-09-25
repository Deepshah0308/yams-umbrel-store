"""Shared test setup: point the dashboard modules at temporary folders before import."""
import json
import os
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "yams-media", "hooks", "yams", "dashboard"))
sys.path.insert(0, HERE)

_state = tempfile.mkdtemp()
os.environ.setdefault("STATE_DIR", _state)
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("BACKUP_DIR", tempfile.mkdtemp())
os.environ.setdefault("DOCKER_SOCK", os.path.join(tempfile.mkdtemp(), "none.sock"))

KEYS = {"sonarr": "sonarr-key", "radarr": "radarr-key", "prowlarr": "prowlarr-key"}


@pytest.fixture
def secrets_file():
    path = os.path.join(os.environ["STATE_DIR"], "secrets.json")
    with open(path, "w") as fh:
        json.dump({"username": "admin", "password": "not-a-real-password",
                   "sonarr_key": KEYS["sonarr"], "radarr_key": KEYS["radarr"],
                   "prowlarr_key": KEYS["prowlarr"], "gluetun_key": "g"}, fh)
    with open(os.path.join(os.environ["STATE_DIR"], "setup.json"), "w") as fh:
        json.dump({"done": {}}, fh)
    return path
