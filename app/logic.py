"""CSV parsing and the merge / interval-recomputation rules.

Rules implemented here:
* For the same meter and timestamp only the highest revision is kept.
* Same meter + timestamp + revision mapping to different readings (either
  inside the uploaded file or against stored data) fails the whole batch.
* Late readings and corrections recompute the surrounding intervals.
* If merging would produce a negative usage interval, the whole batch fails.
* Re-uploading the same file is a no-op: no duplicate readings or intervals,
  and the provenance (first upload that received a revision) is preserved.

Provenance:
* Every effective reading remembers the upload id and CSV line number of the
  file that first introduced its currently-effective revision.
* When a late reading or a higher revision rebuilds intervals, endpoints that
  were not replaced keep pointing at their original source.
* Re-uploading identical content does not rewrite the association.
* Readings predating this feature have NULL sources ("来源未知").

All DB mutations happen on the caller's connection; the caller controls the
transaction so a failure leaves stored readings and intervals untouched.
"""

import csv
import io
from dataclasses import dataclass
from datetime import datetime

EPS = 1e-9


class UploadError(Exception):
    """Raised when a batch must be rejected; message is shown to the user."""


@dataclass
class Row:
    meter_id: str
    sampled_at: str  # normalized ISO timestamp
    reading: float
    revision: int
    line: int


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

HEADER_ALIASES = {
    "meter_id": {"meter_id", "meter", "meterid", "meter_no", "meter_number",
                 "水表号", "水表编号", "表号"},
    "sampled_at": {"sampled_at", "timestamp", "time", "sample_time",
                   "采样时间", "时间", "读数时间"},
    "reading": {"reading", "cumulative_reading", "value", "total_reading",
                "累计读数", "读数", "累计流量"},
    "revision": {"revision", "rev", "revision_no", "version",
                 "修订号", "修订版本", "版本号"},
}

TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
)


def _normalize_header(name):
    return (name or "").strip().lower()


def _map_headers(fieldnames):
    mapping = {}
    for canonical, aliases in HEADER_ALIASES.items():
        for raw in fieldnames or []:
            if _normalize_header(raw) in aliases:
                mapping[canonical] = raw
                break
    missing = [k for k in ("meter_id", "sampled_at", "reading", "revision")
               if k not in mapping]
    if missing:
        raise UploadError(
            "CSV 缺少必需列：{}（需要 水表号/采样时间/累计读数/修订号 四列）".format(
                "、".join(missing)
            )
        )
    return mapping


def _parse_time(raw, line):
    s = (raw or "").strip()
    if not s:
        raise UploadError(f"第 {line} 行：采样时间为空")
    for fmt in TIME_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise UploadError(f"第 {line} 行：无法解析采样时间 {s!r}")
    if dt.tzinfo is not None:
        raise UploadError(f"第 {line} 行：采样时间不支持时区偏移 {s!r}")
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_reading(raw, line):
    s = (raw or "").strip()
    if not s:
        raise UploadError(f"第 {line} 行：累计读数为空")
    try:
        value = float(s)
    except ValueError:
        raise UploadError(f"第 {line} 行：累计读数不是数字 {s!r}")
    if value != value or value in (float("inf"), float("-inf")):
        raise UploadError(f"第 {line} 行：累计读数非法 {s!r}")
    return value


def _parse_revision(raw, line):
    s = (raw or "").strip()
    if not s:
        raise UploadError(f"第 {line} 行：修订号为空")
    try:
        return int(s)
    except ValueError:
        try:
            f = float(s)
        except ValueError:
            raise UploadError(f"第 {line} 行：修订号不是整数 {s!r}")
        if f.is_integer():
            return int(f)
        raise UploadError(f"第 {line} 行：修订号不是整数 {s!r}")


def parse_csv(text):
    """Parse CSV text into a list of Row; raises UploadError on any problem."""
    reader = csv.DictReader(io.StringIO(text))
    mapping = _map_headers(reader.fieldnames)
    rows = []
    for record in reader:
        line = reader.line_num
        values = [(record.get(mapping[k]) or "").strip()
                  for k in ("meter_id", "sampled_at", "reading", "revision")]
        if not any(values):
            continue  # tolerate blank lines
        meter_id = values[0]
        if not meter_id:
            raise UploadError(f"第 {line} 行：水表号为空")
        rows.append(Row(
            meter_id=meter_id,
            sampled_at=_parse_time(values[1], line),
            reading=_parse_reading(values[2], line),
            revision=_parse_revision(values[3], line),
            line=line,
        ))
    if not rows:
        raise UploadError("CSV 中没有数据行")
    return rows


# ---------------------------------------------------------------------------
# Merge + interval recomputation
# ---------------------------------------------------------------------------

def _check_internal_conflicts(rows):
    seen = {}
    for r in rows:
        key = (r.meter_id, r.sampled_at, r.revision)
        if key in seen and abs(seen[key] - r.reading) > EPS:
            raise UploadError(
                f"文件内部冲突：水表 {r.meter_id} 在 {r.sampled_at} 的修订号 "
                f"{r.revision} 对应两个不同读数（{seen[key]} 与 {r.reading}）"
            )
        seen[key] = r.reading


def apply_rows(conn, rows, upload_id):
    """Merge parsed rows into `conn` and rebuild intervals for affected meters.

    `upload_id` is the uploads row of the retained source file; every reading
    whose effective revision is introduced by this batch is associated with
    that upload and the row's CSV line number.

    Raises UploadError on conflict or negative usage; the caller must roll
    back the transaction so stored readings/intervals stay unchanged.
    Returns a stats dict on success.
    """
    _check_internal_conflicts(rows)

    by_meter = {}
    for r in rows:
        by_meter.setdefault(r.meter_id, []).append(r)

    stats = {"meters": len(by_meter), "readings_changed": 0, "intervals": 0}

    for meter_id in sorted(by_meter):
        incoming = by_meter[meter_id]

        # Current effective readings for this meter:
        # time -> (reading, revision, source_upload_id, source_line)
        effective = {
            row["sampled_at"]: (row["reading"], row["revision"],
                                row["source_upload_id"], row["source_line"])
            for row in conn.execute(
                "SELECT sampled_at, reading, revision, source_upload_id, "
                "source_line FROM readings WHERE meter_id = ?", (meter_id,))
        }
        before = dict(effective)

        # Conflict with stored data: same meter+time+revision, different value.
        for r in incoming:
            cur = effective.get(r.sampled_at)
            if cur and cur[1] == r.revision and abs(cur[0] - r.reading) > EPS:
                raise UploadError(
                    f"与已入库数据冲突：水表 {meter_id} 在 {r.sampled_at} 的修订号 "
                    f"{r.revision} 已对应读数 {cur[0]}，本次文件为 {r.reading}"
                )

        # Merge: keep the highest revision per timestamp. A strictly higher
        # revision takes over provenance; equal/lower revisions (including a
        # byte-identical re-upload) leave the original association untouched.
        for r in incoming:
            cur = effective.get(r.sampled_at)
            if cur is None or r.revision > cur[1]:
                effective[r.sampled_at] = (
                    r.reading, r.revision, upload_id, r.line
                )

        # Rebuild intervals from time-adjacent readings.
        points = sorted(effective.items())
        intervals = []
        for (t1, (v1, rev1, _, _)), (t2, (v2, rev2, _, _)) in zip(
                points, points[1:]):
            usage = v2 - v1
            if usage < -EPS:
                raise UploadError(
                    f"合并后出现负用量：水表 {meter_id} 区间 "
                    f"{t1} → {t2} 的用量为 {round(usage, 6)}"
                )
            intervals.append((meter_id, t1, t2, v1, v2, rev1, rev2,
                              max(usage, 0.0)))

        conn.execute("DELETE FROM readings WHERE meter_id = ?", (meter_id,))
        conn.executemany(
            "INSERT INTO readings (meter_id, sampled_at, reading, revision, "
            "source_upload_id, source_line) VALUES (?, ?, ?, ?, ?, ?)",
            [(meter_id, t, v, rev, src_id, src_line)
             for t, (v, rev, src_id, src_line) in points],
        )
        conn.execute("DELETE FROM intervals WHERE meter_id = ?", (meter_id,))
        conn.executemany(
            "INSERT INTO intervals (meter_id, start_time, end_time, "
            "start_reading, end_reading, start_revision, end_revision, usage) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            intervals,
        )

        stats["readings_changed"] += sum(
            1 for t, val in effective.items()
            if before.get(t) != val
        )
        stats["intervals"] += len(intervals)

    return stats
