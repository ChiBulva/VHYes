import json
import os
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
from .metadata import BarcodeLookupError, BarcodeRateLimitError, is_physical_media_candidate, lookup_barcode, search_open_library, search_tmdb, search_wikidata

bp = Blueprint("vhyes", __name__)

ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}


@bp.route("/")
def library():
    db = get_db()
    filters = {
        "q": request.args.get("q", "").strip(),
        "format": request.args.get("format", "").strip(),
        "mood": request.args.get("mood", "").strip(),
        "year": request.args.get("year", "").strip(),
        "rating": request.args.get("rating", "").strip(),
    }

    where = []
    params = []
    if filters["q"]:
        where.append("(mi.title LIKE ? OR mi.summary LIKE ?)")
        params.extend([f"%{filters['q']}%", f"%{filters['q']}%"])
    if filters["format"]:
        where.append("f.name = ?")
        params.append(filters["format"])
    if filters["mood"]:
        where.append("mi.mood = ?")
        params.append(filters["mood"])
    if filters["year"]:
        where.append("mi.release_year = ?")
        params.append(filters["year"])
    if filters["rating"]:
        where.append("COALESCE(mi.personal_rating, mi.rating, 0) >= ?")
        params.append(filters["rating"])

    where_sql = "WHERE " + " AND ".join(where) if where else ""
    items = db.execute(
        f"""
        SELECT
            mi.*,
            f.name AS format_name,
            pc.barcode,
            pc.shelf_location,
            img.local_path,
            img.remote_url
        FROM media_items mi
        LEFT JOIN physical_copies pc ON pc.media_item_id = mi.id
        LEFT JOIN formats f ON f.id = pc.format_id
        LEFT JOIN images img ON img.media_item_id = mi.id AND img.image_type = 'cover'
        {where_sql}
        GROUP BY mi.id
        ORDER BY mi.created_at DESC
        """,
        params,
    ).fetchall()

    return render_template(
        "library.html",
        items=items,
        filters=filters,
        formats=_formats(),
        moods=_distinct("mood"),
        years=_years(),
    )


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


def _database_counts():
    db = get_db()
    return {
        "media_items": db.execute("SELECT COUNT(*) FROM media_items").fetchone()[0],
        "physical_copies": db.execute("SELECT COUNT(*) FROM physical_copies").fetchone()[0],
        "images": db.execute("SELECT COUNT(*) FROM images").fetchone()[0],
        "barcode_cache": db.execute("SELECT COUNT(*) FROM barcode_cache").fetchone()[0],
    }


def _reset_catalog_data():
    db = get_db()
    for table in (
        "images",
        "media_genres",
        "genres",
        "physical_copies",
        "media_items",
        "barcode_cache",
    ):
        db.execute(f"DELETE FROM {table}")
    db.commit()


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


@bp.route("/media/<int:item_id>")
def detail(item_id):
    db = get_db()
    item = db.execute(
        """
        SELECT mi.*, f.name AS format_name, pc.barcode, pc.edition, pc.shelf_location,
               pc.condition_note, img.local_path, img.remote_url, img.source_name,
               img.source_url, img.license_note
        FROM media_items mi
        LEFT JOIN physical_copies pc ON pc.media_item_id = mi.id
        LEFT JOIN formats f ON f.id = pc.format_id
        LEFT JOIN images img ON img.media_item_id = mi.id AND img.image_type = 'cover'
        WHERE mi.id = ?
        """,
        (item_id,),
    ).fetchone()
    if item is None:
        return "Not found", 404

    genres = db.execute(
        """
        SELECT g.name
        FROM genres g
        JOIN media_genres mg ON mg.genre_id = g.id
        WHERE mg.media_item_id = ?
        ORDER BY g.name
        """,
        (item_id,),
    ).fetchall()
    return render_template("detail.html", item=item, genres=[g["name"] for g in genres])


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


def _search_candidates(query):
    query = (query or "").strip()
    if not query:
        return []

    if _is_barcode_query(query):
        barcode = _clean_barcode(query)
        cached = _get_cached_barcode(barcode)
        if cached:
            if cached["status"] == "match":
                return [_prepare_candidate(json.loads(cached["payload"]))]
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

        _set_cached_barcode(barcode, "match", candidate, None)
        return [_prepare_candidate(candidate)]

    candidates = []
    candidates.extend(search_tmdb(query, current_app.config["TMDB_API_KEY"]))
    candidates.extend(search_open_library(query))
    candidates.extend(search_wikidata(query))
    return [_prepare_candidate(candidate) for candidate in _dedupe_candidates(candidates)]


def _flash_if_possible(message, category):
    if has_request_context():
        flash(message, category)


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


def _candidate_to_save_data(candidate):
    data = dict(candidate)
    data.setdefault("source_url", data.get("remote_url") or "")
    data.setdefault(
        "license_note",
        f"Remote image URL stored from {data.get('source_name') or 'metadata source'}; not cached locally.",
    )
    return data


def _is_barcode_query(query):
    return _clean_barcode(query).isdigit()


def _clean_barcode(query):
    return (query or "").replace("-", "").replace(" ", "").strip()


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
    candidate["format_id"] = _infer_format_id(candidate)
    return candidate


def _infer_format_id(candidate):
    text = " ".join(
        str(candidate.get(key) or "")
        for key in ("title", "summary")
    ).lower()

    if "4k" in text or "uhd" in text or "ultra hd" in text:
        preferred = "4K UHD"
    elif "blu-ray" in text or "bluray" in text or "blue ray" in text:
        preferred = "Blu-ray"
    elif "dvd" in text:
        preferred = "DVD"
    elif "vhs" in text or "videocassette" in text:
        preferred = "VHS"
    else:
        preferred = "Other"

    match = get_db().execute("SELECT id FROM formats WHERE name = ?", (preferred,)).fetchone()
    if match:
        return match["id"]

    fallback = get_db().execute("SELECT id FROM formats WHERE name = 'Other'").fetchone()
    return fallback["id"] if fallback else None


def _save_media(form, cover_file):
    db = get_db()
    now = now_iso()

    cursor = db.execute(
        """
        INSERT INTO media_items (
            title, media_kind, release_year, runtime_minutes, rating,
            personal_rating, mood, summary, source_name, source_id,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            form.get("title", "").strip(),
            form.get("media_kind", "movie"),
            _int_or_none(form.get("release_year")),
            _int_or_none(form.get("runtime_minutes")),
            _float_or_none(form.get("rating")),
            _float_or_none(form.get("personal_rating")),
            form.get("mood", "").strip() or None,
            form.get("summary", "").strip() or None,
            form.get("source_name", "").strip() or None,
            form.get("source_id", "").strip() or None,
            now,
            now,
        ),
    )
    item_id = cursor.lastrowid

    db.execute(
        """
        INSERT INTO physical_copies (
            media_item_id, format_id, barcode, edition, shelf_location,
            condition_note, acquired_at, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            _int_or_none(form.get("format_id")),
            form.get("barcode", "").strip() or None,
            form.get("edition", "").strip() or None,
            form.get("shelf_location", "").strip() or None,
            form.get("condition_note", "").strip() or None,
            form.get("acquired_at", "").strip() or None,
            now,
        ),
    )

    _save_genres(db, item_id, form.get("genres", ""))
    _save_image(db, item_id, form, cover_file, now)

    db.commit()
    return item_id


def _save_genres(db, item_id, genres_text):
    names = [name.strip() for name in genres_text.split(",") if name.strip()]
    for name in names:
        db.execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (name,))
        genre = db.execute("SELECT id FROM genres WHERE name = ?", (name,)).fetchone()
        db.execute(
            "INSERT OR IGNORE INTO media_genres (media_item_id, genre_id) VALUES (?, ?)",
            (item_id, genre["id"]),
        )


def _save_image(db, item_id, form, upload, now):
    local_path = None
    if upload and upload.filename and _allowed_image(upload.filename):
        filename = secure_filename(upload.filename)
        ext = filename.rsplit(".", 1)[1].lower()
        stored_name = f"{uuid.uuid4().hex}.{ext}"
        upload.save(os.path.join(current_app.config["COVERS_DIR"], stored_name))
        local_path = stored_name

    remote_url = form.get("image_url_override", "").strip() or form.get("remote_url", "").strip() or None
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


def _allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def _formats():
    return get_db().execute("SELECT * FROM formats ORDER BY sort_order, name").fetchall()


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
