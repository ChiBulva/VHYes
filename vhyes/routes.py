import os
import uuid

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

from .db import get_db, now_iso, row_to_dict
from .metadata import lookup_barcode, search_tmdb

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


@bp.route("/add", methods=("GET", "POST"))
def add_media():
    candidate = None
    if request.method == "POST" and request.form.get("lookup_action"):
        candidate = _lookup_candidate(request.form)
        if not candidate:
            flash("No metadata match found. You can still add it manually.", "warn")

    if request.method == "POST" and request.form.get("save_action"):
        item_id = _save_media(request.form, request.files.get("cover_file"))
        flash("Media added.", "success")
        return redirect(url_for("vhyes.detail", item_id=item_id))

    return render_template(
        "add.html",
        formats=_formats(),
        candidate=candidate,
        form=request.form,
    )


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


@bp.route("/covers/<path:filename>")
def cover_file(filename):
    return send_from_directory(current_app.config["COVERS_DIR"], filename)


def _lookup_candidate(form):
    barcode = form.get("barcode", "").strip()
    title = form.get("title", "").strip()

    try:
        if barcode:
            candidate = lookup_barcode(
                barcode,
                current_app.config["BARCODE_LOOKUP_API_KEY"],
            )
            if candidate:
                return candidate

        results = search_tmdb(title, current_app.config["TMDB_API_KEY"])
        return results[0] if results else None
    except Exception as exc:
        flash(f"Lookup failed: {exc}", "error")
        return None


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

    remote_url = form.get("remote_url", "").strip() or None
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
                form.get("source_url", "").strip() or None,
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
