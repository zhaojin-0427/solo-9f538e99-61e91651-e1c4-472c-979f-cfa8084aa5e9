import io
import sqlite3

import pytest

from app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app({
        "TESTING": True,
        "DATABASE": str(tmp_path / "test.db"),
        "SECRET_KEY": "test",
    })
    with app.test_client() as client:
        yield client, app.config["DATABASE"]


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


def test_basic_upload_builds_intervals(client):
    client, db_path = client
    resp = upload(client, BASE_CSV)
    assert resp.status_code == 200

    rows = readings(db_path)
    assert len(rows) == 3

    iv = intervals(db_path)
    assert len(iv) == 2
    assert iv[0]["start_time"] == "2026-01-01T08:00:00"
    assert iv[0]["end_time"] == "2026-02-01T08:00:00"
    assert iv[0]["start_reading"] == 100.0
    assert iv[0]["end_reading"] == 150.0
    assert iv[0]["start_revision"] == 1
    assert iv[0]["end_revision"] == 1
    assert iv[0]["usage"] == pytest.approx(50.0)
    assert iv[1]["usage"] == pytest.approx(60.0)


def test_english_headers_accepted(client):
    client, db_path = client
    resp = upload(client,
                  "meter_id,sampled_at,reading,revision\n"
                  "M1,2026-01-01T00:00:00,5,0\n"
                  "M1,2026-01-02T00:00:00,9,0\n")
    assert resp.status_code == 200
    assert len(readings(db_path)) == 2
    assert intervals(db_path)[0]["usage"] == pytest.approx(4.0)


def test_highest_revision_wins(client):
    client, db_path = client
    upload(client, BASE_CSV)
    # rev 2 corrects the Feb reading; a stale rev 1 duplicate must be ignored
    upload(client,
           "水表号,采样时间,累计读数,修订号\n"
           "M1,2026-02-01 08:00:00,155.0,2\n"
           "M1,2026-02-01 08:00:00,150.0,1\n")
    rows = readings(db_path)
    feb = [r for r in rows if r["sampled_at"] == "2026-02-01T08:00:00"]
    assert len(feb) == 1
    assert feb[0]["reading"] == 155.0
    assert feb[0]["revision"] == 2

    iv = intervals(db_path)
    assert iv[0]["usage"] == pytest.approx(55.0)   # 155 - 100
    assert iv[1]["usage"] == pytest.approx(55.0)   # 210 - 155
    assert iv[0]["end_revision"] == 2


def test_late_reading_changes_neighbouring_intervals(client):
    client, db_path = client
    upload(client, BASE_CSV)
    # late January-15 reading splits the Jan→Feb interval
    upload(client,
           "水表号,采样时间,累计读数,修订号\n"
           "M1,2026-01-15 08:00:00,120.0,1\n")
    iv = intervals(db_path)
    assert len(iv) == 3
    assert [(i["start_time"], i["end_time"]) for i in iv] == [
        ("2026-01-01T08:00:00", "2026-01-15T08:00:00"),
        ("2026-01-15T08:00:00", "2026-02-01T08:00:00"),
        ("2026-02-01T08:00:00", "2026-03-01T08:00:00"),
    ]
    assert [i["usage"] for i in iv] == [pytest.approx(20.0),
                                        pytest.approx(30.0),
                                        pytest.approx(60.0)]


def test_conflicting_revision_fails_whole_batch(client):
    client, db_path = client
    upload(client, BASE_CSV)
    before_r = readings(db_path)
    before_i = intervals(db_path)

    # same meter+time+revision but a different reading; also carries a valid
    # new row for another meter which must NOT be persisted either
    resp = upload(client,
                  "水表号,采样时间,累计读数,修订号\n"
                  "M1,2026-02-01 08:00:00,999.0,1\n"
                  "M2,2026-01-01 00:00:00,1.0,1\n")
    assert resp.status_code == 200
    assert "失败" in resp.get_data(as_text=True)

    assert readings(db_path) == before_r
    assert intervals(db_path) == before_i
    assert readings(db_path, "M2") == []  # nothing leaked into the DB


def test_conflict_inside_file_fails(client):
    client, db_path = client
    upload(client, BASE_CSV)
    before_r = readings(db_path)
    resp = upload(client,
                  "水表号,采样时间,累计读数,修订号\n"
                  "M3,2026-01-01 00:00:00,10.0,1\n"
                  "M3,2026-01-01 00:00:00,11.0,1\n")
    assert "失败" in resp.get_data(as_text=True)
    assert readings(db_path) == before_r
    assert readings(db_path, "M3") == []


def test_negative_usage_fails_whole_batch(client):
    client, db_path = client
    upload(client, BASE_CSV)
    before_r = readings(db_path)
    before_i = intervals(db_path)

    # a correction that drops the Feb reading below the Jan one
    resp = upload(client,
                  "水表号,采样时间,累计读数,修订号\n"
                  "M1,2026-02-01 08:00:00,90.0,2\n")
    assert "负用量" in resp.get_data(as_text=True)
    assert readings(db_path) == before_r
    assert intervals(db_path) == before_i


def test_reupload_same_file_is_idempotent(client):
    client, db_path = client
    upload(client, BASE_CSV)
    first_r = readings(db_path)
    first_i = intervals(db_path)

    resp = upload(client, BASE_CSV)
    assert "没有新的读数变更" in resp.get_data(as_text=True)
    assert readings(db_path) == first_r
    assert intervals(db_path) == first_i
    assert len(intervals(db_path)) == 2  # no duplicates


def test_failed_upload_is_recorded(client):
    client, db_path = client
    upload(client, "水表号,采样时间,累计读数,修订号\nM1,bad-time,1,1\n")
    ups = fetch(db_path, "SELECT * FROM uploads")
    assert len(ups) == 1
    assert ups[0]["status"] == "failed"


def test_pages_render(client):
    client, db_path = client
    upload(client, BASE_CSV)
    home = client.get("/")
    assert home.status_code == 200
    assert "M1" in home.get_data(as_text=True)
    detail = client.get("/meters/M1")
    body = detail.get_data(as_text=True)
    assert detail.status_code == 200
    assert "区间用量" in body
    assert "修订 1" in body  # endpoint revisions are displayed
