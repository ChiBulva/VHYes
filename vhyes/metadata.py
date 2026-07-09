import re
from urllib.parse import quote

import requests

try:
    from imdb import IMDb
except ImportError:  # pragma: no cover - optional provider
    IMDb = None


class BarcodeLookupError(Exception):
    pass


class BarcodeRateLimitError(BarcodeLookupError):
    pass


TMDB_BASE_URL = "https://api.themoviedb.org/3"
BARCODE_LOOKUP_URL = "https://api.barcodelookup.com/v3/products"
UPCITEMDB_URL = "https://api.upcitemdb.com/prod/trial/lookup"
WIKIDATA_SEARCH_URL = "https://www.wikidata.org/w/api.php"
OPEN_LIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
IMDB_TITLE_URL = "https://www.imdb.com/title/tt{movie_id}/"

_IMDB_CLIENT = None

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



def search_imdb(query):
    if not query or IMDb is None:
        return []

    client = _imdb_client()
    if client is None:
        return []

    try:
        matches = client.search_movie(query)[:10]
    except Exception:
        return []

    results = []
    for match in matches[:8]:
        movie_id = getattr(match, "movieID", None)
        if not movie_id:
            continue

        try:
            movie = client.get_movie(movie_id)
        except Exception:
            movie = match

        kind = str(movie.get("kind") or match.get("kind") or "").lower()
        if "video game" in kind or "podcast" in kind:
            continue

        title = movie.get("title") or movie.get("original title") or match.get("title")
        if not title:
            continue

        genres = movie.get("genres") or []
        runtime_minutes = _first_runtime_minutes(movie.get("runtimes"))
        plot_outline = movie.get("plot outline") or _first_text(movie.get("plot"))
        image_url = movie.get("full-size cover url") or movie.get("cover url") or ""
        rating = movie.get("rating")
        votes = _safe_int(movie.get("votes"))
        imdb_id = str(movie_id).zfill(7)

        raw_payload = {
            "movieID": str(movie_id),
            "title": title,
            "kind": kind,
            "year": _safe_int(movie.get("year")),
            "rating": rating,
            "votes": votes,
            "runtimes": movie.get("runtimes") or [],
            "genres": genres,
            "directors": _person_names(movie.get("director") or movie.get("directors")),
            "cast": _person_names(movie.get("cast"), limit=8),
            "plot_outline": plot_outline,
            "cover_url": image_url,
        }

        results.append(
            {
                "title": title,
                "media_kind": "movie",
                "release_year": _safe_int(movie.get("year")),
                "runtime_minutes": runtime_minutes,
                "genres": ", ".join(genres[:8]) if genres else "",
                "summary": plot_outline,
                "rating": rating,
                "barcode": "",
                "source_name": "imdb",
                "source_id": str(movie_id),
                "source_url": IMDB_TITLE_URL.format(movie_id=imdb_id),
                "remote_url": image_url,
                "confidence": votes or rating,
                "raw_payload": raw_payload,
            }
        )
    return results


def search_wikidata(query):
    if not query:
        return []

    try:
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
    except requests.RequestException:
        return []

    search_rows = response.json().get("search", [])
    entity_ids = [item.get("id") for item in search_rows if item.get("id")]
    entities = _wikidata_entities(entity_ids)

    genre_ids = []
    for entity in entities.values():
        genre_ids.extend(_wikidata_claim_entity_ids(entity.get("claims", {}), "P136"))
    genre_labels = _wikidata_labels(genre_ids)

    results = []
    for item in search_rows:
        entity = entities.get(item.get("id"), {})
        description = _wikidata_description(entity) or item.get("description") or ""
        haystack = f"{item.get('label', '')} {description}".lower()
        if not any(word in haystack for word in ("film", "movie", "television", "series", "video")):
            continue
        results.append(_wikidata_candidate(item, entity, genre_labels))
    return results


def _wikidata_entities(entity_ids):
    entity_ids = [entity_id for entity_id in entity_ids if entity_id]
    if not entity_ids:
        return {}

    try:
        response = requests.get(
            WIKIDATA_SEARCH_URL,
            params={
                "action": "wbgetentities",
                "format": "json",
                "ids": "|".join(entity_ids[:10]),
                "props": "labels|descriptions|claims",
                "languages": "en",
            },
            headers={"User-Agent": "VHYes local media catalog"},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException:
        return {}
    return response.json().get("entities", {})


def _wikidata_labels(entity_ids):
    entity_ids = sorted({entity_id for entity_id in entity_ids if entity_id})
    if not entity_ids:
        return {}

    try:
        response = requests.get(
            WIKIDATA_SEARCH_URL,
            params={
                "action": "wbgetentities",
                "format": "json",
                "ids": "|".join(entity_ids[:40]),
                "props": "labels",
                "languages": "en",
            },
            headers={"User-Agent": "VHYes local media catalog"},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException:
        return {}
    entities = response.json().get("entities", {})
    return {entity_id: _wikidata_label(entity) for entity_id, entity in entities.items()}


def _wikidata_candidate(search_item, entity, genre_labels):
    claims = entity.get("claims", {})
    entity_id = search_item.get("id")
    label = _wikidata_label(entity) or search_item.get("label")
    description = _wikidata_description(entity) or search_item.get("description") or ""
    imdb_id = _wikidata_claim_string(claims, "P345")
    image_name = _wikidata_claim_string(claims, "P18")
    genre_ids = _wikidata_claim_entity_ids(claims, "P136")
    genres = [genre_labels.get(genre_id) for genre_id in genre_ids if genre_labels.get(genre_id)]
    release_year = _wikidata_claim_year(claims, "P577") or _wikidata_claim_year(claims, "P571") or _year_from_text(description)
    runtime_minutes = _wikidata_claim_quantity(claims, "P2047")

    raw_payload = {
        "search": search_item,
        "wikidata": {
            "id": entity_id,
            "label": label,
            "description": description,
            "imdb_id": imdb_id,
            "imdb_url": _imdb_url(imdb_id),
            "image": image_name,
            "genres": genres,
            "release_year": release_year,
            "runtime_minutes": runtime_minutes,
        },
    }

    return {
        "title": label,
        "media_kind": "movie",
        "release_year": release_year,
        "runtime_minutes": runtime_minutes,
        "genres": ", ".join(genres),
        "summary": description,
        "rating": "",
        "barcode": "",
        "source_name": "wikidata",
        "source_id": entity_id,
        "source_url": f"https://www.wikidata.org/wiki/{entity_id}" if entity_id else "",
        "remote_url": _wikimedia_image_url(image_name),
        "confidence": search_item.get("score"),
        "raw_payload": raw_payload,
    }


def _wikidata_label(entity):
    return ((entity.get("labels") or {}).get("en") or {}).get("value", "")


def _wikidata_description(entity):
    return ((entity.get("descriptions") or {}).get("en") or {}).get("value", "")


def _wikidata_claim_string(claims, prop):
    for claim in claims.get(prop, []):
        value = _wikidata_claim_value(claim)
        if value:
            return str(value)
    return ""


def _wikidata_claim_entity_ids(claims, prop):
    ids = []
    for claim in claims.get(prop, []):
        value = _wikidata_claim_value(claim)
        if isinstance(value, dict) and value.get("id"):
            ids.append(value["id"])
    return ids


def _wikidata_claim_year(claims, prop):
    for claim in claims.get(prop, []):
        value = _wikidata_claim_value(claim)
        if isinstance(value, dict):
            year = _year_from_text(value.get("time"))
            if year:
                return year
    return None


def _wikidata_claim_quantity(claims, prop):
    for claim in claims.get(prop, []):
        value = _wikidata_claim_value(claim)
        if isinstance(value, dict) and value.get("amount") is not None:
            try:
                return int(float(value["amount"]))
            except (TypeError, ValueError):
                continue
    return None


def _wikidata_claim_value(claim):
    mainsnak = claim.get("mainsnak") or {}
    datavalue = mainsnak.get("datavalue") or {}
    return datavalue.get("value")


def _wikimedia_image_url(filename):
    if not filename:
        return ""
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(filename)}"


def _imdb_url(imdb_id):
    if not imdb_id:
        return ""
    return IMDB_TITLE_URL.format(movie_id=str(imdb_id).replace("tt", ""))


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
        enriched.extend(search_imdb(query))
        enriched.extend(search_wikidata(query))
    elif candidate.get("media_kind") in {"book", "magazine", "audiobook"}:
        enriched.extend(search_open_library(query))
    else:
        enriched.extend(search_tmdb(query, tmdb_api_key))
        enriched.extend(search_imdb(query))
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
            "runtime_minutes": best.get("runtime_minutes") or candidate.get("runtime_minutes"),
            "genres": best.get("genres") or candidate.get("genres"),
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
    title = re.sub(r"\([^)]*(blu[- ]?ray|blue ray|dvd|vhs|4k|uhd|ultra hd|digital|disc|disk|video)[^)]*\)", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\([^)]*(anniversary|edition|special|widescreen|fullscreen|collector|collectors|limited)[^)]*\)", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(vhs|dvd|blu[- ]?ray|blue ray|4k|uhd|ultra hd|video tape|videotape|cassette|disc|disk)\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(anniversary|edition|special|widescreen|fullscreen|collector'?s?|limited)\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(sequel|part)\b\s*\d*", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\b(18|19|20)\d{2}\b", "", title)
    title = title.replace("+", " ")
    title = re.sub(r"\s+", " ", title).strip(" -:|")
    if "milo" in title.lower() and "return" in title.lower() and ":" not in title:
        title = re.sub(r"\bAtlantis\b\s+", "Atlantis: ", title, flags=re.IGNORECASE)
    return title.strip()


def _best_enrichment_match(query, candidates):
    if not candidates:
        return None

    query_tokens = _title_tokens(query)
    source_priority = {"tmdb": 4, "imdb": 3, "wikidata": 2, "openlibrary": 1}
    best = None
    best_score = None
    for index, candidate in enumerate(candidates):
        title = candidate.get("title") or ""
        title_tokens = _title_tokens(title)
        if not title_tokens:
            continue
        overlap = len(query_tokens & title_tokens)
        if query_tokens and overlap < max(1, min(2, len(query_tokens))):
            continue
        score = (
            overlap,
            bool(candidate.get("release_year")),
            bool(candidate.get("summary")),
            source_priority.get(candidate.get("source_name"), 0),
            _score_confidence(candidate.get("confidence")),
            -index,
        )
        if best_score is None or score > best_score:
            best = candidate
            best_score = score

    return best


def _title_tokens(value):
    stop = {"the", "a", "an", "and", "of", "part", "sequel", "video", "tape"}
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (value or "").lower())
        if token not in stop and len(token) > 1
    }



def _imdb_client():
    global _IMDB_CLIENT
    if IMDb is None:
        return None
    if _IMDB_CLIENT is None:
        try:
            _IMDB_CLIENT = IMDb()
        except Exception:
            return None
    return _IMDB_CLIENT


def _first_text(values):
    if isinstance(values, (list, tuple)) and values:
        return values[0]
    if isinstance(values, str):
        return values
    return ""


def _first_runtime_minutes(values):
    if not values:
        return None
    if isinstance(values, (list, tuple)):
        values = values[0] if values else None
    match = re.search(r"\d+", str(values or ""))
    return int(match.group(0)) if match else None


def _person_names(values, limit=4):
    if not values:
        return []
    if not isinstance(values, (list, tuple)):
        values = [values]
    return [str(value) for value in values[:limit]]


def _safe_int(value):
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _score_confidence(value):
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0


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
