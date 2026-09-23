"""双动赋能双驱育心平台端到端集成测试。

测试范围对应轻量级 V2.1 的 V01–V07 验收要求，并覆盖：
登录角色、课程、历史/即时导入、重复导入删除、报表闭环、学生隔离、
预警合并、干预、心理转介、量表静默通道、低利害导出与审计。

运行方式：
    python tools/integration_test.py
"""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from urllib.parse import quote, urlencode


ROOT = Path(__file__).resolve().parent.parent
TEMP_DIR = Path(tempfile.mkdtemp(prefix="dds_integration_"))
os.environ["DDS_DB_PATH"] = str(TEMP_DIR / "integration.sqlite3")
sys.path.insert(0, str(ROOT))

from backend.database import connect, migrate  # noqa: E402
from backend.engine import compute_measure, score_color, weighted_adg  # noqa: E402
from backend.web import Handler  # noqa: E402


PASSED: list[str] = []


class Response:
    def __init__(self, status: int, body: bytes, headers: dict[str, str]):
        self.status = status
        self.body = body
        self.headers = headers

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def text(self):
        return self.body.decode("utf-8-sig")


class Client:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

    def request(
        self,
        path: str,
        method: str = "GET",
        json_body: dict | None = None,
        token: str | None = None,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        connection = HTTPConnection(self.host, self.port, timeout=15)
        request_headers = dict(headers or {})
        body = raw_body
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        result = Response(
            response.status,
            response.read(),
            {key.lower(): value for key, value in response.getheaders()},
        )
        connection.close()
        return result


def check(name: str, condition: bool, detail: str = ""):
    if not condition:
        raise AssertionError(f"{name} 失败：{detail}")
    PASSED.append(name)
    print(f"  ✔ {name}")


def login(client: Client, username: str, password: str):
    response = client.request(
        "/api/auth/login", "POST", {"username": username, "password": password}
    )
    assert response.status == 200, response.text()
    return response.json()


def import_file(
    client: Client,
    token: str,
    course_id: int,
    kind: str,
    filename: str,
    content: bytes,
    **params,
):
    query = {"course_id": course_id, "kind": kind, **params}
    response = client.request(
        f"/api/imports?{urlencode(query)}",
        "POST",
        raw_body=content,
        token=token,
        headers={"X-Filename": quote(filename)},
    )
    assert response.status == 201, response.text()
    return response.json()


def test_algorithm_units():
    settings = {
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
    # V01：三色边界。
    assert score_color(89.95, settings) == "blue"
    assert score_color(90.0, settings) == "green"
    assert score_color(59.95, settings) == "red"
    assert score_color(60.0, settings) == "blue"

    # V02：ADG 时间加权，近→远 50/30/20。
    adg, periods = weighted_adg(
        [("2026-01-01", 70), ("2026-01-04", 80)],
        "2026-01-07",
        90,
        settings,
    )
    assert len(periods) == 3
    assert abs(adg - 83.0) < 0.01, adg

    # V03：防刷课与公告完整阅读。
    base = {
        "total_score": 95,
        "full_score": 100,
        "effective_pages": 29,
        "total_pages": 30,
        "interaction_count": 18,
        "interaction_standard": 20,
        "announcement_complete": 19,
        "total_announcements": 20,
        "video_seconds": 50,
        "total_video_seconds": 100,
    }
    result = compute_measure(base, settings)
    assert abs(result["course_percent"] - 95) < 0.01
    assert abs(result["material_coeff"] - 0.94) < 0.01
    assert abs(result["announcement_coeff"] - 0.95) < 0.01
    assert abs(result["idg"] - (95 * 0.94 * 0.95)) < 0.01
    assert any(x["field"] == "video_coeff" and x["scope"] == "observation" for x in result["warnings"])
    rushed = compute_measure(
        dict(base, effective_pages=30, page_completion_seconds=20), settings
    )
    assert rushed["material_coeff"] == 0
    incomplete = compute_measure(
        dict(base, announcement_read_seconds=200, announcement_media_loaded=False),
        settings,
    )
    assert incomplete["announcement_coeff"] == 0
    check("V01–V04 算法边界、ADG、防刷课、观测预警", True)


def build_history_csv() -> bytes:
    rows = [
        ["学号", "所属行政班", "姓名", "0101即时动态成绩", "0104即时动态成绩"],
        ["A001", "测试班", "低分学生", "70", "80"],
        ["Z002", "测试班", "高分学生", "70", "80"],
    ]
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def build_realtime_csv() -> bytes:
    rows = [
        [
            "学号", "所属学院", "所属行政班", "姓名", "学生实得分", "课程总分",
            "有效阅读页数", "课件总页数", "有效交互次数", "标准交互次数",
            "完整阅读公告条数", "公告总条数", "已看视频秒数", "视频总秒数",
        ],
        [
            "A001", "测试学院", "测试班", "低分学生", "50", "100",
            "15", "30", "10", "20", "10", "20", "50", "100",
        ],
        [
            "Z002", "测试学院", "测试班", "高分学生", "100", "100",
            "30", "30", "20", "20", "20", "20", "100", "100",
        ],
    ]
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def run_e2e():
    migrate()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(*server.server_address[:2])
    try:
        health = client.request("/api/health")
        check("健康检查", health.status == 200 and health.json()["status"] == "ok")

        teacher_login = login(client, "teacher", "teacher123456")
        admin_login = login(client, "admin", "admin123456")
        teacher = teacher_login["token"]
        teacher_user = teacher_login["user"]
        admin = admin_login["token"]

        # 账号与角色管理。
        created = client.request(
            "/api/users",
            "POST",
            {
                "username": "newteacher",
                "password": "Safe12345678",
                "role": "teacher",
                "display_name": "新教师",
            },
            admin,
        )
        check("管理员创建教师账号", created.status == 201)
        new_teacher_login = login(client, "newteacher", "Safe12345678")
        check("新教师可登录", new_teacher_login["user"]["role"] == "teacher")
        forbidden = client.request(
            "/api/settings", "PUT", {"green_threshold": 91}, new_teacher_login["token"]
        )
        check("教师不能修改全局参数", forbidden.status == 403)

        # 课程。
        course_response = client.request(
            "/api/courses",
            "POST",
            {
                "name": "集成测试课程",
                "code": "IT-COURSE-001",
                "source_platform": "标准CSV",
                "academic_year": "2025-2026",
            },
            teacher,
        )
        check("创建课程", course_response.status == 201)
        course_id = course_response.json()["id"]

        # 历史 IDG 与即时过程数据。
        history = import_file(
            client,
            teacher,
            course_id,
            "history",
            "history.csv",
            build_history_csv(),
            history_year=2026,
        )
        check("导入历史 IDG", history["rows_imported"] == 4)

        realtime = import_file(
            client,
            teacher,
            course_id,
            "realtime",
            "realtime.csv",
            build_realtime_csv(),
            period_date="2026-01-07",
        )
        check("导入即时过程数据", realtime["rows_imported"] == 2)

        # 重复导入同日期数据不应重复计算；删除重复导入后数据保留。
        duplicate = import_file(
            client,
            teacher,
            course_id,
            "realtime",
            "realtime-duplicate.csv",
            build_realtime_csv(),
            period_date="2026-01-07",
        )
        deleted = client.request(f"/api/imports/{duplicate['import_id']}", "DELETE", token=teacher)
        check("删除重复即时导入", deleted.status == 200 and deleted.json()["reassigned_to"] == realtime["import_id"])

        generated = client.request(
            "/api/reports/generate",
            "POST",
            {"course_id": course_id, "period_date": "2026-01-07"},
            teacher,
        )
        check("生成双动态报表", generated.status == 201)
        report_id = generated.json()["report_id"]
        detail = client.request(f"/api/reports/{report_id}", token=teacher).json()
        check("报表人数与颜色汇总", detail["summary"]["total"] == 2 and detail["summary"]["colors"] == {"green": 1, "red": 1})

        by_no = {item["student_no"]: item for item in detail["items"]}
        low = by_no["A001"]
        high = by_no["Z002"]
        check(
            "V02 IDG 逐点计算",
            abs(low["idg"] - 12.5) < 0.01 and abs(high["idg"] - 100.0) < 0.01,
            f"low={low['idg']}, high={high['idg']}",
        )
        check(
            "V02 ADG 时间加权",
            abs(low["adg"] - 44.25) < 0.01 and abs(high["adg"] - 88.0) < 0.01,
            f"low={low['adg']}, high={high['adg']}",
        )
        check("分子分母证据显式可查", low["evidence"]["课件有效页"]["numerator"] == 15)

        # V06：默认无排名，按班级/学号排序，而不是按成绩排序。
        check("低利害默认无排名视图", [x["student_no"] for x in detail["items"]] == ["A001", "Z002"])
        ranking = client.request("/api/ranking", token=teacher)
        check("全站无排名接口", ranking.status == 404)

        # V05：未核定物理拒发；核定后发布。
        draft_publish = client.request(f"/api/reports/{report_id}/publish", "POST", {}, teacher)
        check("未核定报表后端拒发", draft_publish.status == 403)
        bad_review = client.request(f"/api/reports/{report_id}/review", "POST", {"confirmed": False}, teacher)
        check("核定必须显式确认", bad_review.status == 400)
        review = client.request(f"/api/reports/{report_id}/review", "POST", {"confirmed": True}, teacher)
        check("教师核定成功", review.status == 200 and review.json()["status"] == "reviewed")
        publish = client.request(f"/api/reports/{report_id}/publish", "POST", {}, teacher)
        check("核定后发布成功", publish.status == 200 and publish.json()["status"] == "published")
        publish_again = client.request(f"/api/reports/{report_id}/publish", "POST", {}, teacher)
        check("发布幂等", publish_again.status == 200 and publish_again.json().get("idempotent") is True)

        # 预警合并与观测标红。
        warnings = client.request("/api/warnings", token=teacher).json()["items"]
        low_warnings = next(x for x in warnings if x["student_no"] == "A001")
        scopes = {x.get("scope") for x in low_warnings["warnings"]}
        check("同一学生多项预警合并为一行", low_warnings["report_item_id"] == low["id"])
        check("系数预警与观测预警独立", "coefficient" in scopes and "observation" in scopes)

        # 学生登录、绑定、隔离访问。
        student_login = login(client, "student", "student123456")
        student = student_login["token"]
        before_binding = client.request(f"/api/reports/{report_id}", token=student)
        check("未绑定学号不能查看报表", before_binding.status == 403)
        binding = client.request(
            "/api/auth/student-binding",
            "POST",
            {"student_no": "Z002", "name": "高分学生"},
            student,
        )
        check("学号姓名绑定", binding.status == 200)
        student_detail = client.request(f"/api/reports/{report_id}", token=student).json()
        check("学生只看到本人一行", len(student_detail["items"]) == 1 and student_detail["items"][0]["student_no"] == "Z002")
        student_forbidden = client.request("/api/interventions", "POST", {}, student)
        check("学生不能创建干预", student_forbidden.status == 403)
        growth = client.request("/api/students/growth?student_no=Z002", token=student)
        check("学生学号成长曲线", growth.status == 200 and len(growth.json()["trend"]) == 3)

        # 干预：仅 report_item_id 也应自动补齐学生与课程；学号也可作为标识。
        intervention_1 = client.request(
            "/api/interventions",
            "POST",
            {
                "report_item_id": low["id"],
                "mode": "ambulance",
                "category": "教师一对一",
                "content": "48小时内一对一沟通",
            },
            teacher,
        )
        check("报表记录自动关联学生和课程", intervention_1.status == 201)
        intervention_2 = client.request(
            "/api/interventions",
            "POST",
            {
                "student_no": "Z002",
                "course_id": course_id,
                "mode": "navigation",
                "category": "学习提醒",
                "content": "保持过程投入",
            },
            teacher,
        )
        check("学号创建干预", intervention_2.status == 201)

        # 心理转介：学号创建、分派、状态更新。
        referral_create = client.request(
            "/api/referrals",
            "POST",
            {
                "student_no": "A001",
                "course_id": course_id,
                "reason": "集成测试心理支持",
                "assigned_to": teacher_user["id"],
            },
            teacher,
        )
        check("学号创建2小时转介工单", referral_create.status == 201)
        referral_id = referral_create.json()["id"]
        referral_update = client.request(
            "/api/referrals",
            "PATCH",
            {"id": referral_id, "status": "reported", "notes": "已上报"},
            teacher,
        )
        check("被分派教师处理工单", referral_update.status == 200)
        referrals = client.request("/api/referrals", token=teacher).json()
        check("工单列表包含处理留痕", any(x["id"] == referral_id and x["status"] == "reported" for x in referrals["items"]))

        # 静默申请与审批。
        silent_create = client.request(
            "/api/silent-requests", "POST", {"reason": "近期减少提醒"}, student
        )
        check("学生提交静默申请", silent_create.status == 201)
        silent_id = silent_create.json()["id"]
        silent_update = client.request(
            "/api/silent-requests",
            "PATCH",
            {"id": silent_id, "status": "approved"},
            teacher,
        )
        check("教师审批静默申请", silent_update.status == 200)

        # V07：量表静默通道与危机 2 小时上报。
        questionnaire_list = client.request("/api/questionnaires", token=student).json()["items"]
        questionnaire = next(x for x in questionnaire_list if x["question_count"] > 0)
        questionnaire_detail = client.request(
            f"/api/questionnaires/{questionnaire['id']}", token=student
        ).json()
        answers = {str(question["id"]): 3 for question in questionnaire_detail["questions"]}
        crisis_question = questionnaire_detail["questions"][-1]
        answers[str(crisis_question["id"])] = 1
        questionnaire_submit = client.request(
            f"/api/questionnaires/{questionnaire['id']}/responses",
            "POST",
            {"answers": answers},
            student,
        )
        submit_body = questionnaire_submit.json()
        check(
            "量表结果不返回给学生",
            questionnaire_submit.status == 201 and "score" not in submit_body,
        )
        questionnaire_responses = client.request(
            f"/api/questionnaires/{questionnaire['id']}/responses", token=teacher
        )
        check(
            "教师可在授权工作流查看量表响应",
            questionnaire_responses.status == 200
            and any(x["student_no"] == "Z002" for x in questionnaire_responses.json()["items"]),
        )
        referral_items = client.request("/api/referrals", token=teacher).json()["items"]
        crisis_referral = next(
            (
                x
                for x in referral_items
                if x["source"] == "questionnaire" and x["student_no"] == "Z002"
            ),
            None,
        )
        check("危机触发自动生成工单", crisis_referral is not None)
        if crisis_referral:
            created_time = datetime.fromisoformat(crisis_referral["created_at"])
            due_time = datetime.fromisoformat(crisis_referral["due_at"])
            elapsed_hours = (due_time - created_time).total_seconds() / 3600
            check("危机上报截止不超过2小时", 1.95 <= elapsed_hours <= 2.05, str(elapsed_hours))

        # V06：导出必须二次确认，导出保持低利害顺序。
        export_without_confirm = client.request(
            f"/api/reports/{report_id}/export", token=teacher
        )
        check("导出必须低利害确认", export_without_confirm.status == 400)
        export = client.request(
            f"/api/reports/{report_id}/export?confirm_low_stakes=true", token=teacher
        )
        check("确认后可导出CSV", export.status == 200 and export.headers["content-type"].startswith("text/csv"))
        export_rows = list(csv.DictReader(io.StringIO(export.text())))
        check("导出无排名且按学号排序", [x["学号"] for x in export_rows] == ["A001", "Z002"])

        # 审计必须覆盖关键动作。
        audit = client.request("/api/audit", token=admin).json()["items"]
        actions = {x["action"] for x in audit}
        required_actions = {
            "login", "course.create", "import.realtime", "import.delete",
            "report.generate", "report.review", "report.publish",
            "report.export", "intervention.create", "referral.create",
            "silent.decide", "questionnaire.submit",
        }
        check("关键业务动作全部审计", required_actions.issubset(actions), str(required_actions - actions))

        # 手册与静态资源可访问。
        manual = client.request("/docs/teacher-guide.md")
        home = client.request("/")
        check("平台首页与手册可访问", home.status == 200 and manual.status == 200)

        # 24小时内撤回后学生不可见。
        revoke = client.request(
            f"/api/reports/{report_id}/revoke",
            "POST",
            {"reason": "集成测试撤回"},
            teacher,
        )
        check("24小时内可撤回", revoke.status == 200 and revoke.json()["status"] == "revoked")
        student_after_revoke = client.request(f"/api/reports/{report_id}", token=student)
        check("撤回后学生端不可见", student_after_revoke.status == 403)

        # 数据库层最终一致性。
        with connect() as connection:
            measure_count = connection.execute(
                "SELECT COUNT(*) FROM measures WHERE course_id=?", (course_id,)
            ).fetchone()[0]
            import_count = connection.execute(
                "SELECT COUNT(*) FROM imports WHERE course_id=?", (course_id,)
            ).fetchone()[0]
        check("删除重复导入后过程数据保留", measure_count == 2 and import_count == 2)

    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main():
    print("双动赋能双驱育心平台：集成测试开始")
    test_algorithm_units()
    run_e2e()
    print(f"\n集成测试全部通过：{len(PASSED)} 项")
    print("覆盖 V01–V07 验收要求与主要业务闭环。")


if __name__ == "__main__":
    main()
