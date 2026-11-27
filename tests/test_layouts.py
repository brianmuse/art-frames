import json

import pytest
from test_web import PASSWORD, auth, csrf

from art_frames.apps import App
from art_frames.config import ConfigStore
from art_frames.layouts import LayoutStore, Manifest, load_manifest
from art_frames.web import create_app


def manifest_file(tmp_path, **extra):
    manifest = {
        "id": "example",
        "name": "An independent app",
        "settings": [
            {"key": "location", "label": "Location", "type": "text"},
            {"key": "private_feed", "label": "Private feed", "type": "secret"},
        ],
        **extra,
    }
    path = tmp_path / "layout.json"
    path.write_text(json.dumps(manifest))
    return path


def store_for(tmp_path, path, scope="example"):
    """The settings store of an installed app with this manifest."""
    return LayoutStore(tmp_path / "data", App("example", scope, path, None, load_manifest(path)))


def test_site_secrets_are_private_masked_and_never_part_of_core_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYFRAME_LAYOUT_MANIFEST", str(manifest_file(tmp_path)))
    monkeypatch.setenv("DAYFRAME_DEFAULT_SITE_SOCKET", "website.sock")
    monkeypatch.setenv("DAYFRAME_LAYOUT_ID", "example")
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    client = app.test_client()
    store = store_for(tmp_path, manifest_file(tmp_path))
    secret = "SYNTHETIC-PRIVATE-FEED"
    assert client.post("/site/settings", headers=auth()).status_code == 400
    assert client.post("/site/settings").status_code == 401
    response = client.post(
        "/site/settings",
        headers=auth(),
        data={
            "csrf": csrf(client),
            "manifest": store.fingerprint(store.manifest()),
            "location": "A place",
            "private_feed": secret,
        },
    )
    assert response.status_code == 302
    path = store.path(store.manifest())
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    for route in ("/site/settings", "/", "/tv/main", "/tv/main/display", "/tv/main/settings", "/screen/preview-data"):
        page = client.get(route, headers=auth(), follow_redirects=True)
        assert secret.encode() not in page.data
    page = client.get("/site/settings", headers=auth(), follow_redirects=True)
    assert b"Art Frames keeps it private" in page.data and b"A place" in page.data
    # Blank input preserves the secret, while explicit clearing removes it.
    store.update(
        {"manifest": store.fingerprint(store.manifest()), "private_feed": "", "location": "Another place"}
    )
    assert store.values(store.manifest())["private_feed"] == secret
    store.update({"manifest": store.fingerprint(store.manifest()), "clear_private_feed": "on"})
    assert store.values(store.manifest())["private_feed"] == ""


def test_old_site_form_cannot_write_secrets_into_a_replacement_site(tmp_path):
    path = manifest_file(tmp_path)
    store = store_for(tmp_path, path)
    prior = store.fingerprint(store.manifest())
    path.write_text(json.dumps({"id": "different", "name": "Replacement", "settings": []}))
    with pytest.raises(ValueError, match="Site changed|does not match"):
        store.update({"manifest": prior, "private_feed": "SHOULD-NOT-BE-SAVED"})
    assert not store.root.exists()


def test_old_form_cannot_write_to_an_identical_manifest_from_another_source(tmp_path):
    path = manifest_file(tmp_path)
    token = store_for(tmp_path, path, "example-first-source").fingerprint(load_manifest(path))
    store = store_for(tmp_path, path, "example-replacement-source")
    with pytest.raises(ValueError, match="Site changed"):
        store.update({"manifest": token, "private_feed": "DO-NOT-SEND-TO-REPLACEMENT"})
    assert not store.root.exists()


@pytest.mark.parametrize(
    "change",
    [
        {"id": "../outside"},
        {"entry": "../../private.html"},
        {"settings": [{"key": "secret", "label": "Bad", "type": "secret", "default": "credentials"}]},
        {
            "settings": [
                {"key": "a", "label": "A", "type": "text"},
                {"key": "a", "label": "Again", "type": "text"},
            ]
        },
    ],
)
def test_bad_manifests_are_rejected(change):
    with pytest.raises(ValueError):
        Manifest.model_validate({"id": "safe", "name": "Safe", **change})


@pytest.mark.parametrize("key", ["csrf", "manifest", "clear_private_feed"])
def test_manifest_fields_cannot_collide_with_secret_clear_controls(key):
    with pytest.raises(ValueError, match="host form metadata"):
        Manifest.model_validate(
            {
                "id": "safe",
                "name": "Safe",
                "settings": [{"key": key, "label": "Unsafe collision", "type": "text"}],
            }
        )


def test_legacy_dashboard_configuration_is_preserved_before_core_only_save(tmp_path):
    secret = "SYNTHETIC-CALENDAR-SECRET"
    original = {
        "timezone": "America/New_York",
        "weather": {"name": "A place"},
        "calendars": [{"name": "Work", "url": secret}],
        "planner": {"start": "07:00"},
        "site": {"entry": "dashboard/index.html", "providers": ["planner", "art"]},
        "tv": {"ip": "192.168.1.50"},
    }
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(original))
    store = ConfigStore(tmp_path)
    cfg = store.load()
    assert cfg.tv.ip == "192.168.1.50" and cfg.site.providers == ["art"]
    archive = tmp_path / "legacy-dashboard.json"
    assert json.loads(archive.read_bytes())["calendars"][0]["url"] == secret
    assert archive.stat().st_mode & 0o777 == 0o600
    store.save(cfg)
    assert secret not in path.read_text()
    assert secret in archive.read_text()
    assert not hasattr(store.load(), "weather")


def list_store(tmp_path):
    path = manifest_file(
        tmp_path,
        settings=[
            {
                "key": "feeds",
                "label": "Feeds",
                "type": "list",
                "item_label": "Feed",
                "max_items": 3,
                "fields": [
                    {"key": "title", "label": "Title", "type": "text"},
                    {"key": "url", "label": "Private link", "type": "secret"},
                    {"key": "color", "label": "Color", "type": "color", "default": "#af2842"},
                ],
                "legacy_items": [
                    {
                        "id": "legacy",
                        "values": {"title": "Existing", "color": "#8850a8"},
                        "copy_from": {"url": "old_private_url"},
                    }
                ],
            }
        ],
    )
    return store_for(tmp_path, path)


def test_repeatable_settings_migrate_without_disclosing_or_losing_secrets(tmp_path):
    from werkzeug.datastructures import MultiDict

    from art_frames.config import private_write

    store = list_store(tmp_path)
    manifest = store.manifest()
    path = store.path(manifest)
    private_write(path, json.dumps({"old_private_url": "PRIVATE-LEGACY"}).encode())
    public, configured = store.public_values(manifest)
    assert public == {"feeds": [{"id": "legacy", "title": "Existing", "color": "#8850a8"}]}
    assert configured == {"list.feeds.legacy.url"}
    form = MultiDict(
        {
            "manifest": store.fingerprint(manifest),
            "list.feeds.legacy.title": "Renamed",
            "list.feeds.legacy.url": "",
            "list.feeds.legacy.color": "#af2842",
        }
    )
    form.setlist("list.feeds.rows", ["legacy", "new"])
    form.update(
        {"list.feeds.new.title": "", "list.feeds.new.url": "PRIVATE-NEW", "list.feeds.new.color": "#123456"}
    )
    store.update(form)
    saved = store.values(manifest)
    assert "old_private_url" not in saved
    assert saved["feeds"][0]["url"] == "PRIVATE-LEGACY"
    assert saved["feeds"][1]["url"] == "PRIVATE-NEW"
    assert "PRIVATE" not in json.dumps(store.public_values(manifest)[0])
    assert path.stat().st_mode & 0o777 == 0o600
    form.setlist("list.feeds.rows", ["new"])
    form["list.feeds.new.url"] = ""
    form["clear_list.feeds.new.url"] = "on"
    store.update(form)
    assert store.values(manifest)["feeds"] == [{"id": "new", "title": "", "url": "", "color": "#123456"}]
    form.setlist("list.feeds.rows", [])
    store.update(form)
    assert store.public_values(manifest)[0] == {"feeds": []}


def test_repeatable_form_masks_links_and_renders_generic_controls(tmp_path, monkeypatch):
    from art_frames.config import private_write

    store = list_store(tmp_path)
    private_write(
        store.path(store.manifest()), json.dumps({"old_private_url": "PRIVATE-NEVER-RENDER"}).encode()
    )
    monkeypatch.setenv("DAYFRAME_LAYOUT_MANIFEST", str(store.app.manifest_path))
    monkeypatch.setenv("DAYFRAME_DEFAULT_SITE_SOCKET", "website.sock")
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    client = app.test_client()
    page = client.get("/site/settings", headers=auth(), follow_redirects=True)
    assert page.status_code == 200
    assert b"PRIVATE-NEVER-RENDER" not in page.data
    for expected in [
        b"Existing",
        b"Art Frames keeps it private",
        b"Add feed",
        b"Remove feed",
        b'type="color"',
        b"data-list-template",
    ]:
        assert expected in page.data


@pytest.mark.parametrize(
    "ids,color",
    [
        (["same", "same"], "#123456"),
        (["../bad"], "#123456"),
        (["a", "b", "c", "d"], "#123456"),
        (["a"], "red; background:url(https://example.com)"),
    ],
)
def test_repeatable_form_rejects_invalid_rows_and_colors_atomically(tmp_path, ids, color):
    from werkzeug.datastructures import MultiDict

    store = list_store(tmp_path)
    manifest = store.manifest()
    form = MultiDict({"manifest": store.fingerprint(manifest), "list.feeds.a.color": color})
    form.setlist("list.feeds.rows", ids)
    with pytest.raises(ValueError):
        store.update(form)
    assert not store.path(manifest).exists()


def test_list_schema_cannot_migrate_a_secret_into_a_public_title(tmp_path):
    store = list_store(tmp_path)
    manifest = json.loads(store.app.manifest_path.read_text())
    manifest["settings"][0]["legacy_items"][0]["copy_from"] = {"title": "old_private_url"}
    with pytest.raises(ValueError, match="only migrate into secret"):
        Manifest.model_validate(manifest)
