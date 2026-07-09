import sqlite3
from datetime import datetime

from flask import current_app, g


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS formats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS media_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    sort_title TEXT,
    media_kind TEXT NOT NULL DEFAULT 'movie',
    release_year INTEGER,
    runtime_minutes INTEGER,
    rating REAL,
    personal_rating REAL,
    mood TEXT,
    mood_summary TEXT,
    summary TEXT,
    filter_notes TEXT,
    extra_info TEXT,
    source_name TEXT,
    source_id TEXT,
    source_fingerprint TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS physical_copies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
    format_id INTEGER REFERENCES formats(id),
    barcode TEXT,
    edition TEXT,
    shelf_location TEXT,
    condition_note TEXT,
    purchase_price REAL,
    estimated_value REAL,
    acquired_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS genres (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS media_genres (
    media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
    genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
    PRIMARY KEY (media_item_id, genre_id)
);

CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
    image_type TEXT NOT NULL DEFAULT 'cover',
    local_path TEXT,
    remote_url TEXT,
    source_name TEXT,
    source_url TEXT,
    license_note TEXT,
    created_at TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS imdb_titles (
    tconst TEXT PRIMARY KEY,
    title_type TEXT NOT NULL,
    primary_title TEXT NOT NULL,
    original_title TEXT,
    start_year INTEGER,
    runtime_minutes INTEGER,
    genres TEXT,
    average_rating REAL,
    num_votes INTEGER,
    imported_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS imdb_title_fts USING fts5(
    tconst UNINDEXED,
    title,
    primary_title UNINDEXED,
    title_type UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS imdb_imports (
    source TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS barcode_cache (
    barcode TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    payload TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
    source_name TEXT NOT NULL,
    source_id TEXT,
    source_url TEXT,
    raw_payload TEXT,
    confidence REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS external_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    url TEXT NOT NULL,
    source_name TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    tag_type TEXT NOT NULL DEFAULT 'filter',
    UNIQUE(name, tag_type)
);

CREATE TABLE IF NOT EXISTS media_tags (
    media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (media_item_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_media_title ON media_items(title);
CREATE INDEX IF NOT EXISTS idx_media_year ON media_items(release_year);
CREATE INDEX IF NOT EXISTS idx_media_mood ON media_items(mood);
CREATE INDEX IF NOT EXISTS idx_copies_barcode ON physical_copies(barcode);
CREATE INDEX IF NOT EXISTS idx_metadata_sources_item ON metadata_sources(media_item_id);
CREATE INDEX IF NOT EXISTS idx_external_links_item ON external_links(media_item_id);

CREATE INDEX IF NOT EXISTS idx_imdb_titles_year ON imdb_titles(start_year);
CREATE INDEX IF NOT EXISTS idx_imdb_titles_votes ON imdb_titles(num_votes);
"""

DEFAULT_FORMATS = [
    ("VHS", 10),
    ("DVD", 20),
    ("Blu-ray", 30),
    ("4K UHD", 40),
    ("LaserDisc", 50),
    ("Book", 60),
    ("Magazine", 70),
    ("Audiobook", 80),
    ("CD", 90),
    ("Vinyl", 100),
    ("Cassette", 110),
    ("Comic", 120),
    ("Other", 999),
]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    _ensure_columns(db)
    db.execute("CREATE INDEX IF NOT EXISTS idx_media_sort_title ON media_items(sort_title)")
    db.executemany(
        "INSERT OR IGNORE INTO formats (name, sort_order) VALUES (?, ?)",
        DEFAULT_FORMATS,
    )
    db.commit()


def _ensure_columns(db):
    _add_missing_columns(
        db,
        "media_items",
        {
            "sort_title": "TEXT",
            "mood_summary": "TEXT",
            "filter_notes": "TEXT",
            "extra_info": "TEXT",
            "source_fingerprint": "TEXT",
        },
    )
    _add_missing_columns(
        db,
        "physical_copies",
        {
            "purchase_price": "REAL",
            "estimated_value": "REAL",
        },
    )


def _add_missing_columns(db, table, columns):
    existing = {
        row["name"]
        for row in db.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, definition in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def row_to_dict(row):
    return dict(row) if row is not None else None
