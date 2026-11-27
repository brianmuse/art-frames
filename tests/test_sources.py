import json
from urllib.parse import parse_qs, urlsplit

import pytest

from art_frames import sources
from art_frames.network import FetchError


def cleveland(**changes):
    return {
        "id": 123,
        "accession_number": "1960.81",
        "title": "Water Lilies",
        "creators": [{"description": "Claude Monet"}],
        "creation_date": "1915–26",
        "share_license_status": "CC0",
        "images": {
            "web": {"url": "https://openaccess-cdn.clevelandart.org/1960.81/web.jpg"},
            "print": {"url": "https://openaccess-cdn.clevelandart.org/1960.81/print.jpg"},
        },
        **changes,
    }


def test_cleveland_artist_search_normalizes_results_without_detail_fanout(monkeypatch):
    calls = []

    def get(url, *, hosts):
        calls.append((url, hosts))
        return {"info": {"total": 13}, "data": [cleveland()]}

    monkeypatch.setattr(sources, "get_json", get)
    page = sources.search("cleveland", "Claude Monet", artist=True)
    assert len(calls) == 1 and calls[0][1] == sources.CMA_HOSTS
    params = parse_qs(urlsplit(calls[0][0]).query)
    assert params["artists"] == ["Claude Monet"] and "q" not in params
    assert params["cc0"] == ["1"] and params["has_image"] == ["1"]
    assert page.has_more and page.items[0]["artist"] == "Claude Monet"
    assert page.items[0]["source_url"] == "https://www.clevelandart.org/art/1960.81"


def test_cleveland_general_query_pagination_and_missing_images(monkeypatch):
    calls = []

    def get(url, *, hosts):
        calls.append(url)
        return {"info": {"total": 13}, "data": [cleveland(), cleveland(images={})]}

    monkeypatch.setattr(sources, "get_json", get)
    page = sources.search("cleveland", "flowers", 2)
    params = parse_qs(urlsplit(calls[0]).query)
    assert params["q"] == ["flowers"] and params["skip"] == ["12"]
    assert not page.has_more and len(page.items) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"share_license_status": "Copyrighted"},
        {"images": {"web": {"url": "https://127.0.0.1/private"}}},
        {"images": {"web": {"url": "https://attacker.example/file.jpg"}}},
        {"images": {"web": {"url": "https://user:pass@openaccess-cdn.clevelandart.org/file.jpg"}}},
    ],
)
def test_import_revalidates_museum_rights_and_image_host(monkeypatch, library, changes):
    monkeypatch.setattr(sources, "get_json", lambda *args, **kwargs: {"data": cleveland(**changes)})
    monkeypatch.setattr(sources, "fetch", lambda *args, **kwargs: pytest.fail("Unapproved image downloaded"))
    with pytest.raises(ValueError):
        sources.import_artwork(library, "cleveland", 123)
    assert not library.all()


def test_cleveland_import_fetches_fresh_record_and_preserves_metadata(monkeypatch, library, image_bytes):
    calls = []

    def get(url, *, hosts):
        calls.append(url)
        return {"data": cleveland()}

    def download(url, *, hosts):
        assert url.endswith("/print.jpg") and hosts == sources.CMA_HOSTS
        return image_bytes

    monkeypatch.setattr(sources, "get_json", get)
    monkeypatch.setattr(sources, "fetch", download)
    art_id = sources.import_artwork(library, "cleveland", "123")
    assert calls == [sources.CMA_API + "/123"]
    item = library.get(art_id)
    assert item["source"] == "cleveland" and item["selected"]
    assert "CC0" in item["rights"] and item["title"] == "Water Lilies"


@pytest.mark.parametrize("source", ["met", "artic"])
def test_artist_filters_and_pagination_contracts(monkeypatch, source):
    calls = []

    def get(url, *, hosts):
        calls.append(url)
        return {"objectIDs": [1], "total": 13, "data": [{"id": 1}], "pagination": {"total_pages": 2}}

    monkeypatch.setattr(sources, "get_json", get)
    monkeypatch.setattr(sources, "artwork", lambda *args: {"title": "Monet"})
    page = sources.search(source, "Monet", artist=True)
    params = parse_qs(urlsplit(calls[0]).query)
    if source == "met":
        assert params["artistOrCulture"] == ["true"]
        assert "isPublicDomain" not in params
    else:
        query = json.loads(params["query"][0])
        assert query["bool"]["must"] == [{"match": {"artist_title": "Monet"}}]
        assert query["bool"]["filter"] == [{"term": {"is_public_domain": True}}]
    assert page.items == [{"title": "Monet"}] and page.has_more


def test_filtered_records_are_distinct_from_unavailable_metadata(monkeypatch):
    monkeypatch.setattr(sources, "get_json", lambda *args, **kwargs: {"objectIDs": [1], "total": 14})

    def unavailable(*args):
        raise FetchError("Source returned HTTP 403", status=403)

    monkeypatch.setattr(sources, "artwork", unavailable)
    with pytest.raises(FetchError, match="403"):
        sources.search("met", "Monet")

    def no_image(*args):
        raise ValueError("No public image")

    monkeypatch.setattr(sources, "artwork", no_image)
    page = sources.search("met", "Monet")
    assert page.items == [] and page.has_more
