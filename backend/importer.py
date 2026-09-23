"""数据适配层：雨课堂导出、历史平均表与标准模板导入。"""
from __future__ import annotations

from datetime import date
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .database import ROOT, iso_now
from .xlsx_reader import (
    choose_sheet,
    first_number,
    pick_header,
    read_table,
    rows_as_dicts,
    to_bool,
    to_float,
)


UPLOAD_DIR = ROOT / "data" / "platform" / "uploads"


EXACT_HEADERS = {
    "student_no": ["学号", "student_id", "student_no", "Student ID"],
    "college": ["所属学院", "college"],
    "class_name": ["所属行政班", "行政班", "class_name"],
    "name": ["姓名", "name"],
    "total_score": ["总分", "学生实得分", "实得分", "total_score"],
    "full_score": ["满分", "课程总分", "full_score"],
    "effective_pages": ["有效阅读页数", "观看总页数", "effective_pages"],
    "total_pages": ["课件总页数", "共发放页数", "total_pages"],
    "announcement_complete": [
        "完整阅读条数",
        "完整阅读公告条数",
        "阅读公告数",
        "公告阅读数",
        "签到次数",
        "announcement_complete",
    ],
    "total_announcements": [
        "公告总条数",
        "共发布公告数",
        "共发布公告",
        "开课次数",
        "total_announcements",
    ],
    "interaction_count": ["参与次数", "互动次数", "有效交互次数", "interaction_count"],
    "interaction_standard": ["互动总次数", "标准交互次数", "interaction_standard"],
    "video_seconds": ["已看时长", "视频已看时长", "已看视频秒数", "video_seconds"],
    "total_video_seconds": ["视频总时长", "视频总秒数", "total_video_seconds"],
    "announcement_read_seconds": ["公告阅读停留秒数", "announcement_read_seconds"],
    "announcement_media_loaded": ["公告图文加载完成", "announcement_media_loaded"],
    "page_stay_seconds": ["单页停留秒数", "page_stay_seconds"],
    "page_completion_seconds": ["全部页面完成秒数", "page_completion_seconds"],
}


def _find_exact(record: dict[str, str], field: str) -> str | None:
    for expected in EXACT_HEADERS[field]:
        for header in record:
            if header.strip() == expected:
                return header
    return None


def _find_contains(record: dict[str, str], field: str) -> str | None:
    for expected in EXACT_HEADERS[field]:
        for header in record:
            normalized = header.replace(" ", "").lower()
            if expected.replace(" ", "").lower() in normalized:
                return header
    return None


def _pair(
    record: dict[str, str], field: str, denominator_field: str
) -> tuple[float | None, float | None, str | None, str | None]:
    numerator_header = _find_exact(record, field) or _find_contains(record, field)
    denominator_header = _find_exact(record, denominator_field)
    if denominator_header is None:
        denominator_header = _find_contains(record, denominator_field)
    if denominator_header == numerator_header:
        denominator_header = None
    numerator = to_float(record.get(numerator_header, "")) if numerator_header else None

    # 雨课堂导出常把分母写在分子表头中，例如“总分(满分5600.0分)”。
    inline_denominator = None
    if numerator_header:
        inline_denominator = first_number(
            re.search(
                r"(?:满分|共|总共|开课|发布)([^）)]*)", str(numerator_header)
            ).group(1)
            if re.search(r"(?:满分|共|总共|开课|发布)", str(numerator_header))
            else ""
        )
    denominator = (
        to_float(record.get(denominator_header, ""))
        if denominator_header
        else inline_denominator
    )
    return numerator, denominator, numerator_header, denominator_header


def _safe_filename(filename: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", filename)
    return cleaned[:120] or "upload.bin"


def _store_upload(filename: str, content: bytes) -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = iso_now().replace(":", "").replace("-", "").replace("+", "_")
    path = UPLOAD_DIR / f"{timestamp}_{_safe_filename(filename)}"
    path.write_bytes(content)
    return path


def _anonymous_code(tenant_id: int, student_no: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}:{student_no}".encode("utf-8")).hexdigest()
    return digest[:8].upper()


def _upsert_student(
    connection: sqlite3.Connection,
    tenant_id: int,
    student_no: str,
    name: str,
    college: str,
    class_name: str,
) -> int:
    connection.execute(
        "INSERT INTO students(tenant_id,student_no,name,college,class_name,anonymous_code,created_at) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(tenant_id,student_no) DO UPDATE SET name=excluded.name, "
        "college=excluded.college,class_name=excluded.class_name",
        (
            tenant_id,
            student_no,
            name,
            college,
            class_name,
            _anonymous_code(tenant_id, student_no),
            iso_now(),
        ),
    )
    row = connection.execute(
        "SELECT id FROM students WHERE tenant_id=? AND student_no=?", (tenant_id, student_no)
    ).fetchone()
    return int(row["id"])


def _course(connection: sqlite3.Connection, course_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
    if not row:
        raise ValueError("课程不存在")
    return row


def _match_filter(
    student_no: str, name: str, class_name: str, student_filter: str | None
) -> bool:
    if not student_filter:
        return True
    keyword = student_filter.strip()
    if not keyword:
        return True
    return (
        keyword in student_no
        or keyword in name
        or keyword in class_name
        or student_no.startswith(keyword)
    )


def import_realtime(
    connection: sqlite3.Connection,
    course_id: int,
    user_id: int | None,
    filename: str,
    content: bytes,
    period_date: str,
    student_filter: str | None = None,
) -> dict[str, Any]:
    date.fromisoformat(period_date)
    course = _course(connection, course_id)
    sheets = read_table(content, filename)
    sheet = choose_sheet(sheets, "realtime")
    records = rows_as_dicts(sheet.rows)
    if not records:
        raise ValueError("导入文件没有数据行")

    storage_path = _store_upload(filename, content)
    cursor = connection.execute(
        "INSERT INTO imports(course_id,user_id,kind,filename,storage_path,status,period_date,rows_read,created_at) "
        "VALUES(?,?,?,?,?,'running',?,?,?)",
        (course_id, user_id, "realtime", filename, str(storage_path), period_date, len(records), iso_now()),
    )
    import_id = int(cursor.lastrowid)
    warnings: list[dict[str, str]] = []

    imported = 0
    for record in records:
        student_header = _find_exact(record, "student_no") or _find_contains(record, "student_no")
        name_header = _find_exact(record, "name") or _find_contains(record, "name")
        college_header = _find_exact(record, "college") or _find_contains(record, "college")
        class_header = _find_exact(record, "class_name") or _find_contains(record, "class_name")
        student_no = str(record.get(student_header, "")).strip() if student_header else ""
        name = str(record.get(name_header, "")).strip() if name_header else ""
        college = str(record.get(college_header, "")).strip() if college_header else ""
        class_name = str(record.get(class_header, "")).strip() if class_header else ""
        if not student_no:
            continue
        if not _match_filter(student_no, name, class_name, student_filter):
            continue

        total_score, full_score, score_header, full_header = _pair(
            record, "total_score", "full_score"
        )
        effective_pages, total_pages, page_header, pages_total_header = _pair(
            record, "effective_pages", "total_pages"
        )
        announcements, total_announcements, announcement_header, announcements_total_header = _pair(
            record, "announcement_complete", "total_announcements"
        )
        missing = []
        if full_score is None:
            missing.append("满分/课程总分")
        if total_pages is None:
            missing.append("课件总页数")
        if total_announcements is None:
            missing.append("公告总条数")
        if missing:
            raise ValueError("表头缺少分母：" + "、".join(missing))

        if announcement_header and "签到次数" in announcement_header:
            warnings.append(
                {
                    "type": "legacy_sign_field",
                    "message": "雨课堂导出使用“签到次数”作为公告阅读分子，已按项目既有口径适配",
                }
            )

        interaction_header = _find_exact(record, "interaction_count") or _find_contains(record, "interaction_count")
        interaction_standard_header = _find_exact(record, "interaction_standard") or _find_contains(record, "interaction_standard")
        video_header = _find_exact(record, "video_seconds") or _find_contains(record, "video_seconds")
        video_total_header = _find_exact(record, "total_video_seconds") or _find_contains(record, "total_video_seconds")
        read_seconds_header = pick_header(record, ["公告阅读停留秒数", "announcement_read_seconds"])
        media_header = pick_header(record, ["公告图文加载完成", "announcement_media_loaded"])
        page_stay_header = pick_header(record, ["单页停留秒数", "page_stay_seconds"])
        page_completion_header = pick_header(record, ["全部页面完成秒数", "page_completion_seconds"])

        student_id = _upsert_student(
            connection, int(course["tenant_id"]), student_no, name, college, class_name
        )
        source_fields = {
            "total_score": score_header,
            "full_score": full_header or score_header,
            "effective_pages": page_header,
            "total_pages": pages_total_header or page_header,
            "announcement_complete": announcement_header,
            "total_announcements": announcements_total_header or announcement_header,
            "sheet": sheet.name,
        }
        connection.execute(
            "INSERT INTO measures(course_id,student_id,import_id,period_date,total_score,full_score,"
            "effective_pages,total_pages,interaction_count,interaction_standard,announcement_complete,total_announcements,"
            "announcement_read_seconds,announcement_media_loaded,video_seconds,total_video_seconds,"
            "page_stay_seconds,page_completion_seconds,source_fields_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(course_id,student_id,period_date) DO UPDATE SET "
            "import_id=excluded.import_id,total_score=excluded.total_score,full_score=excluded.full_score,"
            "effective_pages=excluded.effective_pages,total_pages=excluded.total_pages,"
            "interaction_count=excluded.interaction_count,interaction_standard=excluded.interaction_standard,"
            "announcement_complete=excluded.announcement_complete,total_announcements=excluded.total_announcements,"
            "announcement_read_seconds=excluded.announcement_read_seconds,"
            "announcement_media_loaded=excluded.announcement_media_loaded,"
            "video_seconds=excluded.video_seconds,total_video_seconds=excluded.total_video_seconds,"
            "page_stay_seconds=excluded.page_stay_seconds,page_completion_seconds=excluded.page_completion_seconds,"
            "source_fields_json=excluded.source_fields_json",
            (
                course_id,
                student_id,
                import_id,
                period_date,
                total_score or 0,
                full_score,
                effective_pages or 0,
                total_pages,
                to_float(record.get(interaction_header, "")) if interaction_header else None,
                to_float(record.get(interaction_standard_header, "")) if interaction_standard_header else None,
                announcements or 0,
                total_announcements,
                to_float(record.get(read_seconds_header, "")) if read_seconds_header else None,
                to_bool(record.get(media_header, "")) if media_header else None,
                to_float(record.get(video_header, "")) if video_header else None,
                to_float(record.get(video_total_header, "")) if video_total_header else None,
                to_float(record.get(page_stay_header, "")) if page_stay_header else None,
                to_float(record.get(page_completion_header, "")) if page_completion_header else None,
                json.dumps(source_fields, ensure_ascii=False),
                iso_now(),
            ),
        )
        imported += 1

    if imported == 0:
        connection.execute(
            "UPDATE imports SET status='failed', error='筛选后没有可导入学生' WHERE id=?",
            (import_id,),
        )
        raise ValueError("筛选后没有可导入学生")

    deduplicated_warnings: list[dict[str, str]] = []
    seen = set()
    for warning in warnings:
        key = warning["type"]
        if key not in seen:
            deduplicated_warnings.append(warning)
            seen.add(key)
    connection.execute(
        "UPDATE imports SET status='success',rows_imported=?,warnings_json=?,error='' WHERE id=?",
        (imported, json.dumps(deduplicated_warnings, ensure_ascii=False), import_id),
    )
    return {
        "import_id": import_id,
        "kind": "realtime",
        "sheet": sheet.name,
        "rows_read": len(records),
        "rows_imported": imported,
        "warnings": deduplicated_warnings,
        "storage_path": str(storage_path),
    }


def _infer_year(filename: str) -> int:
    match = re.search(r"(20\d{2})", filename)
    if match:
        return int(match.group(1))
    match = re.search(r"(?<!\d)(2[3-9])(?!\d)", filename)
    if match:
        return 2000 + int(match.group(1))
    return date.today().year


def _header_date(header: str, year: int) -> str | None:
    full = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", header)
    if full:
        try:
            return date(int(full.group(1)), int(full.group(2)), int(full.group(3))).isoformat()
        except ValueError:
            return None
    month_day = re.search(r"(\d{1,2})月(\d{1,2})日", header)
    if month_day:
        try:
            return date(year, int(month_day.group(1)), int(month_day.group(2))).isoformat()
        except ValueError:
            return None
    compact = re.match(r"^(\d{2})(\d{2})(?:即时|动态)", header.strip())
    if compact:
        try:
            return date(year, int(compact.group(1)), int(compact.group(2))).isoformat()
        except ValueError:
            return None
    return None


def import_history(
    connection: sqlite3.Connection,
    course_id: int,
    user_id: int | None,
    filename: str,
    content: bytes,
    history_year: int | None = None,
) -> dict[str, Any]:
    course = _course(connection, course_id)
    sheets = read_table(content, filename)
    sheet = choose_sheet(sheets, "history")
    records = rows_as_dicts(sheet.rows)
    if not records:
        raise ValueError("历史平均表没有数据行")
    year = history_year or _infer_year(filename)
    storage_path = _store_upload(filename, content)
    cursor = connection.execute(
        "INSERT INTO imports(course_id,user_id,kind,filename,storage_path,status,period_date,rows_read,created_at) "
        "VALUES(?,?,?,?,?,'running',NULL,?,?)",
        (course_id, user_id, "history", filename, str(storage_path), len(records), iso_now()),
    )
    import_id = int(cursor.lastrowid)

    sample = records[0]
    student_header = _find_exact(sample, "student_no") or _find_contains(sample, "student_no")
    name_header = _find_exact(sample, "name") or _find_contains(sample, "name")
    class_header = _find_exact(sample, "class_name") or _find_contains(sample, "class_name")
    score_headers = [
        header
        for header in sample
        if ("即时动态成绩" in header or "即时成绩" in header)
        and _header_date(header, year)
    ]
    if not student_header or not score_headers:
        raise ValueError("历史表必须包含学号和至少一个“即时动态成绩”日期列")

    imported = 0
    for record in records:
        student_no = str(record.get(student_header, "")).strip()
        if not student_no:
            continue
        name = str(record.get(name_header, "")).strip() if name_header else ""
        class_name = str(record.get(class_header, "")).strip() if class_header else ""
        student_id = _upsert_student(
            connection, int(course["tenant_id"]), student_no, name, "", class_name
        )
        for header in score_headers:
            value = to_float(record.get(header, ""))
            if value is None:
                continue
            period_date = _header_date(header, year)
            connection.execute(
                "INSERT INTO historical_scores(course_id,student_id,import_id,period_date,idg,created_at) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(course_id,student_id,period_date) DO UPDATE SET "
                "import_id=excluded.import_id,idg=excluded.idg",
                (course_id, student_id, import_id, period_date, value, iso_now()),
            )
            imported += 1

    warning = {
        "type": "history_idg_only",
        "message": "历史表仅导入历史 IDG 用于 ADG 时间加权，不覆盖过程性分子/分母证据",
    }
    connection.execute(
        "UPDATE imports SET status='success',rows_imported=?,warnings_json=? WHERE id=?",
        (imported, json.dumps([warning], ensure_ascii=False), import_id),
    )
    return {
        "import_id": import_id,
        "kind": "history",
        "sheet": sheet.name,
        "year": year,
        "periods": len(score_headers),
        "rows_read": len(records),
        "rows_imported": imported,
        "warnings": [warning],
        "storage_path": str(storage_path),
    }
