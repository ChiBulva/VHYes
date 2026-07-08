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
    media_kind TEXT NOT NULL DEFAULT 'movie',
    release_year INTEGER,
    runtime_minutes INTEGER,
    rating REAL,
    personal_rating REAL,
    mood TEXT,
    summary TEXT,
    source_name TEXT,
    source_id TEXT,
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

CREATE TABLE IF NOT EXISTS barcode_cache (
    barcode TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    payload TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_title ON media_items(title);
CREATE INDEX IF NOT EXISTS idx_media_year ON media_items(release_year);
CREATE INDEX IF NOT EXISTS idx_media_mood ON media_items(mood);
CREATE INDEX IF NOT EXISTS idx_copies_barcode ON physical_copies(barcode);
"""

DEFAULT_FORMATS = [
    ("VHS", 10),
    ("DVD", 20),
    ("Blu-ray", 30),
    ("4K UHD", 40),
    ("LaserDisc", 50),
    ("Book", 60),
    ("CD", 70),
    ("Cassette", 80),
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
    db.executemany(
        "INSERT OR IGNORE INTO formats (name, sort_order) VALUES (?, ?)",
        DEFAULT_FORMATS,
    )
    db.commit()


def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def row_to_dict(row):
    return dict(row) if row is not None else None
