import hashlib

from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for)

from .db import get_db
from .logic import UploadError, apply_rows, parse_csv

bp = Blueprint("main", __name__)


def _decode(raw):
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UploadError("文件编码无法识别（支持 UTF-8 / GB18030）")


@bp.route("/")
def index():
    db = get_db()
    meters = db.execute(
        """
        SELECT r.meter_id,
               COUNT(*) AS reading_count,
               MIN(r.sampled_at) AS first_at,
               MAX(r.sampled_at) AS last_at,
               (SELECT COUNT(*) FROM intervals i
                 WHERE i.meter_id = r.meter_id) AS interval_count,
               (SELECT ROUND(SUM(i.usage), 3) FROM intervals i
                 WHERE i.meter_id = r.meter_id) AS total_usage
        FROM readings r
        GROUP BY r.meter_id
        ORDER BY r.meter_id
        """
    ).fetchall()
    uploads = db.execute(
        "SELECT * FROM uploads ORDER BY id DESC LIMIT 20"
    ).fetchall()
    return render_template("index.html", meters=meters, uploads=uploads)


@bp.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("file")
    if file is None or not file.filename:
        flash("请选择要上传的 CSV 文件", "error")
        return redirect(url_for("main.index"))

    raw = file.read()
    sha256 = hashlib.sha256(raw).hexdigest()
    filename = file.filename

    db = get_db()
    try:
        rows = parse_csv(_decode(raw))
        stats = apply_rows(db, rows)
    except UploadError as exc:
        db.rollback()  # readings & intervals stay exactly as before
        db.execute(
            "INSERT INTO uploads (filename, sha256, status, message, "
            "rows_total, rows_applied) VALUES (?, ?, 'failed', ?, 0, 0)",
            (filename, sha256, str(exc)),
        )
        db.commit()
        flash(f"上传失败，已入库读数和区间均未改动：{exc}", "error")
        return redirect(url_for("main.index"))

    message = (f"应用 {stats['readings_changed']} 条读数变更，涉及 "
               f"{stats['meters']} 个水表，当前共 {stats['intervals']} 个区间")
    db.execute(
        "INSERT INTO uploads (filename, sha256, status, message, rows_total, "
        "rows_applied) VALUES (?, ?, 'applied', ?, ?, ?)",
        (filename, sha256, message, len(rows), stats["readings_changed"]),
    )
    db.commit()

    if stats["readings_changed"] == 0:
        flash(f"{filename}：没有新的读数变更（重复上传不会产生重复区间）", "info")
    else:
        flash(f"{filename}：{message}", "success")
    return redirect(url_for("main.index"))


@bp.route("/meters/<path:meter_id>")
def meter_detail(meter_id):
    db = get_db()
    readings = db.execute(
        "SELECT * FROM readings WHERE meter_id = ? ORDER BY sampled_at",
        (meter_id,),
    ).fetchall()
    if not readings:
        flash(f"水表 {meter_id} 不存在", "error")
        return redirect(url_for("main.index"))
    intervals = db.execute(
        "SELECT * FROM intervals WHERE meter_id = ? ORDER BY start_time",
        (meter_id,),
    ).fetchall()
    return render_template(
        "meter.html", meter_id=meter_id, readings=readings,
        intervals=intervals,
    )
