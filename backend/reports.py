"""报表生成与发布状态流转。"""
from __future__ import annotations

from collections import Counter
import json
import sqlite3
from typing import Any

from .database import get_settings, iso_now
from .engine import (
    build_profile,
    compute_measure,
    guidance_for,
    pre_warnings,
    score_color,
    state_type,
    weighted_adg,
)


def _measure_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def generate_report(
    connection: sqlite3.Connection,
    course_id: int,
    period_date: str,
    user_id: int | None,
) -> int:
    settings = get_settings(connection)
    current_rows = connection.execute(
        "SELECT m.*, s.id AS student_table_id, s.student_no, s.name, s.class_name "
        "FROM measures m JOIN students s ON s.id=m.student_id "
        "WHERE m.course_id=? AND m.period_date=? "
        "ORDER BY s.class_name COLLATE NOCASE, s.student_no COLLATE NOCASE",
        (course_id, period_date),
    ).fetchall()
    if not current_rows:
        raise ValueError("该课程在指定日期没有过程观测数据，请先导入")

    existing = connection.execute(
        "SELECT id,status FROM reports WHERE course_id=? AND period_date=?",
        (course_id, period_date),
    ).fetchone()
    if existing and existing["status"] == "published":
        raise ValueError("已发布报表不能直接重算；请先在 24 小时内撤回")

    now = iso_now()
    if existing:
        report_id = int(existing["id"])
        connection.execute("DELETE FROM report_items WHERE report_id=?", (report_id,))
        connection.execute(
            "UPDATE reports SET status='draft',generated_by=?,reviewed_by=NULL,reviewed_at=NULL,"
            "published_by=NULL,published_at=NULL,revoked_by=NULL,revoked_at=NULL,revoke_reason='',"
            "updated_at=? WHERE id=?",
            (user_id, now, report_id),
        )
    else:
        cursor = connection.execute(
            "INSERT INTO reports(course_id,period_date,status,generated_by,created_at,updated_at) "
            "VALUES(?,?,'draft',?,?,?)",
            (course_id, period_date, user_id, now, now),
        )
        report_id = int(cursor.lastrowid)

    color_counter: Counter[str] = Counter()
    state_counter: Counter[str] = Counter()
    warning_count = 0
    for row in current_rows:
        measure = _measure_dict(row)
        metrics = compute_measure(measure, settings)
        history = connection.execute(
            "SELECT period_date,idg FROM historical_scores "
            "WHERE course_id=? AND student_id=? AND period_date<? "
            "ORDER BY period_date",
            (course_id, row["student_id"], period_date),
        ).fetchall()
        adg, periods = weighted_adg(
            [(item["period_date"], item["idg"]) for item in history],
            period_date,
            metrics["idg"],
            settings,
        )

        recent_measure_rows = connection.execute(
            "SELECT * FROM measures WHERE course_id=? AND student_id=? AND period_date<=? "
            "ORDER BY period_date DESC LIMIT 3",
            (course_id, row["student_id"], period_date),
        ).fetchall()
        metric_history = [
            compute_measure(_measure_dict(item), settings)
            for item in reversed(recent_measure_rows)
        ]
        warnings = metrics["warnings"] + pre_warnings(metric_history, settings)
        flags = metrics["flags"] + warnings
        profile = build_profile(metrics, adg)
        color = score_color(metrics["idg"], settings)
        state = state_type(metrics["idg"], adg, settings)
        guidance = guidance_for(color, metrics)
        evidence = dict(metrics["evidence"])
        evidence["ADG参与周期"] = periods

        connection.execute(
            "INSERT INTO report_items(report_id,student_id,course_percent,material_coeff,announcement_coeff,"
            "video_coeff,interaction_coeff,idg,adg,score_color,state_type,flags_json,profile_json,evidence_json,guidance) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                report_id,
                row["student_id"],
                metrics["course_percent"],
                metrics["material_coeff"],
                metrics["announcement_coeff"],
                metrics["video_coeff"],
                metrics["interaction_coeff"],
                metrics["idg"],
                adg,
                color,
                state,
                json.dumps(flags, ensure_ascii=False),
                json.dumps(profile, ensure_ascii=False),
                json.dumps(evidence, ensure_ascii=False),
                guidance,
            ),
        )
        color_counter[color] += 1
        state_counter[state] += 1
        warning_count += len(warnings)

    summary = {
        "total": len(current_rows),
        "colors": dict(color_counter),
        "states": dict(state_counter),
        "warning_count": warning_count,
        "low_stakes": True,
        "ranking": "disabled",
        "config_snapshot": {
            "green_threshold": settings["green_threshold"],
            "blue_threshold": settings["blue_threshold"],
            "material_page_weight": settings["material_page_weight"],
            "material_interaction_weight": settings["material_interaction_weight"],
            "adg_window_days": settings["adg_window_days"],
            "adg_weights_recent_to_old": settings["adg_weights_recent_to_old"],
        },
        "generated_at": now,
    }
    connection.execute(
        "UPDATE reports SET summary_json=?,updated_at=? WHERE id=?",
        (json.dumps(summary, ensure_ascii=False), now, report_id),
    )
    return report_id


def report_detail(connection: sqlite3.Connection, report_id: int) -> dict[str, Any] | None:
    report = connection.execute(
            "SELECT r.*, c.tenant_id, c.name AS course_name,c.code AS course_code "
        "FROM reports r JOIN courses c ON c.id=r.course_id WHERE r.id=?",
        (report_id,),
    ).fetchone()
    if not report:
        return None
    items = connection.execute(
        "SELECT ri.*,s.student_no,s.name,s.class_name,s.college,s.anonymous_code "
        "FROM report_items ri JOIN students s ON s.id=ri.student_id "
        "WHERE ri.report_id=? "
        "ORDER BY s.class_name COLLATE NOCASE,s.student_no COLLATE NOCASE",
        (report_id,),
    ).fetchall()
    result = {key: report[key] for key in report.keys()}
    result["summary"] = json.loads(result.pop("summary_json") or "{}")
    result["items"] = []
    for item in items:
        serialized = {key: item[key] for key in item.keys()}
        serialized["flags"] = json.loads(serialized.pop("flags_json") or "[]")
        serialized["profile"] = json.loads(serialized.pop("profile_json") or "{}")
        serialized["evidence"] = json.loads(serialized.pop("evidence_json") or "{}")
        result["items"].append(serialized)
    return result
