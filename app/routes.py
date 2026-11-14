import hashlib
import os
import uuid

from flask import (Blueprint, abort, flash, redirect, render_template,
                   request, send_file, url_for)
from werkzeug.utils import secure_filename

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


def _stored_name(filename):
    """Build a collision-proof, path-traversal-safe name inside UPLOAD_FOLDER."""
    safe = secure_filename(filename) or "upload"
    if not safe.lower().endswith(".csv"):
        safe += ".csv"
    return f"{uuid.uuid4().hex}_{safe}"


def _stored_path(stored_name):
    """Resolve a stored file, rejecting any name that escapes UPLOAD_FOLDER."""
    base = os.path.realpath(request_config_folder())
    path = os.path.realpath(os.path.join(base, stored_name))
    if os.path.commonpath([base, path]) != base:
        abort(404)
    return path


def request_config_folder():
    # Indirection keeps the module importable outside an app context.
    from flask import current_app
    return current_app.config["UPLOAD_FOLDER"]


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
    except UploadError as exc:
        # Nothing was retained: a parse failure leaves no downloadable file.
        db.rollback()
        db.execute(
            "INSERT INTO uploads (filename, stored_name, sha256, status, "
            "message, rows_total, rows_applied) "
            "VALUES (?, NULL, ?, 'failed', ?, 0, 0)",
            (filename, sha256, str(exc)),
        )
        db.commit()
        flash(f"上传失败，已入库读数和区间均未改动：{exc}", "error")
        return redirect(url_for("main.index"))

    # Reserve the upload row first so readings can reference its id; nothing is
    # visible as 'applied' until the whole batch commits.
    stored_name = _stored_name(filename)
    cursor = db.execute(
        "INSERT INTO uploads (filename, stored_name, sha256, status, message, "
        "rows_total, rows_applied) VALUES (?, ?, ?, 'pending', NULL, ?, 0)",
        (filename, stored_name, sha256, len(rows)),
    )
    upload_id = cursor.lastrowid

    stored_path = os.path.join(request_config_folder(), stored_name)
    try:
        # Retain the exact original bytes before any DB mutation.
        with open(stored_path, "wb") as fh:
            fh.write(raw)

        stats = apply_rows(db, rows, upload_id)
    except Exception as exc:
        # Atomic failure: roll back every reading/interval/upload change and
        # remove the retained bytes so no orphan file can ever be downloaded.
        db.rollback()
        try:
            os.remove(stored_path)
        except OSError:
            pass
        message = str(exc) if isinstance(exc, UploadError) else "服务器内部错误"
        db.execute(
            "INSERT INTO uploads (filename, stored_name, sha256, status, "
            "message, rows_total, rows_applied) "
            "VALUES (?, NULL, ?, 'failed', ?, ?, 0)",
            (filename, sha256, message, len(rows)),
        )
        db.commit()
        if isinstance(exc, UploadError):
            flash(f"上传失败，已入库读数和区间均未改动：{exc}", "error")
        else:
            flash("上传失败：服务器处理文件时出错，数据未改动", "error")
        return redirect(url_for("main.index"))

    message = (f"应用 {stats['readings_changed']} 条读数变更，涉及 "
               f"{stats['meters']} 个水表，当前共 {stats['intervals']} 个区间")
    db.execute(
        "UPDATE uploads SET status = 'applied', message = ?, "
        "rows_applied = ? WHERE id = ?",
        (message, stats["readings_changed"], upload_id),
    )
    db.commit()

    if stats["readings_changed"] == 0:
        flash(f"{filename}：没有新的读数变更（重复上传不会产生重复区间，"
              "来源关联保持不变）", "info")
    else:
        flash(f"{filename}：{message}", "success")
    return redirect(url_for("main.index"))


@bp.route("/uploads/<int:upload_id>/download")
def download_upload(upload_id):
    db = get_db()
    row = db.execute(
        "SELECT filename, stored_name, status FROM uploads WHERE id = ?",
        (upload_id,),
    ).fetchone()
    # Only successfully applied batches retain a downloadable original file.
    if row is None or row["status"] != "applied" or not row["stored_name"]:
        abort(404)
    path = _stored_path(row["stored_name"])
    if not os.path.isfile(path):
        abort(404)
    download_name = row["filename"] or row["stored_name"]
    return send_file(path, as_attachment=True, download_name=download_name)


@bp.route("/meters/<path:meter_id>")
def meter_detail(meter_id):
    db = get_db()
    readings = db.execute(
        """
        SELECT r.*, u.filename AS source_filename
        FROM readings r
        LEFT JOIN uploads u ON u.id = r.source_upload_id
        WHERE r.meter_id = ?
        ORDER BY r.sampled_at
        """,
        (meter_id,),
    ).fetchall()
    if not readings:
        flash(f"水表 {meter_id} 不存在", "error")
        return redirect(url_for("main.index"))
    # Interval endpoint provenance is derived from the effective readings, so
    # un-replaced endpoints always keep pointing at their original source even
    # after late readings or higher revisions rebuild the intervals.
    intervals = db.execute(
        """
        SELECT i.*,
               rs.source_upload_id AS start_source_upload_id,
               rs.source_line      AS start_source_line,
               us.filename         AS start_source_filename,
               re.source_upload_id AS end_source_upload_id,
               re.source_line      AS end_source_line,
               ue.filename         AS end_source_filename
        FROM intervals i
        JOIN readings rs
          ON rs.meter_id = i.meter_id AND rs.sampled_at = i.start_time
        JOIN readings re
          ON re.meter_id = i.meter_id AND re.sampled_at = i.end_time
        LEFT JOIN uploads us ON us.id = rs.source_upload_id
        LEFT JOIN uploads ue ON ue.id = re.source_upload_id
        WHERE i.meter_id = ?
        ORDER BY i.start_time
        """,
        (meter_id,),
    ).fetchall()
    return render_template(
        "meter.html", meter_id=meter_id, readings=readings,
        intervals=intervals,
    )
