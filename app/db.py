import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    meter_id   TEXT NOT NULL,
    sampled_at TEXT NOT NULL,          -- normalized ISO: YYYY-MM-DDTHH:MM:SS
    reading    REAL NOT NULL,          -- cumulative meter reading
    revision   INTEGER NOT NULL,       -- revision number for this meter+time
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
    sha256       TEXT,
    status       TEXT NOT NULL,        -- 'applied' or 'failed'
    message      TEXT,
    rows_total   INTEGER NOT NULL DEFAULT 0,
    rows_applied INTEGER NOT NULL DEFAULT 0,
    uploaded_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


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


def init_schema(app):
    """Create tables if they do not exist yet (called once at startup)."""
    with app.app_context():
        conn = get_db()
        conn.executescript(SCHEMA)
        conn.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
    init_schema(app)
