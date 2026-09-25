"""App updates against an in-memory Docker Engine."""
import pytest

import server
import updater
from fakes import FakeDocker

OLD_ENV = ["PATH=/usr/bin", "VERSION=1"]
NEW_ENV = ["PATH=/usr/bin", "VERSION=2"]


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker().start()
    monkeypatch.setattr(server, "DOCKER_SOCK", fake.sock)
    backups_taken = []
    monkeypatch.setattr(updater, "hooks", {
        "docker": server.docker, "containers": server.project_containers, "inspect": server.inspect,
        "backup": lambda: backups_taken.append(1) or True, "set_busy": lambda *a: None})
    updater.job.update(active=False, message=None, error=None)
    updater.check_cache.update(data=None, at=0)
    fake.backups_taken = backups_taken
    yield fake
    fake.stop()


def stack(fake, gluetun_update=False):
    labels = {"org.opencontainers.image.version": "4.0.20.2946-ls1"}
    fake.add_image("linuxserver/sonarr:latest", "sha256:s-old", {"Env": OLD_ENV, "Labels": labels, "Cmd": ["/init"]})
    fake.add_image("linuxserver/radarr:latest", "sha256:r-old", {"Env": OLD_ENV, "Labels": {}})
    fake.add_image("qmcgaw/gluetun:v3", "sha256:g-old", {"Env": OLD_ENV, "Labels": {}})
    fake.add_image("linuxserver/qbittorrent:latest", "sha256:q-old", {"Env": OLD_ENV, "Labels": {}})
    fake.add_image("linuxserver/bazarr:1.6.1@sha256:pinned", "sha256:pinned", {"Env": [], "Labels": {}})
    fake.add_container("sonarr", "linuxserver/sonarr:latest", config={"Env": ["PUID=1000"]})
    fake.add_container("radarr", "linuxserver/radarr:latest")
    gluetun = fake.add_container("gluetun", "qmcgaw/gluetun:v3", config={"Entrypoint": ["/bin/sh", "/app/vpn/vpn-wrapper.sh"]})
    fake.add_container("qbittorrent", "linuxserver/qbittorrent:latest",
                       host={"NetworkMode": f"container:{gluetun}"}, networks={})
    fake.add_container("bazarr", "linuxserver/bazarr:1.6.1@sha256:pinned")
    fake.publish("linuxserver/sonarr:latest", "sha256:s-new", {"Env": NEW_ENV, "Labels": {"org.opencontainers.image.version": "4.0.21.3000-ls2"}, "Cmd": ["/init"]})
    if gluetun_update:
        fake.publish("qmcgaw/gluetun:v3", "sha256:g-new", {"Env": NEW_ENV, "Labels": {}})
    return gluetun


def test_check_reports_what_can_be_updated(docker):
    stack(docker)
    result = {r["service"]: r for r in updater.check(force=True)["apps"]}
    assert result["sonarr"]["status"] == "update" and result["sonarr"]["version"] == "4.0.20.2946"
    assert result["radarr"]["status"] == "current"
    assert result["bazarr"]["status"] == "pinned"
    assert updater.check_cache["data"]["available"] == ["sonarr"]


def test_update_recreates_only_changed_apps_keeping_their_settings(docker):
    stack(docker)
    radarr_before = docker.by_service("radarr")["Id"]
    updater._begin()
    updater.run_update()
    assert updater.job["error"] is None, updater.job["error"]
    assert docker.backups_taken == [1]
    sonarr = docker.by_service("sonarr")
    assert sonarr["Name"] == "/yams-media_sonarr_1" and sonarr["State"]["Running"]
    assert docker.images[sonarr["Image"]]["RepoDigests"][0].endswith("s-new")
    env = sonarr["Config"]["Env"]
    assert "PUID=1000" in env and "VERSION=2" in env and "VERSION=1" not in env   # new image defaults win
    assert sonarr["Config"]["Labels"]["com.docker.compose.service"] == "sonarr"
    assert sonarr["HostConfig"]["Binds"] == ["/data/sonarr:/config"]
    aliases = sonarr["NetworkSettings"]["Networks"]["umbrel_main_network"]["Aliases"]
    assert "sonarr" in aliases and "yams-media_sonarr_1" in aliases
    assert docker.by_service("radarr")["Id"] == radarr_before                       # untouched
    assert not [c for c in docker.containers.values() if c["Name"].endswith("-yams-old")]


def test_new_vpn_container_means_qbittorrent_is_rebuilt_inside_it(docker):
    stack(docker, gluetun_update=True)
    updater._begin()
    updater.run_update(services=["gluetun"])
    assert updater.job["error"] is None, updater.job["error"]
    gluetun, qbit = docker.by_service("gluetun"), docker.by_service("qbittorrent")
    assert qbit["HostConfig"]["NetworkMode"] == f"container:{gluetun['Id']}"
    assert gluetun["Config"]["Entrypoint"] == ["/bin/sh", "/app/vpn/vpn-wrapper.sh"]
    assert "Hostname" not in qbit["CreateBody"]
    assert qbit["State"]["Running"]


def test_rolls_back_when_the_new_container_will_not_start(docker):
    stack(docker)
    old = docker.by_service("sonarr")
    old_id = old["Id"]
    docker.publish("linuxserver/sonarr:latest", "sha256:s-broken", {"Env": NEW_ENV, "Labels": {}})
    real_add = docker.add_image

    def add_and_break(ref, digest, config):
        image_id = real_add(ref, digest, config)
        docker.fail_start.add(image_id)
        return image_id
    docker.add_image = add_and_break
    updater._begin()
    updater.run_update(services=["sonarr"])
    assert "wouldn't start" in updater.job["error"]
    sonarr = docker.by_service("sonarr")
    assert sonarr["Id"] == old_id and sonarr["Name"] == "/yams-media_sonarr_1" and sonarr["State"]["Running"]
    assert len([c for c in docker.containers.values() if "sonarr" in c["Name"]]) == 1


def test_failed_download_changes_nothing(docker):
    stack(docker)
    before = {c["Id"] for c in docker.containers.values()}
    docker.pull_error = "toomanyrequests"
    updater._begin()
    updater.run_update()
    assert "toomanyrequests" in updater.job["error"]
    assert {c["Id"] for c in docker.containers.values()} == before


def test_split_ref():
    assert updater.split_ref("linuxserver/sonarr:latest") == ("linuxserver/sonarr", "latest")
    assert updater.split_ref("qmcgaw/gluetun") == ("qmcgaw/gluetun", "latest")
    assert updater.split_ref("registry:5000/app:1.2@sha256:abc") == ("registry:5000/app", "1.2")


def test_weekly_schedule():
    import time
    at5 = time.mktime(time.strptime("2026-10-04 05:10", "%Y-%m-%d %H:%M"))
    at13 = time.mktime(time.strptime("2026-10-04 13:10", "%Y-%m-%d %H:%M"))
    assert updater.due({"auto": "weekly", "last_auto": 0}, at5)
    assert not updater.due({"auto": "weekly", "last_auto": at5 - 86400}, at13)
    assert not updater.due({"auto": "off", "last_auto": 0}, at5)
