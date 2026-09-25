import io
import json
import os
import tarfile
import tempfile

import pytest

import backups as B


@pytest.fixture
def env(monkeypatch):
    data, out = tempfile.mkdtemp(), tempfile.mkdtemp()
    monkeypatch.setattr(B, "DATA_DIR", data)
    monkeypatch.setattr(B, "BACKUP_DIR", out)
    monkeypatch.setattr(B, "SETTINGS_PATH", f"{data}/state/backup-settings.json")
    monkeypatch.setattr(B, "PAUSED_PATH", f"{data}/state/paused-by-backup.json")
    monkeypatch.setattr(B, "UID", os.getuid())
    monkeypatch.setattr(B, "GID", os.getgid())
    calls = []
    monkeypatch.setitem(B.hooks, "stop", lambda reason, vpn=False: calls.append(("stop", vpn)) or ["sonarr"])
    monkeypatch.setitem(B.hooks, "start", lambda services: calls.append(("start", tuple(services))))
    monkeypatch.setitem(B.hooks, "after_restore", lambda: calls.append("after"))
    B.job.update(active=False)

    def write(rel, text):
        path = os.path.join(data, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
    write("config/sonarr/config.xml", "ORIGINAL")
    write("config/sonarr/logs/big.txt", "LOG")
    write("config/jellyfin/cache/x.bin", "CACHE")
    write("state/secrets.json", "{}")
    write("vpn/wireguard/wg0.conf", "[Interface]")
    write("vpn/servers.json", "gluetun data")
    return data, out, write, calls


def test_backup_skips_caches_and_restarts_apps(env):
    data, out, _, calls = env
    B._begin("backup")
    name = B.run_backup("manual")
    with tarfile.open(os.path.join(out, name)) as tar:
        names = set(tar.getnames())
    assert "config/sonarr/config.xml" in names and "vpn/wireguard/wg0.conf" in names
    assert "config/sonarr/logs/big.txt" not in names and "config/jellyfin/cache/x.bin" not in names
    assert "vpn/servers.json" not in names
    assert calls == [("stop", False), ("start", ("sonarr",))]


def test_restore_brings_settings_back_and_keeps_a_safety_copy(env):
    data, out, write, calls = env
    B._begin("backup")
    name = B.run_backup("manual")
    write("config/sonarr/config.xml", "CHANGED")
    B._begin("restore")
    assert B.run_restore(os.path.join(out, name))
    assert open(f"{data}/config/sonarr/config.xml").read() == "ORIGINAL"
    assert any(b["label"] == "pre-restore" for b in B.list_backups())
    assert not [d for d in os.listdir(data) if d.startswith(".restore")]


def _archive(out, name, entries, meta={"format": 1}):
    path = os.path.join(out, name)
    with tarfile.open(path, "w:gz") as tar:
        if meta is not None:
            raw = json.dumps(meta).encode()
            info = tarfile.TarInfo("yams-backup.json")
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
        for entry in entries:
            info = tarfile.TarInfo(entry)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    return name


@pytest.mark.parametrize("entries, meta, message", [
    (["../../etc/cron.d/x"], {"format": 1}, "unsafe paths"),
    (["docker-compose.yml"], {"format": 1}, "never has"),
    (["config/a"], None, "isn't a YAMS"),
    ([], {"format": 99}, "newer YAMS"),
])
def test_bad_archives_are_refused_before_anything_stops(env, entries, meta, message):
    _, out, _, calls = env
    name = _archive(out, "bad.tar.gz", entries, meta)
    with pytest.raises(B.BackupError, match=message):
        B.start_restore(name)
    assert calls == []


def test_automatic_backups_are_pruned(env):
    _, out, _, _ = env
    for i in range(6):
        path = os.path.join(out, f"yams-umbrel-backup-2026-01-0{i + 1}-000000-auto.tar.gz")
        open(path, "w").write("x")
        os.utime(path, (1e9 + i, 1e9 + i))
    B.prune_auto(3)
    assert len([b for b in B.list_backups() if b["label"] == "auto"]) == 3
