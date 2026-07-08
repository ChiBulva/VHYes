import re

import requests


class BarcodeLookupError(Exception):
    pass


class BarcodeRateLimitError(BarcodeLookupError):
    pass


TMDB_BASE_URL = "https://api.themoviedb.org/3"
BARCODE_LOOKUP_URL = "https://api.barcodelookup.com/v3/products"
UPCITEMDB_URL = "https://api.upcitemdb.com/prod/trial/lookup"
WIKIDATA_SEARCH_URL = "https://www.wikidata.org/w/api.php"
OPEN_LIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"

PHYSICAL_MEDIA_TERMS = (
    "media",
    "dvd",
    "video",
    "vhs",
    "blu-ray",
    "bluray",
    "4k",
    "ultra hd",
    "uhd",
    "laserdisc",
    "book",
    "books",
    "magazine",
    "magazines",
    "comic",
    "manga",
    "graphic novel",
    "music",
    "cd",
    "compact disc",
    "vinyl",
    "cassette",
    "record",
)

NON_MEDIA_TERMS = (
    "toploader",
    "top loader",
    "sleeve",
    "protector",
    "storage",
    "toy",
    "toys & games",
    "card games",
    "trading card",
    "accessory",
    "supplies",
)


def search_tmdb(query, api_key):
    if not api_key or not query:
        return []

    response = requests.get(
        f"{TMDB_BASE_URL}/search/movie",
        params={"api_key": api_key, "query": query, "include_adult": "false"},
        timeout=10,
    )
    response.raise_for_status()

    results = []
    for item in response.json().get("results", [])[:10]:
        results.append(
            {
                "title": item.get("title") or item.get("original_title"),
                "media_kind": "movie",
                "release_year": _year_from_date(item.get("release_date")),
                "summary": item.get("overview"),
                "rating": item.get("vote_average"),
                "barcode": "",
                "source_name": "tmdb",
                "source_id": str(item.get("id")),
                "source_url": f"https://www.themoviedb.org/movie/{item.get('id')}",
                "remote_url": _tmdb_image_url(item.get("poster_path")),
                "confidence": item.get("popularity"),
                "raw_payload": item,
            }
        )
    return results


def search_wikidata(query):
    if not query:
        return []

    response = requests.get(
        WIKIDATA_SEARCH_URL,
        params={
            "action": "wbsearchentities",
            "format": "json",
            "language": "en",
            "uselang": "en",
            "type": "item",
            "limit": 10,
            "search": query,
        },
        headers={"User-Agent": "VHYes local media catalog"},
        timeout=10,
    )
    response.raise_for_status()

    results = []
    for item in response.json().get("search", []):
        description = item.get("description") or ""
        haystack = f"{item.get('label', '')} {description}".lower()
        if not any(word in haystack for word in ("film", "movie", "television", "series", "video")):
            continue
        results.append(
            {
                "title": item.get("label"),
                "media_kind": "movie",
                "release_year": _year_from_text(description),
                "summary": description,
                "rating": "",
                "barcode": "",
                "source_name": "wikidata",
                "source_id": item.get("id"),
                "source_url": f"https://www.wikidata.org/wiki/{item.get('id')}",
                "remote_url": "",
                "confidence": None,
                "raw_payload": item,
            }
        )
    return results


def search_open_library(query):
    if not query:
        return []

    response = requests.get(
        OPEN_LIBRARY_SEARCH_URL,
        params={"q": query, "limit": 12},
        headers={"User-Agent": "VHYes local media catalog"},
        timeout=10,
    )
    response.raise_for_status()

    results = []
    for item in response.json().get("docs", [])[:12]:
        title = item.get("title")
        if not title:
            continue

        authors = item.get("author_name") or []
        publish_year = item.get("first_publish_year")
        cover_id = item.get("cover_i")
        source_id = (item.get("key") or "").replace("/works/", "")

        summary_parts = []
        if authors:
            summary_parts.append("by " + ", ".join(authors[:2]))
        if item.get("edition_count"):
            summary_parts.append(f"{item.get('edition_count')} edition(s)")

        results.append(
            {
                "title": title,
                "media_kind": _open_library_kind(item),
                "release_year": publish_year,
                "summary": "; ".join(summary_parts),
                "rating": "",
                "barcode": "",
                "category": "Books",
                "brand": ", ".join(authors[:2]),
                "source_name": "openlibrary",
                "source_id": source_id,
                "source_url": f"https://openlibrary.org/works/{source_id}" if source_id else "",
                "remote_url": _open_library_cover_url(cover_id),
                "confidence": item.get("edition_count"),
                "raw_payload": item,
            }
        )
    return results


def lookup_barcode(barcode, barcode_lookup_key=""):
    barcode = (barcode or "").strip()
    if not barcode:
        return None

    if barcode_lookup_key:
        response = requests.get(
            BARCODE_LOOKUP_URL,
            params={"barcode": barcode, "formatted": "y", "key": barcode_lookup_key},
            timeout=10,
        )
        _raise_for_barcode_response(response)
        products = response.json().get("products", [])
        return _barcode_lookup_to_candidate(products[0]) if products else None

    response = requests.get(UPCITEMDB_URL, params={"upc": barcode}, timeout=10)
    _raise_for_barcode_response(response)
    items = response.json().get("items", [])
    return _upcitemdb_to_candidate(items[0]) if items else None


def _raise_for_barcode_response(response):
    if response.status_code == 429:
        raise BarcodeRateLimitError("Barcode provider rate limit reached. Try again later or search by title.")
    if response.status_code in (401, 403):
        raise BarcodeLookupError("Barcode provider rejected the request. Check the API key or provider limit.")
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise BarcodeLookupError(f"Barcode provider failed with HTTP {response.status_code}.") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise BarcodeLookupError("Barcode provider returned a non-JSON response.") from exc

    message = str(data.get("message") or data.get("error") or "")
    code = str(data.get("code") or "")
    if "limit" in message.lower() or "exceed" in message.lower() or code in {"TOO_FAST", "EXCEEDED"}:
        raise BarcodeRateLimitError("Barcode provider rate limit reached. Try again later or search by title.")


def is_physical_media_candidate(candidate):
    haystack = " ".join(
        str(candidate.get(key) or "")
        for key in ("title", "summary", "category", "brand")
    ).lower()

    if any(term in haystack for term in NON_MEDIA_TERMS):
        return False
    return any(term in haystack for term in PHYSICAL_MEDIA_TERMS)


def infer_media_kind(candidate):
    haystack = " ".join(
        str(candidate.get(key) or "")
        for key in ("title", "summary", "category")
    ).lower()
    if any(term in haystack for term in ("book", "paperback", "hardcover", "isbn")):
        return "book"
    if any(term in haystack for term in ("magazine", "periodical")):
        return "magazine"
    if any(term in haystack for term in ("music", "cd", "vinyl", "record", "cassette")):
        return "music"
    return "movie"


def clean_barcode_title(title):
    title = title or ""
    title = re.sub(r"\s*\(B[0-9A-Z]{9}\)\s*$", "", title).strip()
    title = title.replace("OZ", "Oz")
    return title


def _year_from_text(value):
    if not value:
        return None
    match = re.search(r"\b(18|19|20)\d{2}\b", value)
    return int(match.group(0)) if match else None


def _year_from_date(value):
    if not value:
        return None
    try:
        return int(value[:4])
    except ValueError:
        return None


def _tmdb_image_url(path):
    if not path:
        return None
    return f"https://image.tmdb.org/t/p/w500{path}"


def _barcode_lookup_to_candidate(product):
    return {
        "title": product.get("title"),
        "media_kind": infer_media_kind(product),
        "release_year": _year_from_date(product.get("release_date")),
        "summary": product.get("description"),
        "category": product.get("category"),
        "brand": product.get("brand"),
        "barcode": product.get("barcode_number"),
        "remote_url": (product.get("images") or [None])[0],
        "source_name": "barcodelookup",
        "source_id": product.get("barcode_number"),
        "source_url": "",
        "confidence": None,
        "raw_payload": product,
    }


def _upcitemdb_to_candidate(item):
    return {
        "title": clean_barcode_title(item.get("title")),
        "media_kind": infer_media_kind(item),
        "release_year": None,
        "summary": item.get("description"),
        "category": item.get("category"),
        "brand": item.get("brand"),
        "barcode": item.get("upc") or item.get("ean"),
        "remote_url": (item.get("images") or [None])[0],
        "source_name": "upcitemdb",
        "source_id": item.get("upc") or item.get("ean"),
        "source_url": f"https://www.upcitemdb.com/upc/{item.get('upc') or item.get('ean')}",
        "confidence": None,
        "raw_payload": item,
    }


def _open_library_cover_url(cover_id):
    if not cover_id:
        return ""
    return f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"


def _open_library_kind(item):
    subjects = " ".join(item.get("subject") or []).lower()
    title = (item.get("title") or "").lower()
    if "magazine" in subjects or "magazine" in title:
        return "magazine"
    if "comic" in subjects or "graphic novel" in subjects or "manga" in subjects:
        return "book"
    return "book"
