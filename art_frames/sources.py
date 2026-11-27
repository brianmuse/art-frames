"""Small museum adapters: browse remotely, import only deliberately selected images."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit

from .network import FetchError, fetch, get_json

MET_API = "https://collectionapi.metmuseum.org/public/collection"
MET_HOSTS = {"collectionapi.metmuseum.org", "images.metmuseum.org"}
AIC_API = "https://api.artic.edu/api/v1"
AIC_HOSTS = {"api.artic.edu", "www.artic.edu"}
CMA_API = "https://openaccess-api.clevelandart.org/api/artworks"
CMA_HOSTS = {"openaccess-api.clevelandart.org", "openaccess-cdn.clevelandart.org"}
PROVIDERS = {
    "cleveland": "Cleveland Museum of Art",
    "met": "The Metropolitan Museum of Art",
    "artic": "Art Institute of Chicago",
}
COLLECTIONS = {
    "cleveland": "https://www.clevelandart.org/art/collection",
    "met": "https://www.metmuseum.org/art/collection",
    "artic": "https://www.artic.edu/collection",
}


@dataclass
class SearchPage:
    items: list[dict]
    has_more: bool = False


def _cleveland(data):
    images = data.get("images") or {}
    image = (images.get("print") or images.get("web") or {}).get("url", "")
    thumb = (images.get("web") or {}).get("url", "") or image
    for url in (image, thumb):
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname != "openaccess-cdn.clevelandart.org"
            or parts.port not in (None, 443)
            or parts.username
            or parts.password
        ):
            raise ValueError("This artwork has no approved museum image available")
    if data.get("share_license_status") != "CC0":
        raise ValueError("This artwork has no public-domain image available")
    return {
        "source": "cleveland",
        "source_id": str(_id(data["id"])),
        "title": data["title"],
        "artist": "; ".join(creator.get("description", "") for creator in data.get("creators", [])),
        "year": data.get("creation_date", ""),
        "image": image,
        "thumb": thumb,
        "source_url": "https://www.clevelandart.org/art/" + quote(str(data["accession_number"]), safe=""),
        "rights": "Public domain · Cleveland Museum of Art (CC0)",
    }


def _id(value):
    result = int(value)
    if result <= 0:
        raise ValueError("Invalid museum object ID")
    return result


def artwork(source: str, object_id):
    object_id = _id(object_id)
    if source == "cleveland":
        return _cleveland(get_json(f"{CMA_API}/{object_id}", hosts=CMA_HOSTS)["data"])
    if source == "met":
        data = get_json(f"{MET_API}/v1/objects/{object_id}", hosts=MET_HOSTS)
        if not data.get("isPublicDomain") or not data.get("primaryImage"):
            raise ValueError("This artwork has no public-domain image available")
        return {
            "source": source,
            "source_id": str(object_id),
            "title": data["title"],
            "artist": data.get("artistDisplayName", ""),
            "year": data.get("objectDate", ""),
            "image": data["primaryImage"],
            "thumb": data.get("primaryImageSmall") or data["primaryImage"],
            "source_url": f"https://www.metmuseum.org/art/collection/search/{object_id}",
            "rights": "Public domain · The Met Open Access (CC0)",
        }
    if source == "artic":
        data = get_json(f"{AIC_API}/artworks/{object_id}", hosts=AIC_HOSTS)["data"]
        if not data.get("is_public_domain") or not data.get("image_id"):
            raise ValueError("This artwork has no public-domain image available")
        image_id = data["image_id"]
        # The documented IIIF image URL is constructed locally, not accepted from a browser.
        base = f"https://www.artic.edu/iiif/2/{image_id}/full/"
        return {
            "source": source,
            "source_id": str(object_id),
            "title": data["title"],
            "artist": data.get("artist_title") or "Unknown artist",
            "year": data.get("date_display", ""),
            "image": base + "!3840,3840/0/default.jpg",
            "thumb": base + "!640,640/0/default.jpg",
            "source_url": f"https://www.artic.edu/artworks/{object_id}",
            "rights": "Public domain · Art Institute of Chicago (CC0)",
        }
    raise ValueError("Unknown museum")


def search(source: str, query: str, page: int = 1, *, artist=False):
    query = query.strip()[:120]
    page = max(1, min(page, 100))
    if not query:
        return SearchPage([])
    if source == "cleveland":
        params = urlencode(
            {
                "artists" if artist else "q": query,
                "cc0": 1,
                "has_image": 1,
                "limit": 12,
                "skip": (page - 1) * 12,
                "fields": "id,accession_number,title,creators,creation_date,share_license_status,images",
            }
        )
        raw = get_json(f"{CMA_API}/?{params}", hosts=CMA_HOSTS)
        rows = raw.get("data", [])
        results = []
        for row in rows[:12]:
            try:
                results.append(_cleveland(row))
            except (ValueError, KeyError, TypeError):
                continue
        return SearchPage(results, page * 12 < int(raw.get("info", {}).get("total", 0)))
    if source == "met":
        # v1 search retired in October 2026; v1.1 supplies bounded pagination.
        params = urlencode(
            {
                "q": query,
                "hasImages": "true",
                "artistOrCulture": "true" if artist else "false",
                "limit": 12,
                "offset": (page - 1) * 12,
            }
        )
        raw = get_json(f"{MET_API}/v1.1/search?{params}", hosts=MET_HOSTS)
        ids = (raw.get("objectIDs") or [])[:12]
        has_more = page * 12 < int(raw.get("total", 0))
    elif source == "artic":
        parameters = {"limit": 12, "page": page, "fields": "id"}
        if artist:
            parameters["query"] = json.dumps(
                {
                    "bool": {
                        "must": [{"match": {"artist_title": query}}],
                        "filter": [{"term": {"is_public_domain": True}}],
                    }
                }
            )
        else:
            parameters["q"] = query
            parameters["query[term][is_public_domain]"] = "true"
        params = urlencode(parameters)
        raw = get_json(f"{AIC_API}/artworks/search?{params}", hosts=AIC_HOSTS)
        ids = [row["id"] for row in raw.get("data", [])][:12]
        has_more = page < int(raw.get("pagination", {}).get("total_pages", page))
    else:
        raise ValueError("Unknown museum")

    def candidate(object_id):
        try:
            return artwork(source, object_id), None
        except FetchError as error:
            return None, error
        except OSError:
            return None, FetchError("Museum artwork records could not be reached")
        except ValueError:
            return None, None

    with ThreadPoolExecutor(max_workers=3) as pool:
        resolved = list(pool.map(candidate, ids))
    items = [item for item, _ in resolved if item]
    if not items:
        error = next((error for _, error in resolved if error), None)
        if error:
            raise error
    return SearchPage(items, has_more)


def import_artwork(library, source: str, object_id):
    item = artwork(source, object_id)  # revalidate source metadata and rights server-side
    hosts = {"met": MET_HOSTS, "artic": AIC_HOSTS, "cleveland": CMA_HOSTS}[source]
    image = fetch(item.pop("image"), hosts=hosts)
    item.pop("thumb", None)
    return library.add(image, **item)
