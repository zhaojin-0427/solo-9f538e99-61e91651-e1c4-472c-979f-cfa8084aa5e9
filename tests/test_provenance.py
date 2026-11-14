import io
import os
import sqlite3

import pytest

from app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app({
        "TESTING": True,
        "DATABASE": str(tmp_path / "test.db"),
        "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        "SECRET_KEY": "test",
    })
    with app.test_client() as client:
        yield client, app.config["DATABASE"], app.config["UPLOAD_FOLDER"]


def upload(client, csv_text, filename="data.csv"):
    return client.post(
        "/upload",
        data={"file": (io.BytesIO(csv_text.encode("utf-8")), filename)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def fetch(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def readings(db_path, meter="M1"):
    return fetch(db_path,
                 "SELECT * FROM readings WHERE meter_id=? ORDER BY sampled_at",
                 (meter,))


def intervals(db_path, meter="M1"):
    return fetch(db_path,
                 "SELECT * FROM intervals WHERE meter_id=? ORDER BY start_time",
                 (meter,))


BASE_CSV = (
    "水表号,采样时间,累计读数,修订号\n"
    "M1,2026-01-01 08:00:00,100.0,1\n"
    "M1,2026-02-01 08:00:00,150.0,1\n"
    "M1,2026-03-01 08:00:00,210.0,1\n"
)


# ---------------------------------------------------------------------------
# Provenance: source file + CSV line number
# ---------------------------------------------------------------------------

def test_effective_readings_linked_to_source_file_and_line(client):
    c, db_path, upload_folder = client
    upload(c, BASE_CSV, filename="base.csv")

    rows = readings(db_path)
    assert len(rows) == 3
    up = fetch(db_path, "SELECT * FROM uploads WHERE status='applied'")
    assert len(up) == 1
    upload_id = up[0]["id"]

    # header is line 1, first data row is line 2 ...
    assert [(r["source_upload_id"], r["source_line"]) for r in rows] == [
        (upload_id, 2), (upload_id, 3), (upload_id, 4),
    ]

    # the original file was retained byte-for-byte
    stored = os.listdir(upload_folder)
    assert len(stored) == 1
    with open(os.path.join(upload_folder, stored[0]), "rb") as fh:
        assert fh.read().decode("utf-8") == BASE_CSV


def test_original_file_downloadable(client):
    c, db_path, upload_folder = client
    upload(c, BASE_CSV, filename="base.csv")
    upload_id = fetch(db_path,
                      "SELECT id FROM uploads WHERE status='applied'")[0]["id"]

    resp = c.get(f"/uploads/{upload_id}/download")
    assert resp.status_code == 200
    assert resp.data.decode("utf-8") == BASE_CSV
    # original filename is offered to the browser
    disposition = resp.headers["Content-Disposition"]
    assert "base.csv" in disposition


def test_late_reading_keeps_unreplaced_endpoints_on_original_source(client):
    c, db_path, upload_folder = client
    upload(c, BASE_CSV, filename="base.csv")
    first_id = fetch(db_path,
                     "SELECT id FROM uploads WHERE status='applied'")[0]["id"]

    late_csv = (
        "水表号,采样时间,累计读数,修订号\n"
        "M1,2026-01-15 08:00:00,120.0,1\n"
    )
    upload(c, late_csv, filename="late.csv")
    second_id = fetch(
        db_path,
        "SELECT id FROM uploads WHERE status='applied' ORDER BY id DESC"
    )[0]["id"]
    assert second_id != first_id

    iv = intervals(db_path)
    assert len(iv) == 3

    # first interval: old endpoint -> base file; new endpoint -> late file
    assert iv[0]["start_time"] == "2026-01-01T08:00:00"
    assert iv[0]["end_time"] == "2026-01-15T08:00:00"

    detail = c.get("/meters/M1").get_data(as_text=True)
    src_rows = fetch(
        db_path,
        """
        SELECT r.sampled_at, r.source_upload_id, r.source_line
        FROM readings r ORDER BY r.sampled_at
        """,
    )
    by_time = {r["sampled_at"]: r for r in src_rows}
    assert by_time["2026-01-01T08:00:00"]["source_upload_id"] == first_id
    assert by_time["2026-01-01T08:00:00"]["source_line"] == 2
    assert by_time["2026-01-15T08:00:00"]["source_upload_id"] == second_id
    assert by_time["2026-01-15T08:00:00"]["source_line"] == 2
    # untouched endpoints still point at the original file
    assert by_time["2026-02-01T08:00:00"]["source_upload_id"] == first_id
    assert by_time["2026-03-01T08:00:00"]["source_upload_id"] == first_id

    # both source filenames and line numbers are shown on the detail page
    assert "base.csv" in detail
    assert "late.csv" in detail
    assert "第 2 行" in detail


def test_higher_reversion_rewrites_only_replaced_endpoint_source(client):
    c, db_path, upload_folder = client
    upload(c, BASE_CSV, filename="base.csv")
    first_id = fetch(db_path,
                     "SELECT id FROM uploads WHERE status='applied'")[0]["id"]

    fix_csv = (
        "水表号,采样时间,累计读数,修订号\n"
        "M1,2026-02-01 08:00:00,155.0,2\n"
    )
    upload(c, fix_csv, filename="fix.csv")
    second_id = fetch(
        db_path,
        "SELECT id FROM uploads ORDER BY id DESC LIMIT 1"
    )[0]["id"]

    rows = {r["sampled_at"]: r for r in readings(db_path)}
    feb = rows["2026-02-01T08:00:00"]
    assert feb["revision"] == 2
    assert feb["source_upload_id"] == second_id
    assert feb["source_line"] == 2
    # neighbors keep their original provenance
    assert rows["2026-01-01T08:00:00"]["source_upload_id"] == first_id
    assert rows["2026-03-01T08:00:00"]["source_upload_id"] == first_id

    iv = intervals(db_path)
    assert iv[0]["usage"] == pytest.approx(55.0)
    assert iv[1]["usage"] == pytest.approx(55.0)


def test_same_content_reupload_does_not_rewrite_association(client):
    c, db_path, upload_folder = client
    upload(c, BASE_CSV, filename="base.csv")
    first_id = fetch(db_path,
                     "SELECT id FROM uploads WHERE status='applied'")[0]["id"]
    before = readings(db_path)

    resp = upload(c, BASE_CSV, filename="base-copy.csv")
    assert "没有新的读数变更" in resp.get_data(as_text=True)

    after = readings(db_path)
    assert after == before  # including source_upload_id / source_line
    assert all(r["source_upload_id"] == first_id for r in after)

    # the second upload still retained its own original copy (it succeeded)
    applied = fetch(db_path,
                    "SELECT id, stored_name FROM uploads "
                    "WHERE status='applied' ORDER BY id")
    assert len(applied) == 2
    assert all(u["stored_name"] for u in applied)


def test_failed_batch_leaves_no_orphan_file_and_no_update(client):
    c, db_path, upload_folder = client
    upload(c, BASE_CSV, filename="base.csv")
    before_r = readings(db_path)
    before_i = intervals(db_path)

    bad_csv = (
        "水表号,采样时间,累计读数,修订号\n"
        "M1,2026-02-01 08:00:00,999.0,1\n"  # conflicts with stored rev 1
        "M2,2026-01-01 00:00:00,1.0,1\n"
    )
    resp = upload(c, bad_csv, filename="bad.csv")
    assert "失败" in resp.get_data(as_text=True)

    assert readings(db_path) == before_r
    assert intervals(db_path) == before_i
    assert readings(db_path, "M2") == []

    # only the base file remains; the failed batch retained nothing
    assert os.listdir(upload_folder) and all(
        open(os.path.join(upload_folder, n), "rb").read().decode("utf-8")
        == BASE_CSV
        for n in os.listdir(upload_folder)
    )
    failed = fetch(db_path,
                   "SELECT * FROM uploads WHERE status='failed'")
    assert len(failed) == 1
    assert failed[0]["stored_name"] is None

    # nothing to download for a failed upload
    resp = c.get(f"/uploads/{failed[0]['id']}/download")
    assert resp.status_code == 404


def test_parse_failure_retains_no_file(client):
    c, db_path, upload_folder = client
    upload(c, "水表号,采样时间,累计读数,修订号\nM1,bad-time,1,1\n",
           filename="broken.csv")
    assert os.listdir(upload_folder) == []
    failed = fetch(db_path,
                   "SELECT stored_name FROM uploads WHERE status='failed'")
    assert failed[0]["stored_name"] is None


def test_legacy_rows_have_unknown_source_but_intervals_viewable(client,
                                                                tmp_path):
    # Simulate a database created before traceability existed: readings and
    # intervals with no source columns populated.
    c, db_path, upload_folder = client
    upload(c, BASE_CSV)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE readings SET source_upload_id = NULL, "
                     "source_line = NULL")
        conn.commit()
    finally:
        conn.close()

    rows = readings(db_path)
    assert all(r["source_upload_id"] is None and r["source_line"] is None
               for r in rows)

    resp = c.get("/meters/M1")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "来源未知" in body
    assert "区间用量" in body


def test_old_schema_is_migrated_with_unknown_sources(tmp_path):
    # A database file created by the pre-feature schema (no source columns,
    # no uploads.stored_name) must be migrated automatically at startup.
    legacy_db = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy_db)
    conn.executescript(
        """
        CREATE TABLE readings (
            meter_id TEXT NOT NULL, sampled_at TEXT NOT NULL,
            reading REAL NOT NULL, revision INTEGER NOT NULL,
            PRIMARY KEY (meter_id, sampled_at)
        );
        CREATE TABLE intervals (
            meter_id TEXT NOT NULL, start_time TEXT NOT NULL,
            end_time TEXT NOT NULL, start_reading REAL NOT NULL,
            end_reading REAL NOT NULL, start_revision INTEGER NOT NULL,
            end_revision INTEGER NOT NULL, usage REAL NOT NULL,
            PRIMARY KEY (meter_id, start_time, end_time)
        );
        CREATE TABLE uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, sha256 TEXT,
            status TEXT NOT NULL, message TEXT, rows_total INTEGER NOT NULL,
            rows_applied INTEGER NOT NULL,
            uploaded_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        INSERT INTO readings VALUES ('M9', '2026-01-01T08:00:00', 10.0, 1);
        INSERT INTO readings VALUES ('M9', '2026-02-01T08:00:00', 18.0, 1);
        INSERT INTO intervals VALUES ('M9', '2026-01-01T08:00:00',
            '2026-02-01T08:00:00', 10.0, 18.0, 1, 1, 8.0);
        """
    )
    conn.commit()
    conn.close()

    app = create_app({
        "TESTING": True,
        "DATABASE": str(legacy_db),
        "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        "SECRET_KEY": "test",
    })
    with app.test_client() as c:
        body = c.get("/meters/M9").get_data(as_text=True)
        assert "来源未知" in body
        assert "8.000" in body

        # new uploads on top of legacy data work and get full provenance
        upload(c, "水表号,采样时间,累计读数,修订号\n"
                  "M9,2026-03-01 08:00:00,30.0,1\n", filename="new.csv")
    conn = sqlite3.connect(legacy_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = {r["sampled_at"]: dict(r) for r in conn.execute(
            "SELECT * FROM readings ORDER BY sampled_at")}
        assert rows["2026-01-01T08:00:00"]["source_upload_id"] is None
        assert rows["2026-03-01T08:00:00"]["source_upload_id"] is not None
    finally:
        conn.close()


def test_download_rejects_missing_and_foreign_names(client):
    c, db_path, upload_folder = client
    upload(c, BASE_CSV)
    assert c.get("/uploads/99999/download").status_code == 404
    # path traversal attempts must not escape the upload folder
    resp = c.get("/uploads/..%2f..%2f..%2fetc%2fpasswd/download")
    assert resp.status_code in (404, 308)
