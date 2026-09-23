"""无第三方依赖的回归检查：算法边界、防刷课、模板导入与报表生成。"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
os.environ["DDS_DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="dds_test_")) / "test.sqlite3")
sys.path.insert(0, str(ROOT))

from backend.database import connect, migrate  # noqa: E402
from backend.engine import compute_measure, score_color, weighted_adg  # noqa: E402
from backend.importer import import_realtime  # noqa: E402
from backend.reports import generate_report, report_detail  # noqa: E402


SETTINGS = {
    "green_threshold": 90.0,
    "blue_threshold": 60.0,
    "coefficient_red_threshold": 0.5,
    "coefficient_blue_threshold": 0.8,
    "material_page_weight": 0.6,
    "material_interaction_weight": 0.4,
    "adg_window_days": 42,
    "adg_weights_recent_to_old": [0.5, 0.3, 0.2],
    "anti_cheat_min_page_stay_seconds": 5,
    "anti_cheat_rush_seconds": 30,
    "announcement_min_stay_seconds": 15,
}


def close(left, right, tolerance=0.01):
    assert abs(left - right) <= tolerance, f"{left} != {right}"


def test_algorithm_boundaries():
    assert score_color(89.95, SETTINGS) == "blue"
    assert score_color(90.0, SETTINGS) == "green"
    assert score_color(59.95, SETTINGS) == "red"
    assert score_color(60.0, SETTINGS) == "blue"
    adg, periods = weighted_adg(
        [("2026-01-01", 70), ("2026-01-04", 80)], "2026-01-07", 90, SETTINGS
    )
    assert len(periods) == 3
    close(adg, (90 * 0.5 + 80 * 0.3 + 70 * 0.2) / 1.0)


def test_standard_formula_and_anti_cheat():
    measure = {
        "total_score": 95,
        "full_score": 100,
        "effective_pages": 29,
        "total_pages": 30,
        "interaction_count": 18,
        "interaction_standard": 20,
        "announcement_complete": 19,
        "total_announcements": 20,
    }
    result = compute_measure(measure, SETTINGS)
    close(result["course_percent"], 95)
    close(result["material_coeff"], 0.94)
    close(result["announcement_coeff"], 0.95)
    close(result["idg"], 95 * 0.94 * 0.95)

    rushed = dict(measure, effective_pages=30, page_completion_seconds=20)
    assert compute_measure(rushed, SETTINGS)["material_coeff"] == 0
    incomplete = dict(measure, announcement_read_seconds=200, announcement_media_loaded=False)
    assert compute_measure(incomplete, SETTINGS)["announcement_coeff"] == 0


TEMPLATE = (
    "学号,所属学院,所属行政班,姓名,学生实得分,课程总分,有效阅读页数,课件总页数,"
    "有效交互次数,标准交互次数,完整阅读公告条数,公告总条数\n"
    "TEST001,测试学院,测试班,测试学生,95,100,29,30,18,20,19,20\n"
).encode("utf-8-sig")


def test_import_and_report():
    migrate()
    with connect() as connection:
        cursor = connection.execute(
            "INSERT INTO courses(tenant_id,name,code,created_at) VALUES(1,'回归课程','TEST-REG',datetime('now'))"
        )
        course_id = int(cursor.lastrowid)
        imported = import_realtime(
            connection,
            course_id,
            None,
            "template.csv",
            TEMPLATE,
            "2026-01-01",
            "",
        )
        assert imported["rows_imported"] == 1
        report_id = generate_report(connection, course_id, "2026-01-01", None)
        detail = report_detail(connection, report_id)
        assert detail["status"] == "draft"
        item = detail["items"][0]
        close(item["course_percent"], 95)
        close(item["material_coeff"], 0.94)
        close(item["announcement_coeff"], 0.95)
        close(item["idg"], 95 * 0.94 * 0.95, 0.02)
        assert item["profile"]["baseline"] is not None


if __name__ == "__main__":
    test_algorithm_boundaries()
    test_standard_formula_and_anti_cheat()
    test_import_and_report()
    print("OK：算法边界、防刷课、标准模板导入与报表生成全部通过。")
