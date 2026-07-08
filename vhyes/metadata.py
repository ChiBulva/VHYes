import requests


TMDB_BASE_URL = "https://api.themoviedb.org/3"
BARCODE_LOOKUP_URL = "https://api.barcodelookup.com/v3/products"
UPCITEMDB_URL = "https://api.upcitemdb.com/prod/trial/lookup"


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
                "release_year": _year_from_date(item.get("release_date")),
                "summary": item.get("overview"),
                "rating": item.get("vote_average"),
                "source_name": "tmdb",
                "source_id": str(item.get("id")),
                "remote_url": _tmdb_image_url(item.get("poster_path")),
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
        response.raise_for_status()
        products = response.json().get("products", [])
        return _barcode_lookup_to_candidate(products[0]) if products else None

    response = requests.get(UPCITEMDB_URL, params={"upc": barcode}, timeout=10)
    response.raise_for_status()
    items = response.json().get("items", [])
    return _upcitemdb_to_candidate(items[0]) if items else None


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
        "release_year": _year_from_date(product.get("release_date")),
        "summary": product.get("description"),
        "barcode": product.get("barcode_number"),
        "remote_url": (product.get("images") or [None])[0],
        "source_name": "barcodelookup",
        "source_id": product.get("barcode_number"),
    }


def _upcitemdb_to_candidate(item):
    return {
        "title": item.get("title"),
        "release_year": None,
        "summary": item.get("description"),
        "barcode": item.get("upc") or item.get("ean"),
        "remote_url": (item.get("images") or [None])[0],
        "source_name": "upcitemdb",
        "source_id": item.get("upc") or item.get("ean"),
    }
