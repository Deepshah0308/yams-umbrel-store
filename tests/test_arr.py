"""Sonarr/Radarr/Prowlarr wiring against strict fake apps."""
import pytest

import server
from conftest import KEYS
from fakes import FakeApps


@pytest.fixture
def apps(secrets_file, monkeypatch):
    fake = FakeApps(KEYS).start()
    monkeypatch.setitem(server.ARR["sonarr"], "base", fake.url("sonarr"))
    monkeypatch.setitem(server.ARR["radarr"], "base", fake.url("radarr"))
    monkeypatch.setattr(server, "PROWLARR", fake.url("prowlarr"))
    monkeypatch.setattr(server, "wait_for", lambda *a, **k: True)
    yield fake
    fake.stop()


def fields(entry):
    return {f["name"]: f.get("value") for f in entry["fields"]}


@pytest.mark.parametrize("kind", ["sonarr", "radarr"])
def test_download_client_is_added_with_every_field(apps, kind):
    assert server.wire_arr(kind)
    client = apps.clients[kind][0]
    assert client["name"] == "qBittorrent (VPN)" and client["enable"]
    assert fields(client)["host"] == server.QBIT_HOST and fields(client)["port"] == 8080
    assert apps.roots[kind] == [{"path": server.ARR[kind]["root"]}]
    assert server.is_done(kind)


def test_broken_download_client_is_repaired_not_duplicated(apps):
    server.ensure_download_client("sonarr")
    apps.clients["sonarr"][0]["enable"] = False
    for f in apps.clients["sonarr"][0]["fields"]:
        if f["name"] == "host":
            f["value"] = "localhost"
    assert server.ensure_download_client("sonarr")
    assert len(apps.clients["sonarr"]) == 1
    assert fields(apps.clients["sonarr"][0])["host"] == server.QBIT_HOST


def test_step_is_not_marked_done_when_the_app_is_down(apps, monkeypatch):
    monkeypatch.setitem(server.ARR["radarr"], "base", "http://127.0.0.1:1/radarr")
    assert not server.wire_arr("radarr")
    assert not server.is_done("radarr")


def test_prowlarr_links_both_apps_and_syncs(apps):
    assert server.wire_prowlarr()
    linked = {a["implementation"]: fields(a) for a in apps.apps}
    assert set(linked) == {"Sonarr", "Radarr"}
    assert linked["Radarr"]["baseUrl"] == apps.url("radarr")
    assert linked["Radarr"]["apiKey"] == KEYS["radarr"]
    assert 2000 in linked["Radarr"]["syncCategories"]
    assert ("prowlarr", "ApplicationIndexerSync") in apps.commands


def test_prowlarr_link_that_fails_its_test_is_repaired(apps):
    server.ensure_prowlarr_app("radarr")
    apps.links_ok = False
    assert server.ensure_prowlarr_app("radarr")
    assert len(apps.apps) == 1 and apps.links_ok


def test_fix_buttons(apps, monkeypatch):
    monkeypatch.setattr(server, "status_cache", {"data": {"answering": {"qbittorrent": True}}})
    good, message = server.fix_prowlarr()
    assert good and "Syncing 2 indexers" in message
    good, message = server.fix_qbittorrent()
    assert good and "passed the connection test" in message
    apps.qbit_ok = False
    good, message = server.fix_qbittorrent()
    assert not good and "can't reach qBittorrent" in message
    good, message = server.fix_search()
    assert good and ("sonarr", "MissingEpisodeSearch") in apps.commands and ("radarr", "MissingMoviesSearch") in apps.commands
