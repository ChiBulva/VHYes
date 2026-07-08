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


def enrich_barcode_candidate(candidate, tmdb_api_key=""):
    if not candidate:
        return candidate
    if candidate.get("barcode_source_name") or candidate.get("source_name") not in {"upcitemdb", "barcodelookup"}:
        return candidate

    query = clean_media_search_title(candidate.get("title", ""))
    if not query:
        return candidate

    enriched = []
    if candidate.get("media_kind") == "movie":
        enriched.extend(search_tmdb(query, tmdb_api_key))
        enriched.extend(search_wikidata(query))
    elif candidate.get("media_kind") in {"book", "magazine", "audiobook"}:
        enriched.extend(search_open_library(query))
    else:
        enriched.extend(search_tmdb(query, tmdb_api_key))
        enriched.extend(search_wikidata(query))
        enriched.extend(search_open_library(query))

    best = _best_enrichment_match(query, enriched)
    if not best:
        candidate = dict(candidate)
        candidate["enrichment_query"] = query
        return candidate

    merged = dict(candidate)
    barcode_source = {
        "source_name": candidate.get("source_name"),
        "source_id": candidate.get("source_id"),
        "source_url": candidate.get("source_url"),
        "raw_payload": candidate.get("raw_payload"),
    }
    merged.update(
        {
            "title": best.get("title") or candidate.get("title"),
            "media_kind": best.get("media_kind") or candidate.get("media_kind"),
            "release_year": best.get("release_year") or candidate.get("release_year"),
            "summary": best.get("summary") or candidate.get("summary"),
            "rating": best.get("rating") or candidate.get("rating"),
            "remote_url": best.get("remote_url") or candidate.get("remote_url"),
            "source_name": best.get("source_name") or candidate.get("source_name"),
            "source_id": best.get("source_id") or candidate.get("source_id"),
            "source_url": best.get("source_url") or candidate.get("source_url"),
            "confidence": best.get("confidence") or candidate.get("confidence"),
            "enrichment_query": query,
            "barcode_source_name": barcode_source["source_name"],
            "barcode_source_id": barcode_source["source_id"],
            "barcode_source_url": barcode_source["source_url"],
            "raw_payload": {
                "barcode_source": barcode_source,
                "enrichment_source": best.get("raw_payload"),
                "enrichment_query": query,
            },
        }
    )
    return merged


def clean_media_search_title(title):
    title = clean_barcode_title(title)
    title = re.sub(r"\b(disney|walt disney|mgm/ua|studios?)\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\([^)]*(anniversary|edition|special|widescreen|fullscreen|collector|collectors|limited)[^)]*\)", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(vhs|dvd|blu[- ]?ray|4k|uhd|ultra hd|video tape|videotape|cassette|disc|disk)\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(anniversary|edition|special|widescreen|fullscreen|collector'?s?|limited)\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(sequel|part)\b\s*\d*", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(18|19|20)\d{2}\b", "", title)
    title = re.sub(r"\s+", " ", title).strip(" -:|")
    if "milo" in title.lower() and "return" in title.lower() and ":" not in title:
        title = re.sub(r"\bAtlantis\b\s+", "Atlantis: ", title, flags=re.IGNORECASE)
    return title.strip()


def _best_enrichment_match(query, candidates):
    if not candidates:
        return None

    query_tokens = _title_tokens(query)
    ranked = []
    for candidate in candidates:
        title = candidate.get("title") or ""
        title_tokens = _title_tokens(title)
        if not title_tokens:
            continue
        overlap = len(query_tokens & title_tokens)
        if query_tokens and overlap < max(1, min(2, len(query_tokens))):
            continue
        ranked.append((overlap, bool(candidate.get("release_year")), bool(candidate.get("summary")), candidate))

    if not ranked:
        return None
    ranked.sort(key=lambda item: item[:3], reverse=True)
    return ranked[0][3]


def _title_tokens(value):
    stop = {"the", "a", "an", "and", "of", "part", "sequel", "video", "tape"}
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (value or "").lower())
        if token not in stop and len(token) > 1
    }


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
