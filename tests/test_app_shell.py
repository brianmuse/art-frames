"""The app shell: per-TV page URLs, redirects from the older URLs, the sidebar and the TV switcher."""

import re

import pytest
from test_web import PASSWORD, auth

from art_frames.web import create_app


@pytest.fixture
def shell(tmp_path):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    second = app.extensions["displays"].add("Den", "192.168.1.51")
    return app.test_client(), second


def current_item(page):
    """The label of the sidebar item marked as the current page."""
    found = re.findall(r'class="sidebar-menu-button" href="[^"]*" aria-current="page">.*?<span>([^<]+)</span>', page)
    assert len(found) == 1, found
    return found[0]


@pytest.mark.parametrize("path,label", [
    ("/", "Home"), ("/tv/main", "Now"), ("/tv/main/schedule", "Schedule"), ("/tv/main/picture", "Picture"),
    ("/tv/main/health", "Health"), ("/tv/main/settings", "TV settings"), ("/household", "Household settings"),
])
def test_each_page_marks_one_sidebar_item_as_current(shell, path, label):
    client, _ = shell
    response = client.get(path, headers=auth())
    assert response.status_code == 200
    assert current_item(response.text) == label


@pytest.mark.parametrize("page", ["", "/schedule", "/picture", "/display", "/insights", "/settings"])
def test_an_unknown_tv_is_not_found(shell, page):
    client, _ = shell
    assert client.get(f"/tv/nope{page}", headers=auth()).status_code == 404


def test_older_urls_redirect_to_the_new_pages_and_keep_their_query(shell):
    client, second = shell
    cases = {
        f"/?tv={second}": f"/tv/{second}",
        f"/?tv={second}&view=tv&job=abc": f"/tv/{second}?view=tv&job=abc",
        f"/settings?tv={second}": f"/tv/{second}/settings",
        "/settings": "/tv/main/settings",
    }
    for old, new in cases.items():
        response = client.get(old, headers=auth())
        assert response.status_code == 302 and response.location == new, old
    # Insights is now Health: its URLs move for good.
    for old, new in {f"/analytics?tv={second}&days=30": f"/tv/{second}/health?days=30",
                     f"/tv/{second}/insights?days=90": f"/tv/{second}/health?days=90"}.items():
        response = client.get(old, headers=auth())
        assert response.status_code == 301 and response.location == new, old
    assert client.get("/settings?tv=nope", headers=auth()).status_code == 404


def test_tv_pages_link_to_the_same_tv(shell):
    client, second = shell
    page = client.get(f"/tv/{second}", headers=auth()).text
    for name in ("schedule", "picture", "health", "settings"):
        assert f'href="/tv/{second}/{name}"' in page
    assert f'href="/activity?tv={second}"' in page


@pytest.mark.parametrize("path,target", [
    ("/tv/main", "/tv/{second}"),
    ("/tv/main/picture", "/tv/{second}/picture"),
    ("/tv/main/schedule", "/tv/{second}/schedule"),
    ("/tv/main/settings", "/tv/{second}/settings"),
    ("/tv/main/health?days=30", "/tv/{second}/health?days=30"),
    ("/activity?tv=main", "/activity?tv={second}"),
])
def test_the_tv_switcher_keeps_the_current_page(shell, path, target):
    client, second = shell
    page = client.get(path, headers=auth()).text
    switcher = page[page.index("data-tv-switch"):page.index("Add TV")]
    assert f'href="{target.format(second=second)}"' in switcher
    assert 'aria-current="true"' in switcher


def test_home_lists_every_tv_without_a_current_tv_page(shell):
    client, second = shell
    page = client.get("/", headers=auth()).text
    assert 'href="/tv/main"' in page and f'href="/tv/{second}"' in page
    assert current_item(page) == "Home"


def test_picture_puts_mats_first_and_folds_the_tests(shell):
    client, _ = shell
    page = client.get("/tv/main/picture", headers=auth()).text
    titles = re.findall(r'<h2 class="card-title" id="([a-z-]+)-title">', page)
    assert titles[:4] == ["unread", "look", "mats", "art-display"]  # the empty state, before the first read
    tests = page.index('id="tests"')
    assert page.index('id="screen-power"') > tests and page.index('id="standby-test"') > tests


def test_sidebar_group_labels_do_not_repeat_item_labels(shell):
    client, _ = shell
    page = client.get("/tv/main", headers=auth()).text
    groups = re.findall(r'<p class="sidebar-group-label">([^<]+)</p>', page)
    items = re.findall(r'class="sidebar-menu-button"[^>]*>.*?<span>([^<]+)</span>', page)
    assert groups and items and not set(groups) & set(items)
    assert "Home" in items and "Your TVs" not in items  # the same name as the page title and breadcrumb
    assert "Activity" not in items  # the top bar opens it
