import json
import os
import re
import uuid

from flask import (
    Blueprint,
    current_app,
    flash,
    has_request_context,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

from .db import get_db, now_iso
from .metadata import (
    BarcodeLookupError,
    BarcodeRateLimitError,
    enrich_barcode_candidate,
    is_physical_media_candidate,
    lookup_barcode,
    search_open_library,
    search_tmdb,
    search_wikidata,
)

bp = Blueprint("vhyes", __name__)

ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}


@bp.route("/")
def library():
    filters = {
        "q": request.args.get("q", "").strip(),
        "format": request.args.get("format", "").strip(),
        "mood": request.args.get("mood", "").strip(),
        "year": request.args.get("year", "").strip(),
        "rating": request.args.get("rating", "").strip(),
        "kind": request.args.get("kind", "").strip(),
        "shelf": request.args.get("shelf", "").strip(),
        "tag": request.args.get("tag", "").strip(),
    }
    items = _library_items(filters=filters)

    return render_template(
        "library.html",
        items=items,
        filters=filters,
        formats=_formats(),
        kinds=_kinds(),
        moods=_distinct("mood"),
        shelves=_shelves(),
        tags=_tags(),
        years=_years(),
    )


@bp.route("/browse")
def browse():
    db = get_db()
    return render_template(
        "browse.html",
        decades=db.execute(
            """
            SELECT (release_year / 10) * 10 AS decade, COUNT(*) AS count
            FROM media_items
            WHERE release_year IS NOT NULL
            GROUP BY decade
            ORDER BY decade DESC
            """
        ).fetchall(),
        formats=db.execute(
            """
            SELECT f.name, COUNT(mi.id) AS count
            FROM formats f
            JOIN physical_copies pc ON pc.format_id = f.id
            JOIN media_items mi ON mi.id = pc.media_item_id
            GROUP BY f.id
            ORDER BY f.sort_order, f.name
            """
        ).fetchall(),
        moods=db.execute(
            """
            SELECT mood AS name, COUNT(*) AS count
            FROM media_items
            WHERE mood IS NOT NULL AND mood != ''
            GROUP BY mood
            ORDER BY mood
            """
        ).fetchall(),
        shelves=db.execute(
            """
            SELECT shelf_location AS name, COUNT(*) AS count
            FROM physical_copies
            WHERE shelf_location IS NOT NULL AND shelf_location != ''
            GROUP BY shelf_location
            ORDER BY shelf_location
            """
        ).fetchall(),
        tags=db.execute(
            """
            SELECT t.name, t.tag_type, COUNT(mt.media_item_id) AS count
            FROM tags t
            JOIN media_tags mt ON mt.tag_id = t.id
            GROUP BY t.id
            ORDER BY t.tag_type, t.name
            """
        ).fetchall(),
        recent=_library_items(limit=8),
        random_item=_random_item(),
    )


@bp.route("/random")
def random_pick():
    item = _random_item()
    if not item:
        flash("No media available for a random pick yet.", "warn")
        return redirect(url_for("vhyes.library"))
    return redirect(url_for("vhyes.detail", item_id=item["id"]))


@bp.route("/settings/database", methods=("GET", "POST"))
def database_settings():
    counts = _database_counts()

    if request.method == "POST":
        if request.form.get("confirm", "").strip() != "DELETE":
            flash("Type DELETE to reset the local database.", "error")
            return render_template("database_settings.html", counts=counts)

        _reset_catalog_data()
        flash("Local catalog database was reset.", "success")
        return redirect(url_for("vhyes.database_settings"))

    return render_template("database_settings.html", counts=counts)


@bp.route("/add", methods=("GET", "POST"))
def add_media():
    query = ""
    candidates = []
    auto_add_single = request.form.get("auto_add_single") == "1"

    if request.method == "POST" and request.form.get("search_action"):
        query = request.form.get("query", "").strip()
        candidates = _search_candidates(query)
        is_barcode = _is_barcode_query(query)

        if is_barcode and auto_add_single and len(candidates) == 1:
            item_id = _save_media(_candidate_to_save_data(candidates[0]), None)
            flash(f"Added {candidates[0].get('title', 'media')}.", "success")
            return redirect(url_for("vhyes.add_media", added=item_id))

        if not candidates and not is_barcode:
            flash("No title match found. Try a more specific title or scan a barcode.", "warn")

    if request.method == "POST" and request.form.get("add_action"):
        item_id = _save_media(request.form, request.files.get("cover_file"))
        flash(f"Added {request.form.get('title', 'media')}.", "success")
        return redirect(url_for("vhyes.add_media", added=item_id))

    return render_template(
        "add.html",
        auto_add_single=auto_add_single,
        candidates=candidates,
        formats=_formats(),
        last_added=_get_added_preview(request.args.get("added")),
        query=query,
    )


@bp.route("/media/<int:item_id>")
def detail(item_id):
    bundle = _get_item_bundle(item_id)
    if bundle["item"] is None:
        return "Not found", 404
    return render_template("detail.html", **bundle)


@bp.route("/media/<int:item_id>/edit", methods=("GET", "POST"))
def edit_media(item_id):
    bundle = _get_item_bundle(item_id)
    if bundle["item"] is None:
        return "Not found", 404

    if request.method == "POST":
        _update_media(item_id, request.form, request.files.get("cover_file"))
        flash(f"Updated {request.form.get('title', 'media')}.", "success")
        return redirect(url_for("vhyes.detail", item_id=item_id))

    return render_template(
        "edit.html",
        **bundle,
        formats=_formats(),
    )


@bp.route("/media/<int:item_id>/delete", methods=("POST",))
def delete_media(item_id):
    db = get_db()
    item = db.execute("SELECT title FROM media_items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        flash("That media item was already gone.", "warn")
        return redirect(url_for("vhyes.library"))

    db.execute("DELETE FROM media_items WHERE id = ?", (item_id,))
    db.commit()
    flash(f"Removed {item['title']}.", "success")
    return redirect(url_for("vhyes.library"))


@bp.route("/covers/<path:filename>")
def cover_file(filename):
    return send_from_directory(current_app.config["COVERS_DIR"], filename)


def _library_items(filters=None, limit=None):
    filters = filters or {}
    where = []
    params = []

    if filters.get("q"):
        where.append("(mi.title LIKE ? OR mi.summary LIKE ? OR mi.extra_info LIKE ?)")
        params.extend([f"%{filters['q']}%", f"%{filters['q']}%", f"%{filters['q']}%"])
    if filters.get("format"):
        where.append("f.name = ?")
        params.append(filters["format"])
    if filters.get("mood"):
        where.append("mi.mood = ?")
        params.append(filters["mood"])
    if filters.get("year"):
        year = filters["year"]
        if year.endswith("s") and year[:-1].isdigit():
            start = int(year[:-1])
            where.append("mi.release_year BETWEEN ? AND ?")
            params.extend([start, start + 9])
        else:
            where.append("mi.release_year = ?")
            params.append(year)
    if filters.get("rating"):
        where.append("COALESCE(mi.personal_rating, mi.rating, 0) >= ?")
        params.append(filters["rating"])
    if filters.get("kind"):
        where.append("mi.media_kind = ?")
        params.append(filters["kind"])
    if filters.get("shelf"):
        where.append("pc.shelf_location = ?")
        params.append(filters["shelf"])
    if filters.get("tag"):
        where.append("EXISTS (SELECT 1 FROM media_tags mt JOIN tags t ON t.id = mt.tag_id WHERE mt.media_item_id = mi.id AND t.name = ?)")
        params.append(filters["tag"])

    where_sql = "WHERE " + " AND ".join(where) if where else ""
    limit_sql = "LIMIT ?" if limit else ""
    if limit:
        params.append(limit)

    return get_db().execute(
        f"""
        SELECT
            mi.*,
            f.name AS format_name,
            pc.barcode,
            pc.shelf_location,
            pc.purchase_price,
            pc.estimated_value,
            img.local_path,
            img.remote_url
        FROM media_items mi
        LEFT JOIN physical_copies pc ON pc.media_item_id = mi.id
        LEFT JOIN formats f ON f.id = pc.format_id
        LEFT JOIN images img ON img.media_item_id = mi.id AND img.image_type = 'cover'
        {where_sql}
        GROUP BY mi.id
        ORDER BY mi.created_at DESC
        {limit_sql}
        """,
        params,
    ).fetchall()


def _random_item():
    return get_db().execute(
        """
        SELECT mi.id, mi.title, mi.release_year, f.name AS format_name,
               img.local_path, img.remote_url
        FROM media_items mi
        LEFT JOIN physical_copies pc ON pc.media_item_id = mi.id
        LEFT JOIN formats f ON f.id = pc.format_id
        LEFT JOIN images img ON img.media_item_id = mi.id AND img.image_type = 'cover'
        GROUP BY mi.id
        ORDER BY RANDOM()
        LIMIT 1
        """
    ).fetchone()


def _get_item_bundle(item_id):
    db = get_db()
    item = db.execute(
        """
        SELECT mi.*, f.name AS format_name, pc.id AS copy_id, pc.format_id,
               pc.barcode, pc.edition, pc.shelf_location, pc.condition_note,
               pc.purchase_price, pc.estimated_value, pc.acquired_at,
               img.local_path, img.remote_url, img.source_name AS image_source_name,
               img.source_url AS image_source_url, img.license_note
        FROM media_items mi
        LEFT JOIN physical_copies pc ON pc.media_item_id = mi.id
        LEFT JOIN formats f ON f.id = pc.format_id
        LEFT JOIN images img ON img.media_item_id = mi.id AND img.image_type = 'cover'
        WHERE mi.id = ?
        """,
        (item_id,),
    ).fetchone()
    if item is None:
        return {"item": None}

    return {
        "item": item,
        "genres": _item_genres(item_id),
        "tags": _item_tags(item_id),
        "metadata_sources": db.execute(
            "SELECT * FROM metadata_sources WHERE media_item_id = ? ORDER BY created_at DESC",
            (item_id,),
        ).fetchall(),
        "external_links": db.execute(
            "SELECT * FROM external_links WHERE media_item_id = ? ORDER BY label",
            (item_id,),
        ).fetchall(),
    }


def _get_added_preview(item_id):
    item_id = _int_or_none(item_id)
    if not item_id:
        return None

    return get_db().execute(
        """
        SELECT mi.id, mi.title, mi.release_year, f.name AS format_name,
               img.local_path, img.remote_url
        FROM media_items mi
        LEFT JOIN physical_copies pc ON pc.media_item_id = mi.id
        LEFT JOIN formats f ON f.id = pc.format_id
        LEFT JOIN images img ON img.media_item_id = mi.id AND img.image_type = 'cover'
        WHERE mi.id = ?
        """,
        (item_id,),
    ).fetchone()


def _search_candidates(query):
    query = (query or "").strip()
    if not query:
        return []

    if _is_barcode_query(query):
        barcode = _clean_barcode(query)
        cached = _get_cached_barcode(barcode)
        if cached:
            if cached["status"] == "match":
                candidate = json.loads(cached["payload"])
                enriched = enrich_barcode_candidate(candidate, current_app.config["TMDB_API_KEY"])
                if enriched != candidate:
                    _set_cached_barcode(barcode, "match", enriched, None)
                return [_prepare_candidate(enriched)]
            _flash_if_possible(cached["error_message"] or "Cached barcode is not physical media.", "warn")
            return []

        try:
            candidate = lookup_barcode(
                barcode,
                current_app.config["BARCODE_LOOKUP_API_KEY"],
            )
        except BarcodeRateLimitError as exc:
            _flash_if_possible(str(exc), "error")
            return []
        except BarcodeLookupError as exc:
            _flash_if_possible(str(exc), "error")
            return []

        if not candidate:
            _set_cached_barcode(barcode, "miss", None, "No barcode match found.")
            return []
        if not is_physical_media_candidate(candidate):
            _set_cached_barcode(barcode, "non_media", candidate, "Barcode match was not physical media.")
            return []

        candidate = enrich_barcode_candidate(candidate, current_app.config["TMDB_API_KEY"])
        _set_cached_barcode(barcode, "match", candidate, None)
        return [_prepare_candidate(candidate)]

    candidates = []
    candidates.extend(search_tmdb(query, current_app.config["TMDB_API_KEY"]))
    candidates.extend(search_open_library(query))
    candidates.extend(search_wikidata(query))
    return [_prepare_candidate(candidate) for candidate in _dedupe_candidates(candidates)]


def _save_media(form, cover_file):
    db = get_db()
    now = now_iso()
    title = form.get("title", "").strip()

    cursor = db.execute(
        """
        INSERT INTO media_items (
            title, sort_title, media_kind, release_year, runtime_minutes, rating,
            personal_rating, mood, mood_summary, summary, filter_notes, extra_info,
            source_name, source_id, source_fingerprint, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            _sort_title(title),
            form.get("media_kind", "movie"),
            _int_or_none(form.get("release_year")),
            _int_or_none(form.get("runtime_minutes")),
            _float_or_none(form.get("rating")),
            _float_or_none(form.get("personal_rating")),
            form.get("mood", "").strip() or None,
            form.get("mood_summary", "").strip() or None,
            form.get("summary", "").strip() or None,
            form.get("filter_notes", "").strip() or None,
            form.get("extra_info", "").strip() or None,
            form.get("source_name", "").strip() or None,
            form.get("source_id", "").strip() or None,
            _source_fingerprint(form),
            now,
            now,
        ),
    )
    item_id = cursor.lastrowid

    _upsert_copy(db, item_id, form, now)
    _replace_genres(db, item_id, form.get("genres", ""))
    _replace_tags(db, item_id, form.get("tags", ""), "filter")
    _save_image(db, item_id, form, cover_file, now, replace=False)
    _save_metadata_source(db, item_id, form, now)
    _save_external_link(db, item_id, form, now)

    db.commit()
    return item_id


def _update_media(item_id, form, cover_file):
    db = get_db()
    now = now_iso()
    title = form.get("title", "").strip()

    db.execute(
        """
        UPDATE media_items
        SET title = ?, sort_title = ?, media_kind = ?, release_year = ?,
            runtime_minutes = ?, rating = ?, personal_rating = ?, mood = ?,
            mood_summary = ?, summary = ?, filter_notes = ?, extra_info = ?,
            source_name = ?, source_id = ?, source_fingerprint = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            title,
            _sort_title(title),
            form.get("media_kind", "movie"),
            _int_or_none(form.get("release_year")),
            _int_or_none(form.get("runtime_minutes")),
            _float_or_none(form.get("rating")),
            _float_or_none(form.get("personal_rating")),
            form.get("mood", "").strip() or None,
            form.get("mood_summary", "").strip() or None,
            form.get("summary", "").strip() or None,
            form.get("filter_notes", "").strip() or None,
            form.get("extra_info", "").strip() or None,
            form.get("source_name", "").strip() or None,
            form.get("source_id", "").strip() or None,
            _source_fingerprint(form),
            now,
            item_id,
        ),
    )

    _upsert_copy(db, item_id, form, now)
    _replace_genres(db, item_id, form.get("genres", ""))
    _replace_tags(db, item_id, form.get("tags", ""), "filter")
    _save_image(db, item_id, form, cover_file, now, replace=True)
    _save_external_link(db, item_id, form, now)
    db.commit()


def _upsert_copy(db, item_id, form, now):
    existing = db.execute(
        "SELECT id FROM physical_copies WHERE media_item_id = ? LIMIT 1",
        (item_id,),
    ).fetchone()
    values = (
        _int_or_none(form.get("format_id")),
        form.get("barcode", "").strip() or None,
        form.get("edition", "").strip() or None,
        form.get("shelf_location", "").strip() or None,
        form.get("condition_note", "").strip() or None,
        _float_or_none(form.get("purchase_price")),
        _float_or_none(form.get("estimated_value")),
        form.get("acquired_at", "").strip() or None,
    )
    if existing:
        db.execute(
            """
            UPDATE physical_copies
            SET format_id = ?, barcode = ?, edition = ?, shelf_location = ?,
                condition_note = ?, purchase_price = ?, estimated_value = ?, acquired_at = ?
            WHERE id = ?
            """,
            values + (existing["id"],),
        )
    else:
        db.execute(
            """
            INSERT INTO physical_copies (
                media_item_id, format_id, barcode, edition, shelf_location,
                condition_note, purchase_price, estimated_value, acquired_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (item_id,) + values + (now,),
        )


def _save_image(db, item_id, form, upload, now, replace=False):
    local_path = None
    if upload and upload.filename and _allowed_image(upload.filename):
        filename = secure_filename(upload.filename)
        ext = filename.rsplit(".", 1)[1].lower()
        stored_name = f"{uuid.uuid4().hex}.{ext}"
        upload.save(os.path.join(current_app.config["COVERS_DIR"], stored_name))
        local_path = stored_name

    remote_url = form.get("image_url_override", "").strip() or form.get("remote_url", "").strip() or None
    if replace and (local_path or remote_url):
        db.execute("DELETE FROM images WHERE media_item_id = ? AND image_type = 'cover'", (item_id,))

    if local_path or remote_url:
        db.execute(
            """
            INSERT INTO images (
                media_item_id, image_type, local_path, remote_url, source_name,
                source_url, license_note, created_at
            )
            VALUES (?, 'cover', ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                local_path,
                remote_url,
                form.get("source_name", "").strip() or None,
                (form.get("image_url_override", "").strip() or form.get("source_url", "").strip()) or None,
                form.get("license_note", "").strip() or None,
                now,
            ),
        )


def _save_metadata_source(db, item_id, form, now):
    source_name = form.get("source_name", "").strip()
    source_id = form.get("source_id", "").strip()
    raw_payload = _raw_payload_text(form.get("raw_payload"))
    source_url = form.get("source_url", "").strip()
    confidence = _float_or_none(form.get("confidence"))
    if not any((source_name, source_id, source_url, raw_payload)):
        return

    db.execute(
        """
        INSERT INTO metadata_sources (
            media_item_id, source_name, source_id, source_url, raw_payload,
            confidence, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            source_name or "manual",
            source_id or None,
            source_url or None,
            raw_payload,
            confidence,
            now,
            now,
        ),
    )


def _save_external_link(db, item_id, form, now):
    source_url = form.get("source_url", "").strip() or form.get("image_url_override", "").strip()
    source_name = form.get("source_name", "").strip() or "Source"
    if not source_url:
        return
    exists = db.execute(
        "SELECT id FROM external_links WHERE media_item_id = ? AND url = ?",
        (item_id, source_url),
    ).fetchone()
    if exists:
        return
    db.execute(
        """
        INSERT INTO external_links (media_item_id, label, url, source_name, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (item_id, source_name.title(), source_url, source_name, now),
    )


def _replace_genres(db, item_id, genres_text):
    db.execute("DELETE FROM media_genres WHERE media_item_id = ?", (item_id,))
    names = _split_values(genres_text)
    for name in names:
        db.execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (name,))
        genre = db.execute("SELECT id FROM genres WHERE name = ?", (name,)).fetchone()
        db.execute(
            "INSERT OR IGNORE INTO media_genres (media_item_id, genre_id) VALUES (?, ?)",
            (item_id, genre["id"]),
        )


def _replace_tags(db, item_id, tags_text, tag_type):
    tag_rows = db.execute(
        "SELECT t.id FROM tags t JOIN media_tags mt ON mt.tag_id = t.id WHERE mt.media_item_id = ? AND t.tag_type = ?",
        (item_id, tag_type),
    ).fetchall()
    for row in tag_rows:
        db.execute("DELETE FROM media_tags WHERE media_item_id = ? AND tag_id = ?", (item_id, row["id"]))

    for name in _split_values(tags_text):
        db.execute("INSERT OR IGNORE INTO tags (name, tag_type) VALUES (?, ?)", (name, tag_type))
        tag = db.execute("SELECT id FROM tags WHERE name = ? AND tag_type = ?", (name, tag_type)).fetchone()
        db.execute(
            "INSERT OR IGNORE INTO media_tags (media_item_id, tag_id) VALUES (?, ?)",
            (item_id, tag["id"]),
        )


def _item_genres(item_id):
    rows = get_db().execute(
        """
        SELECT g.name
        FROM genres g
        JOIN media_genres mg ON mg.genre_id = g.id
        WHERE mg.media_item_id = ?
        ORDER BY g.name
        """,
        (item_id,),
    ).fetchall()
    return [row["name"] for row in rows]


def _item_tags(item_id):
    rows = get_db().execute(
        """
        SELECT t.name, t.tag_type
        FROM tags t
        JOIN media_tags mt ON mt.tag_id = t.id
        WHERE mt.media_item_id = ?
        ORDER BY t.tag_type, t.name
        """,
        (item_id,),
    ).fetchall()
    return rows


def _database_counts():
    db = get_db()
    return {
        "media_items": db.execute("SELECT COUNT(*) FROM media_items").fetchone()[0],
        "physical_copies": db.execute("SELECT COUNT(*) FROM physical_copies").fetchone()[0],
        "images": db.execute("SELECT COUNT(*) FROM images").fetchone()[0],
        "metadata_sources": db.execute("SELECT COUNT(*) FROM metadata_sources").fetchone()[0],
        "external_links": db.execute("SELECT COUNT(*) FROM external_links").fetchone()[0],
        "barcode_cache": db.execute("SELECT COUNT(*) FROM barcode_cache").fetchone()[0],
    }


def _reset_catalog_data():
    db = get_db()
    for table in (
        "external_links",
        "metadata_sources",
        "media_tags",
        "tags",
        "images",
        "media_genres",
        "genres",
        "physical_copies",
        "media_items",
        "barcode_cache",
    ):
        db.execute(f"DELETE FROM {table}")
    db.commit()


def _candidate_to_save_data(candidate):
    data = dict(candidate)
    data.setdefault("source_url", data.get("remote_url") or "")
    data.setdefault(
        "license_note",
        f"Remote image URL stored from {data.get('source_name') or data.get('barcode_source_name') or 'metadata source'}; not cached locally.",
    )
    data["raw_payload"] = _raw_payload_text(data.get("raw_payload"))
    return data


def _prepare_candidate(candidate):
    if not candidate:
        return None

    candidate = dict(candidate)
    candidate.setdefault("media_kind", "movie")
    candidate.setdefault("barcode", "")
    candidate.setdefault("summary", "")
    candidate.setdefault("rating", "")
    candidate.setdefault("release_year", "")
    candidate.setdefault("remote_url", "")
    candidate.setdefault("source_url", candidate.get("remote_url", ""))
    candidate.setdefault("confidence", "")
    candidate["raw_payload"] = _raw_payload_text(candidate.get("raw_payload"))
    candidate["format_id"] = _infer_format_id(candidate)
    return candidate


def _infer_format_id(candidate):
    text = " ".join(
        str(candidate.get(key) or "")
        for key in ("title", "summary", "category", "media_kind")
    ).lower()

    if "4k" in text or "uhd" in text or "ultra hd" in text:
        preferred = "4K UHD"
    elif "blu-ray" in text or "bluray" in text or "blue ray" in text:
        preferred = "Blu-ray"
    elif "dvd" in text:
        preferred = "DVD"
    elif "vhs" in text or "videocassette" in text:
        preferred = "VHS"
    elif "magazine" in text:
        preferred = "Magazine"
    elif "comic" in text or "manga" in text:
        preferred = "Comic"
    elif "audiobook" in text or "audio book" in text:
        preferred = "Audiobook"
    elif "book" in text:
        preferred = "Book"
    elif "vinyl" in text or "record" in text:
        preferred = "Vinyl"
    elif "cd" in text or "compact disc" in text:
        preferred = "CD"
    elif "cassette" in text:
        preferred = "Cassette"
    else:
        preferred = "Other"

    match = get_db().execute("SELECT id FROM formats WHERE name = ?", (preferred,)).fetchone()
    if match:
        return match["id"]

    fallback = get_db().execute("SELECT id FROM formats WHERE name = 'Other'").fetchone()
    return fallback["id"] if fallback else None


def _dedupe_candidates(candidates):
    seen = set()
    unique = []
    for candidate in candidates:
        title = (candidate.get("title") or "").strip().lower()
        year = candidate.get("release_year") or ""
        source = candidate.get("source_name") or ""
        source_id = candidate.get("source_id") or ""
        key = (source, source_id) if source_id else (title, year)
        if not title or key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique[:24]


def _get_cached_barcode(barcode):
    return get_db().execute(
        "SELECT * FROM barcode_cache WHERE barcode = ?",
        (barcode,),
    ).fetchone()


def _set_cached_barcode(barcode, status, candidate, error_message):
    now = now_iso()
    get_db().execute(
        """
        INSERT INTO barcode_cache (barcode, status, payload, error_message, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(barcode) DO UPDATE SET
            status = excluded.status,
            payload = excluded.payload,
            error_message = excluded.error_message,
            updated_at = excluded.updated_at
        """,
        (
            barcode,
            status,
            json.dumps(candidate) if candidate else None,
            error_message,
            now,
            now,
        ),
    )
    get_db().commit()


def _formats():
    return get_db().execute("SELECT * FROM formats ORDER BY sort_order, name").fetchall()


def _kinds():
    rows = get_db().execute(
        "SELECT DISTINCT media_kind FROM media_items WHERE media_kind IS NOT NULL AND media_kind != '' ORDER BY media_kind"
    ).fetchall()
    return [row["media_kind"] for row in rows]


def _shelves():
    rows = get_db().execute(
        "SELECT DISTINCT shelf_location FROM physical_copies WHERE shelf_location IS NOT NULL AND shelf_location != '' ORDER BY shelf_location"
    ).fetchall()
    return [row["shelf_location"] for row in rows]


def _tags():
    return get_db().execute("SELECT * FROM tags ORDER BY tag_type, name").fetchall()


def _distinct(column):
    rows = get_db().execute(
        f"SELECT DISTINCT {column} AS value FROM media_items WHERE {column} IS NOT NULL AND {column} != '' ORDER BY {column}"
    ).fetchall()
    return [row["value"] for row in rows]


def _years():
    rows = get_db().execute(
        "SELECT DISTINCT release_year FROM media_items WHERE release_year IS NOT NULL ORDER BY release_year DESC"
    ).fetchall()
    return [row["release_year"] for row in rows]


def _allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def _flash_if_possible(message, category):
    if has_request_context():
        flash(message, category)


def _is_barcode_query(query):
    return _clean_barcode(query).isdigit()


def _clean_barcode(query):
    return (query or "").replace("-", "").replace(" ", "").strip()


def _sort_title(title):
    cleaned = (title or "").strip()
    return re.sub(r"^(the|a|an)\s+", "", cleaned, flags=re.IGNORECASE)


def _source_fingerprint(form):
    source_name = form.get("source_name", "").strip()
    source_id = form.get("source_id", "").strip()
    barcode = form.get("barcode", "").strip()
    if source_name and source_id:
        return f"{source_name}:{source_id}"
    if barcode:
        return f"barcode:{barcode}"
    return None


def _raw_payload_text(payload):
    if not payload:
        return None
    if isinstance(payload, str):
        try:
            return json.dumps(json.loads(payload), sort_keys=True)
        except ValueError:
            return payload
    return json.dumps(payload, sort_keys=True)


def _split_values(text):
    return [value.strip() for value in (text or "").split(",") if value.strip()]


def _int_or_none(value):
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _float_or_none(value):
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None
