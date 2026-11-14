import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    meter_id      TEXT NOT NULL,
    sampled_at    TEXT NOT NULL,        -- normalized ISO: YYYY-MM-DDTHH:MM:SS
    reading       REAL NOT NULL,        -- cumulative meter reading
    revision      INTEGER NOT NULL,     -- revision number for this meter+time
    source_upload_id INTEGER,           -- upload that first introduced this revision
    source_line   INTEGER,              -- CSV line number inside that upload
    PRIMARY KEY (meter_id, sampled_at)
);

CREATE TABLE IF NOT EXISTS intervals (
    meter_id       TEXT NOT NULL,
    start_time     TEXT NOT NULL,
    end_time       TEXT NOT NULL,
    start_reading  REAL NOT NULL,
    end_reading    REAL NOT NULL,
    start_revision INTEGER NOT NULL,
    end_revision   INTEGER NOT NULL,
    usage          REAL NOT NULL,
    PRIMARY KEY (meter_id, start_time, end_time)
);

CREATE TABLE IF NOT EXISTS uploads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    filename     TEXT,
    stored_name  TEXT,                  -- retained original file inside UPLOAD_FOLDER
    sha256       TEXT,
    status       TEXT NOT NULL,         -- 'applied' or 'failed'
    message      TEXT,
    rows_total   INTEGER NOT NULL DEFAULT 0,
    rows_applied INTEGER NOT NULL DEFAULT 0,
    uploaded_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

# Columns added after the initial release; existing databases are migrated in
# place. Rows predating the traceability feature keep NULL sources, which the
# UI renders as “来源未知”.
MIGRATIONS = (
    ("readings", "source_upload_id",
     "ALTER TABLE readings ADD COLUMN source_upload_id INTEGER"),
    ("readings", "source_line",
     "ALTER TABLE readings ADD COLUMN source_line INTEGER"),
    ("uploads", "stored_name",
     "ALTER TABLE uploads ADD COLUMN stored_name TEXT"),
)


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(exception=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _migrate(conn):
    for table, column, ddl in MIGRATIONS:
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            conn.execute(ddl)


def init_schema(app):
    """Create tables if they do not exist yet (called once at startup)."""
    with app.app_context():
        conn = get_db()
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
    init_schema(app)
