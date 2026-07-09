import csv
import gzip
import os
import re
import sys
from pathlib import Path

import click
from flask import current_app

from .db import get_db, now_iso
from .metadata import clean_media_search_title

TITLE_TYPES = {
    "movie",
    "short",
    "tvMovie",
    "tvSeries",
    "tvMiniSeries",
    "tvSpecial",
    "video",
    "tvShort",
}
PREFERRED_AKA_REGIONS = {"US", "GB", "CA", "AU", "XWW"}
PREFERRED_AKA_LANGUAGES = {"en"}
AKA_TYPES = {"alternative", "dvd", "imdbDisplay", "video", "working"}
BATCH_SIZE = 10_000
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


@click.command("import-imdb")
@click.option("--data-dir", type=click.Path(file_okay=False, path_type=Path), default=None, help="Directory containing IMDb .tsv.gz files.")
@click.option("--limit", type=int, default=None, help="Import only this many title.basics rows for a quick test.")
@click.option("--skip-akas", is_flag=True, help="Skip alternate title import.")
def import_imdb_command(data_dir, limit, skip_akas):
    """Import local IMDb TSV datasets into the VHYes SQLite reference index."""
    data_dir = data_dir or Path(current_app.config["IMDB_DATA_DIR"])
    click.echo(f"Importing IMDb data from {data_dir}")
    stats = import_imdb_dataset(get_db(), data_dir, limit=limit, include_akas=not skip_akas, progress=click.echo)
    click.echo(f"Imported {stats['titles']:,} IMDb titles")
    click.echo(f"Imported {stats['ratings']:,} IMDb ratings")
    click.echo(f"Indexed {stats['aliases']:,} alternate titles")


def import_imdb_dataset(db, data_dir, limit=None, include_akas=True, progress=None):
    data_dir = Path(data_dir)
    basics_path = _dataset_path(data_dir, "title.basics.tsv")
    ratings_path = _dataset_path(data_dir, "title.ratings.tsv", required=False)
    akas_path = _dataset_path(data_dir, "title.akas.tsv", required=False)
    imported_at = now_iso()

    _prepare_for_import(db)
    _reset_reference_tables(db)

    valid_tconsts = set()
    if progress:
        progress("Importing title.basics...")
    title_count = _import_basics(db, basics_path, valid_tconsts, imported_at, limit=limit)
    if progress:
        progress(f"Imported {title_count:,} title rows")
    if progress and ratings_path:
        progress("Importing title.ratings...")
    rating_count = _import_ratings(db, ratings_path, valid_tconsts) if ratings_path else 0
    if progress and ratings_path:
        progress(f"Imported {rating_count:,} rating rows")
    if progress and include_akas and akas_path:
        progress("Indexing title.akas alternate titles...")
    alias_count = _import_akas(db, akas_path, valid_tconsts, limit=limit) if include_akas and akas_path else 0
    if progress and include_akas and akas_path:
        progress(f"Indexed {alias_count:,} alternate title rows")

    _record_import(db, "title.basics", basics_path, title_count, imported_at)
    if ratings_path:
        _record_import(db, "title.ratings", ratings_path, rating_count, imported_at)
    if include_akas and akas_path:
        _record_import(db, "title.akas", akas_path, alias_count, imported_at)
    db.commit()

    return {"titles": title_count, "ratings": rating_count, "aliases": alias_count}


def search_local_imdb(query, db=None, limit=12):
    db = db or get_db()
    match_query = _fts_match_query(query)
    if not match_query or not _has_reference_titles(db):
        return []

    tokens = _token_list(query)
    order_by = (
        "exact_match DESC, COALESCE(t.num_votes, 0) DESC, rank ASC, COALESCE(t.start_year, 0) DESC"
        if len(tokens) == 1
        else "COALESCE(t.num_votes, 0) DESC, exact_match DESC, rank ASC, COALESCE(t.start_year, 0) DESC"
    )
    rows = db.execute(
        f"""
        SELECT t.*, imdb_title_fts.title AS matched_title,
               bm25(imdb_title_fts) AS rank,
               CASE WHEN lower(imdb_title_fts.title) = lower(?) THEN 1 ELSE 0 END AS exact_match
        FROM imdb_title_fts
        JOIN imdb_titles t ON t.tconst = imdb_title_fts.tconst
        WHERE imdb_title_fts MATCH ?
        ORDER BY {order_by}
        LIMIT ?
        """,
        (query.strip(), match_query, limit * 8),
    ).fetchall()

    candidates = []
    seen = set()
    for row in rows:
        if row["tconst"] in seen:
            continue
        seen.add(row["tconst"])
        candidates.append(_row_to_candidate(row))
        if len(candidates) >= limit:
            break
    return candidates


def enrich_candidate_from_local_imdb(candidate, db=None):
    if not candidate:
        return candidate
    if candidate.get("barcode_source_name") or candidate.get("source_name") not in {"upcitemdb", "barcodelookup"}:
        return candidate

    query = clean_media_search_title(candidate.get("title", ""))
    if not query:
        return candidate

    matches = search_local_imdb(query, db=db, limit=8)
    best = _best_match(query, matches)
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
            "runtime_minutes": best.get("runtime_minutes") or candidate.get("runtime_minutes"),
            "genres": best.get("genres") or candidate.get("genres"),
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


def _prepare_for_import(db):
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA temp_store=MEMORY")


def _reset_reference_tables(db):
    db.execute("DELETE FROM imdb_title_fts")
    db.execute("DELETE FROM imdb_titles")
    db.execute("DELETE FROM imdb_imports")
    db.commit()


def _import_basics(db, path, valid_tconsts, imported_at, limit=None):
    title_rows = []
    fts_rows = []
    count = 0
    read_count = 0

    for row in _dict_rows(path):
        read_count += 1
        if limit and read_count > limit:
            break
        if row.get("titleType") not in TITLE_TYPES:
            continue
        if _int_or_none(row.get("isAdult")) == 1:
            continue

        tconst = row.get("tconst")
        primary_title = _clean_value(row.get("primaryTitle"))
        if not tconst or not primary_title:
            continue

        original_title = _clean_value(row.get("originalTitle"))
        title_type = row.get("titleType") or "movie"
        start_year = _int_or_none(row.get("startYear"))
        runtime_minutes = _int_or_none(row.get("runtimeMinutes"))
        genres = _clean_value(row.get("genres"))

        title_rows.append(
            (
                tconst,
                title_type,
                primary_title,
                original_title,
                start_year,
                runtime_minutes,
                genres,
                None,
                None,
                imported_at,
            )
        )
        fts_rows.append((tconst, primary_title, primary_title, title_type))
        if original_title and original_title.lower() != primary_title.lower():
            fts_rows.append((tconst, original_title, primary_title, title_type))
        valid_tconsts.add(tconst)
        count += 1

        if len(title_rows) >= BATCH_SIZE:
            _flush_titles(db, title_rows, fts_rows)
            title_rows.clear()
            fts_rows.clear()

    _flush_titles(db, title_rows, fts_rows)
    db.commit()
    return count


def _import_ratings(db, path, valid_tconsts):
    rows = []
    count = 0
    for row in _dict_rows(path):
        tconst = row.get("tconst")
        if tconst not in valid_tconsts:
            continue
        rows.append((_float_or_none(row.get("averageRating")), _int_or_none(row.get("numVotes")), tconst))
        count += 1
        if len(rows) >= BATCH_SIZE:
            _flush_ratings(db, rows)
            rows.clear()
    _flush_ratings(db, rows)
    db.commit()
    return count


def _import_akas(db, path, valid_tconsts, limit=None):
    rows = []
    count = 0
    read_count = 0
    for row in _dict_rows(path):
        read_count += 1
        if limit and read_count > limit:
            break
        tconst = row.get("titleId")
        if tconst not in valid_tconsts:
            continue
        if not _use_aka(row):
            continue
        title = _clean_value(row.get("title"))
        if not title:
            continue
        rows.append((tconst, title, title, "aka"))
        count += 1
        if len(rows) >= BATCH_SIZE:
            _flush_fts(db, rows)
            rows.clear()
    _flush_fts(db, rows)
    db.commit()
    return count


def _flush_titles(db, title_rows, fts_rows):
    if title_rows:
        db.executemany(
            """
            INSERT OR REPLACE INTO imdb_titles (
                tconst, title_type, primary_title, original_title, start_year,
                runtime_minutes, genres, average_rating, num_votes, imported_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            title_rows,
        )
    _flush_fts(db, fts_rows)


def _flush_ratings(db, rows):
    if rows:
        db.executemany(
            "UPDATE imdb_titles SET average_rating = ?, num_votes = ? WHERE tconst = ?",
            rows,
        )


def _flush_fts(db, rows):
    if rows:
        db.executemany(
            "INSERT INTO imdb_title_fts (tconst, title, primary_title, title_type) VALUES (?, ?, ?, ?)",
            rows,
        )


def _record_import(db, source, path, count, imported_at):
    db.execute(
        """
        INSERT OR REPLACE INTO imdb_imports (source, source_path, row_count, imported_at)
        VALUES (?, ?, ?, ?)
        """,
        (source, str(path), count, imported_at),
    )


def _row_to_candidate(row):
    title_type = row["title_type"] or "movie"
    genres = row["genres"] or ""
    summary_parts = [_title_type_label(title_type)]
    if row["runtime_minutes"]:
        summary_parts.append(f"{row['runtime_minutes']} min")
    if genres:
        summary_parts.append(genres.replace(",", ", "))

    return {
        "title": row["primary_title"],
        "media_kind": "movie",
        "release_year": row["start_year"] or "",
        "runtime_minutes": row["runtime_minutes"] or "",
        "genres": genres,
        "summary": " · ".join(part for part in summary_parts if part),
        "rating": row["average_rating"] or "",
        "barcode": "",
        "source_name": "imdb-local",
        "source_id": row["tconst"],
        "source_url": f"https://www.imdb.com/title/{row['tconst']}/",
        "remote_url": "",
        "confidence": row["num_votes"] or "",
        "raw_payload": {
            "tconst": row["tconst"],
            "title_type": title_type,
            "primary_title": row["primary_title"],
            "original_title": row["original_title"],
            "matched_title": row["matched_title"],
            "start_year": row["start_year"],
            "runtime_minutes": row["runtime_minutes"],
            "genres": genres,
            "average_rating": row["average_rating"],
            "num_votes": row["num_votes"],
            "source": "IMDb non-commercial datasets",
        },
    }


def _best_match(query, candidates):
    query_tokens = _tokens(query)
    best = None
    best_score = None
    for index, candidate in enumerate(candidates):
        title_tokens = _tokens(candidate.get("title"))
        overlap = len(query_tokens & title_tokens)
        if query_tokens and overlap < max(1, min(2, len(query_tokens))):
            continue
        score = (
            overlap,
            bool(candidate.get("release_year")),
            bool(candidate.get("runtime_minutes")),
            _float_or_none(candidate.get("rating")) or 0,
            _int_or_none(candidate.get("confidence")) or 0,
            -index,
        )
        if best_score is None or score > best_score:
            best = candidate
            best_score = score
    return best


def _has_reference_titles(db):
    try:
        return db.execute("SELECT 1 FROM imdb_titles LIMIT 1").fetchone() is not None
    except Exception:
        return False


def _fts_match_query(query):
    tokens = _token_list(query)
    if not tokens:
        return ""
    return " ".join(_fts_token_clause(token) for token in tokens[:8])


def _fts_token_clause(token):
    if token.endswith("s") and len(token) > 4:
        token = token[:-1]
    return f"{token}*"


def _tokens(value):
    return set(_token_list(value))


def _token_list(value):
    stop = {"the", "a", "an", "and", "of", "part", "sequel", "video", "tape", "dvd", "blu", "ray"}
    tokens = []
    seen = set()
    for token in re.findall(r"[a-z0-9]+", (value or "").lower()):
        if token in stop or len(token) <= 1 or token in seen:
            continue
        tokens.append(token)
        seen.add(token)
    return tokens


def _use_aka(row):
    region = _clean_value(row.get("region"))
    language = _clean_value(row.get("language"))
    types = set(_split_imdb_values(row.get("types")))
    attributes = set(_split_imdb_values(row.get("attributes")))
    if region in PREFERRED_AKA_REGIONS or language in PREFERRED_AKA_LANGUAGES:
        return True
    if types & AKA_TYPES:
        return True
    if attributes & {"DVD title", "video box title"}:
        return True
    return row.get("isOriginalTitle") == "1"


def _split_imdb_values(value):
    value = _clean_value(value)
    return [part.strip() for part in value.split(",") if part.strip()]


def _title_type_label(value):
    labels = {
        "movie": "Movie",
        "short": "Short",
        "tvMovie": "TV movie",
        "tvSeries": "TV series",
        "tvMiniSeries": "TV miniseries",
        "tvSpecial": "TV special",
        "video": "Video",
        "tvShort": "TV short",
    }
    return labels.get(value, value or "IMDb title")


def _dataset_path(data_dir, filename, required=True):
    candidates = [data_dir / f"{filename}.gz", data_dir / filename]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    if required:
        raise click.ClickException(f"Missing required IMDb dataset: {candidates[0]}")
    return None


def _dict_rows(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as handle:
        yield from csv.DictReader(handle, delimiter="\t")


def _clean_value(value):
    if value in (None, "", "\\N"):
        return ""
    return str(value).strip()


def _int_or_none(value):
    value = _clean_value(value)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _float_or_none(value):
    value = _clean_value(value)
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
