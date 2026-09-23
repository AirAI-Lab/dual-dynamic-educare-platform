"""轻量级 HTTP/API 服务：仅使用 Python 标准库，静态前端与 REST API 同源部署。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import csv
import io
import json
import mimetypes
import os
import re
import sqlite3
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from . import importer
from .database import (
    ROOT,
    audit,
    connect,
    create_session,
    get_settings,
    iso_now,
    save_settings,
    verify_password,
)
from .reports import generate_report, report_detail


FRONTEND_DIR = ROOT / "frontend"
MAX_BODY_BYTES = 30 * 1024 * 1024
# 单进程内的短时会话缓存：避免登录响应与下一请求在 WAL 提交可见性上的极端竞态。
SESSION_CACHE: dict[str, tuple[int, str]] = {}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return {key: row[key] for key in row.keys()} if row else None


def public_user(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "display_name": row["display_name"],
        "tenant_id": row["tenant_id"],
        "student_id": row["student_id"],
    }


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Handler(BaseHTTPRequestHandler):
    server_version = "DDS/2.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("DDS_DEBUG"):
            super().log_message(fmt, *args)

    def _send(
        self,
        status: int,
        content: bytes,
        content_type: str = "application/json; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def _json(self, value: Any, status: int = 200) -> None:
        content = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, content)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "上传文件超过 30MB 限制")
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> dict[str, Any]:
        body = self._read_body()
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError(400, f"JSON 格式错误：{error}")
        if not isinstance(value, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return value

    def _query(self) -> dict[str, str]:
        parsed = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def _token(self) -> str | None:
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return None

    def _user(self, connection: sqlite3.Connection) -> sqlite3.Row:
        token = self._token()
        if not token:
            raise ApiError(401, "请先登录")
        cached = SESSION_CACHE.get(token)
        if cached:
            user_id, expires_at = cached
            if parse_iso(expires_at) <= datetime.now(timezone.utc):
                SESSION_CACHE.pop(token, None)
            else:
                row = connection.execute(
                    "SELECT * FROM users WHERE id=? AND active=1", (user_id,)
                ).fetchone()
                if row:
                    return row
                SESSION_CACHE.pop(token, None)
        row = connection.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token=? AND s.expires_at>? AND u.active=1",
            (token, iso_now()),
        ).fetchone()
        if not row:
            raise ApiError(401, "登录已过期，请重新登录")
        return row

    @staticmethod
    def _require_role(user: sqlite3.Row, roles: tuple[str, ...]) -> None:
        if user["role"] not in roles:
            raise ApiError(403, "当前角色没有权限执行此操作")

    def do_OPTIONS(self) -> None:
        self._send(
            204,
            b"",
            extra_headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "Authorization,Content-Type,X-Filename",
                "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
            },
        )

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            path = unquote(urlparse(self.path).path)
            if not path.startswith("/api/"):
                if method in {"GET", "HEAD"}:
                    return self._static(path)
                raise ApiError(404, "接口不存在")
            with connect() as connection:
                handler = self._route(method, path)
                result, status, headers = handler(connection)
                # 必须在响应发出前提交会话、报表与审计事务，避免客户端下一请求竞态。
                connection.commit()
                if isinstance(result, bytes):
                    headers = headers or {}
                    content_type = headers.get(
                        "Content-Type", "application/octet-stream"
                    )
                    extra = {
                        key: value
                        for key, value in headers.items()
                        if key.lower() != "content-type"
                    }
                    self._send(status, result, content_type, extra)
                else:
                    self._send(
                        status,
                        json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"),
                        "application/json; charset=utf-8",
                        headers,
                    )
        except ApiError as error:
            self._json({"error": error.message}, error.status)
        except (ValueError, KeyError) as error:
            self._json({"error": str(error)}, 400)
        except sqlite3.IntegrityError as error:
            self._json({"error": f"数据完整性冲突：{error}"}, 409)
        except Exception as error:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": f"服务器错误：{error}"}, 500)

    def _static(self, path: str) -> None:
        if path == "/":
            file_path = FRONTEND_DIR / "index.html"
        else:
            relative = Path(path.lstrip("/"))
            if relative.is_absolute() or ".." in relative.parts:
                raise ApiError(400, "非法静态路径")
            if relative.parts and relative.parts[0] == "docs":
                file_path = ROOT / relative
            else:
                file_path = FRONTEND_DIR / relative
        if not file_path.is_file():
            file_path = FRONTEND_DIR / "index.html"
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        if file_path.suffix.lower() == ".md":
            content_type = "text/markdown; charset=utf-8"
        if "charset=" not in content_type:
            content_type = f"{content_type}; charset=utf-8"
        self._send(200, file_path.read_bytes(), content_type)

    def _route(
        self, method: str, path: str
    ) -> Callable[[sqlite3.Connection], tuple[Any, int, dict[str, str] | None]]:
        routes: dict[tuple[str, str], Callable[[sqlite3.Connection], tuple[Any, int, None]]] = {
            ("GET", "/api/health"): self.api_health,
            ("POST", "/api/auth/login"): self.api_login,
            ("GET", "/api/auth/me"): self.api_me,
            ("POST", "/api/auth/logout"): self.api_logout,
            ("POST", "/api/auth/student-binding"): self.api_bind_student,
            ("POST", "/api/auth/change-password"): self.api_change_password,
            ("GET", "/api/overview"): self.api_overview,
            ("GET", "/api/settings"): self.api_get_settings,
            ("PUT", "/api/settings"): self.api_put_settings,
            ("GET", "/api/users"): self.api_users,
            ("POST", "/api/users"): self.api_create_user,
            ("PATCH", "/api/users"): self.api_update_user,
            ("GET", "/api/users/choices"): self.api_user_choices,
            ("GET", "/api/students/growth"): self.api_student_growth_by_no,
            ("GET", "/api/courses"): self.api_courses,
            ("POST", "/api/courses"): self.api_create_course,
            ("GET", "/api/imports"): self.api_imports,
            ("POST", "/api/imports"): self.api_import,
            ("POST", "/api/reports/generate"): self.api_generate_report,
            ("GET", "/api/reports"): self.api_reports,
            ("GET", "/api/template.csv"): self.api_template,
            ("GET", "/api/warnings"): self.api_warnings,
            ("GET", "/api/interventions"): self.api_interventions,
            ("POST", "/api/interventions"): self.api_create_intervention,
            ("PATCH", "/api/interventions"): self.api_update_intervention,
            ("GET", "/api/questionnaires"): self.api_questionnaires,
            ("POST", "/api/questionnaires"): self.api_create_questionnaire,
            ("GET", "/api/silent-requests"): self.api_silent_requests,
            ("POST", "/api/silent-requests"): self.api_create_silent_request,
            ("PATCH", "/api/silent-requests"): self.api_update_silent_request,
            ("GET", "/api/referrals"): self.api_referrals,
            ("POST", "/api/referrals"): self.api_create_referral,
            ("PATCH", "/api/referrals"): self.api_update_referral,
            ("GET", "/api/audit"): self.api_audit,
        }
        handler = routes.get((method, path))
        if handler:
            return handler

        patterns = [
            ("GET", re.compile(r"^/api/reports/(\d+)$"), self.api_report_detail),
            ("POST", re.compile(r"^/api/reports/(\d+)/review$"), self.api_review_report),
            ("POST", re.compile(r"^/api/reports/(\d+)/publish$"), self.api_publish_report),
            ("POST", re.compile(r"^/api/reports/(\d+)/revoke$"), self.api_revoke_report),
            ("GET", re.compile(r"^/api/reports/(\d+)/export$"), self.api_export_report),
            ("DELETE", re.compile(r"^/api/imports/(\d+)$"), self.api_delete_import),
            ("GET", re.compile(r"^/api/students/(\d+)/growth$"), self.api_student_growth),
            ("GET", re.compile(r"^/api/questionnaires/(\d+)$"), self.api_questionnaire_detail),
            ("POST", re.compile(r"^/api/questionnaires/(\d+)/responses$"), self.api_submit_questionnaire),
            ("GET", re.compile(r"^/api/questionnaires/(\d+)/responses$"), self.api_questionnaire_responses),
        ]
        for expected_method, pattern, handler_factory in patterns:
            match = pattern.match(path)
            if match and method == expected_method:
                object_id = int(match.group(1))

                def bound(connection: sqlite3.Connection, h=handler_factory, oid=object_id):
                    return h(connection, oid)

                return bound
        raise ApiError(404, "接口不存在")

    def api_health(self, connection: sqlite3.Connection):
        return {"status": "ok", "service": "双动赋能双驱育心教学评估平台", "version": "2.1"}, 200, None

    def api_login(self, connection: sqlite3.Connection):
        body = self._read_json()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        user = connection.execute(
            "SELECT * FROM users WHERE tenant_id=1 AND username=? AND active=1", (username,)
        ).fetchone()
        if not user or not verify_password(password, user["password_hash"]):
            raise ApiError(401, "用户名或密码错误")
        token = create_session(connection, int(user["id"]))
        expires_at = connection.execute(
            "SELECT expires_at FROM sessions WHERE token=?", (token,)
        ).fetchone()["expires_at"]
        SESSION_CACHE[token] = (int(user["id"]), expires_at)
        audit(connection, int(user["id"]), "login", "user", user["id"])
        return {"token": token, "user": public_user(user)}, 200, None

    def api_me(self, connection: sqlite3.Connection):
        user = self._user(connection)
        result = public_user(user)
        if user["student_id"]:
            student = connection.execute(
                "SELECT id,student_no,name,college,class_name FROM students WHERE id=?",
                (user["student_id"],),
            ).fetchone()
            result["student"] = row_dict(student)
        return result, 200, None

    def api_logout(self, connection: sqlite3.Connection):
        user = self._user(connection)
        token = self._token()
        connection.execute("DELETE FROM sessions WHERE token=?", (token,))
        SESSION_CACHE.pop(token, None)
        audit(connection, int(user["id"]), "logout", "user", user["id"])
        return {"ok": True}, 200, None

    def api_bind_student(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("student",))
        body = self._read_json()
        student_no = str(body.get("student_no", "")).strip()
        name = str(body.get("name", "")).strip()
        if not student_no:
            raise ApiError(400, "请填写学号")
        student = connection.execute(
            "SELECT * FROM students WHERE tenant_id=? AND student_no=?",
            (user["tenant_id"], student_no),
        ).fetchone()
        if not student:
            raise ApiError(404, "尚未导入该学号，请联系老师先导入班级数据")
        if student["name"] and student["name"] != name:
            raise ApiError(400, "学号与姓名不一致，绑定已拒绝")
        connection.execute(
            "UPDATE users SET student_id=? WHERE id=?", (student["id"], user["id"])
        )
        audit(connection, int(user["id"]), "bind_student", "student", student["id"])
        return {"ok": True, "student": row_dict(student)}, 200, None

    def api_change_password(self, connection: sqlite3.Connection):
        user = self._user(connection)
        body = self._read_json()
        old_password = str(body.get("old_password", ""))
        new_password = str(body.get("new_password", ""))
        if not verify_password(old_password, user["password_hash"]):
            raise ApiError(400, "原密码错误")
        if len(new_password) < 10:
            raise ApiError(400, "新密码至少 10 位")
        from .database import hash_password

        connection.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (hash_password(new_password), user["id"]),
        )
        current_token = self._token()
        old_tokens = [
            row["token"]
            for row in connection.execute(
                "SELECT token FROM sessions WHERE user_id=? AND token<>?",
                (user["id"], current_token),
            )
        ]
        for token in old_tokens:
            SESSION_CACHE.pop(token, None)
        connection.execute(
            "DELETE FROM sessions WHERE user_id=? AND token<>?",
            (user["id"], current_token),
        )
        audit(connection, int(user["id"]), "password.change", "user", user["id"])
        return {"ok": True, "message": "密码已修改，其他登录已失效"}, 200, None

    def api_overview(self, connection: sqlite3.Connection):
        user = self._user(connection)
        result: dict[str, Any] = {
            "low_stakes": True,
            "closed_loop": ["采集", "计算", "核定", "发布", "预警", "育心留痕"],
        }
        if user["role"] == "student":
            if not user["student_id"]:
                result.update({"courses": 0, "reports": 0, "warnings": 0})
                return result, 200, None
            reports = connection.execute(
                "SELECT COUNT(DISTINCT r.id) AS count FROM reports r "
                "JOIN report_items ri ON ri.report_id=r.id WHERE r.status='published' AND ri.student_id=?",
                (user["student_id"],),
            ).fetchone()["count"]
            result.update({"courses": 0, "reports": reports, "warnings": 0})
            return result, 200, None
        warning_rows = connection.execute(
            "SELECT ri.flags_json FROM report_items ri JOIN reports r ON r.id=ri.report_id "
            "WHERE r.status IN ('reviewed','published')"
        ).fetchall()
        warning_count = sum(
            1
            for row in warning_rows
            if any(
                flag.get("scope") in {"coefficient", "pre_warning", "observation"}
                or flag.get("type")
                in {"rush_learning", "invalid_short_stay", "announcement_media_not_loaded"}
                for flag in json.loads(row["flags_json"] or "[]")
            )
        )
        result.update(
            {
                "courses": connection.execute(
                    "SELECT COUNT(*) AS count FROM courses WHERE archived=0"
                ).fetchone()["count"],
                "imports": connection.execute(
                    "SELECT COUNT(*) AS count FROM imports"
                ).fetchone()["count"],
                "reports": connection.execute(
                    "SELECT COUNT(*) AS count FROM reports"
                ).fetchone()["count"],
                "published": connection.execute(
                    "SELECT COUNT(*) AS count FROM reports WHERE status='published'"
                ).fetchone()["count"],
                "warnings": warning_count,
                "open_referrals": connection.execute(
                    "SELECT COUNT(*) AS count FROM referrals WHERE status IN ('open','escalated')"
                ).fetchone()["count"],
            }
        )
        return result, 200, None

    def api_get_settings(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        return get_settings(connection), 200, None

    def api_put_settings(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin",))
        body = self._read_json()
        numeric_keys = {
            "green_threshold",
            "blue_threshold",
            "coefficient_green_threshold",
            "coefficient_blue_threshold",
            "coefficient_red_threshold",
            "observation_threshold",
            "material_page_weight",
            "material_interaction_weight",
            "adg_window_days",
            "idg_period_days",
            "idg_final_term_weight",
            "anti_cheat_min_page_stay_seconds",
            "anti_cheat_rush_seconds",
            "announcement_min_stay_seconds",
        }
        values: dict[str, Any] = {}
        for key, value in body.items():
            if key in numeric_keys:
                values[key] = float(value)
            elif key == "adg_weights_recent_to_old":
                if not isinstance(value, list) or not value:
                    raise ApiError(400, "权重必须是数组")
                values[key] = [float(item) for item in value]
            elif key == "low_stakes_mode":
                if value is not True:
                    raise ApiError(400, "低利害铁律不可关闭")
        if (
            float(values.get("green_threshold", 90)) <= float(values.get("blue_threshold", 60))
            and "green_threshold" in values
        ):
            raise ApiError(400, "绿线必须大于蓝线")
        saved = save_settings(connection, values)
        audit(connection, int(user["id"]), "settings.update", "settings", 1, body)
        return saved, 200, None

    def api_users(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin",))
        rows = connection.execute(
            "SELECT id,username,role,display_name,active,created_at "
            "FROM users WHERE tenant_id=? ORDER BY role,username",
            (user["tenant_id"],)
        ).fetchall()
        return {"items": [dict(row) for row in rows]}, 200, None

    def api_user_choices(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        rows = connection.execute(
            "SELECT id,username,display_name,role FROM users "
            "WHERE tenant_id=? AND active=1 AND role IN ('admin','teacher') "
            "ORDER BY display_name,username",
            (user["tenant_id"],)
        ).fetchall()
        return {"items": [dict(row) for row in rows]}, 200, None

    def api_create_user(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin",))
        from .database import hash_password

        body = self._read_json()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        role = str(body.get("role", "teacher"))
        display_name = str(body.get("display_name", "")).strip() or username
        if not username or len(password) < 10:
            raise ApiError(400, "用户名必填，初始密码至少 10 位")
        if role not in {"admin", "teacher", "student"}:
            raise ApiError(400, "角色只能是 admin、teacher 或 student")
        cursor = connection.execute(
            "INSERT INTO users(tenant_id,username,password_hash,role,display_name,active,created_at) "
            "VALUES(?,?,?,?,?,1,?)",
            (
                user["tenant_id"],
                username,
                hash_password(password),
                role,
                display_name,
                iso_now(),
            ),
        )
        audit(
            connection,
            int(user["id"]),
            "user.create",
            "user",
            cursor.lastrowid,
            {"username": username, "role": role},
        )
        return {"id": cursor.lastrowid, "username": username, "role": role}, 201, None

    def api_update_user(self, connection: sqlite3.Connection):
        operator = self._user(connection)
        self._require_role(operator, ("admin",))
        body = self._read_json()
        user_id = int(body.get("id") or 0)
        if not user_id:
            raise ApiError(400, "id 必填")
        target = connection.execute(
            "SELECT * FROM users WHERE id=? AND tenant_id=?",
            (user_id, operator["tenant_id"]),
        ).fetchone()
        if not target:
            raise ApiError(404, "用户不存在")
        role = body.get("role", target["role"])
        active = body.get("active", target["active"])
        if role not in {"admin", "teacher", "student"}:
            raise ApiError(400, "非法角色")
        if target["id"] == operator["id"] and (role != "admin" or int(active) == 0):
            raise ApiError(400, "不能取消自己的管理员权限或停用自己")
        new_password = body.get("new_password")
        password_hash = target["password_hash"]
        if new_password:
            if len(str(new_password)) < 10:
                raise ApiError(400, "重置密码至少 10 位")
            from .database import hash_password

            password_hash = hash_password(str(new_password))
        connection.execute(
            "UPDATE users SET role=?,active=?,display_name=?,password_hash=? WHERE id=?",
            (
                role,
                1 if active else 0,
                str(body.get("display_name", target["display_name"])),
                password_hash,
                user_id,
            ),
        )
        audit(
            connection,
            int(operator["id"]),
            "user.update",
            "user",
            user_id,
            {
                "role": role,
                "active": bool(active),
                "password_reset": bool(new_password),
            },
        )
        return {"ok": True}, 200, None

    def api_courses(self, connection: sqlite3.Connection):
        user = self._user(connection)
        rows = connection.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM (SELECT student_id FROM measures WHERE course_id=c.id "
            "UNION SELECT student_id FROM historical_scores WHERE course_id=c.id)) AS student_count, "
            "(SELECT COUNT(*) FROM reports r WHERE r.course_id=c.id) AS report_count "
            "FROM courses c WHERE c.tenant_id=? AND c.archived=0 ORDER BY c.created_at DESC",
            (user["tenant_id"],),
        ).fetchall()
        return {"items": [dict(row) for row in rows]}, 200, None

    def api_create_course(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        body = self._read_json()
        name = str(body.get("name", "")).strip()
        code = str(body.get("code", "")).strip()
        if not name or not code:
            raise ApiError(400, "课程名称和课程代码必填")
        cursor = connection.execute(
            "INSERT INTO courses(tenant_id,name,code,source_platform,academic_year,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                user["tenant_id"],
                name,
                code,
                str(body.get("source_platform", "雨课堂")),
                str(body.get("academic_year", "")),
                user["id"],
                iso_now(),
            ),
        )
        audit(connection, int(user["id"]), "course.create", "course", cursor.lastrowid, body)
        return {"id": cursor.lastrowid, "name": name, "code": code}, 201, None

    def api_imports(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        query = self._query()
        sql = (
            "SELECT i.*,c.name AS course_name FROM imports i "
            "JOIN courses c ON c.id=i.course_id WHERE c.tenant_id=?"
        )
        params: list[Any] = [user["tenant_id"]]
        if query.get("course_id"):
            sql += " AND i.course_id=?"
            params.append(int(query["course_id"]))
        sql += " ORDER BY i.id DESC LIMIT 200"
        rows = connection.execute(sql, params).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
            items.append(item)
        return {"items": items}, 200, None

    def api_import(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        query = self._query()
        course_id = int(query.get("course_id") or 0)
        kind = query.get("kind", "realtime")
        filename = unquote(
            self.headers.get("X-Filename") or query.get("filename") or "upload.xlsx"
        )
        content = self._read_body()
        if kind not in {"realtime", "history"}:
            raise ApiError(400, "导入类型必须是 realtime 或 history")
        course = connection.execute(
            "SELECT id FROM courses WHERE id=? AND tenant_id=?",
            (course_id, user["tenant_id"]),
        ).fetchone()
        if not course:
            raise ApiError(404, "课程不存在或无权限")
        if kind == "realtime":
            period_date = query.get("period_date") or datetime.now().date().isoformat()
            result = importer.import_realtime(
                connection,
                course_id,
                int(user["id"]),
                filename,
                content,
                period_date,
                query.get("student_filter"),
            )
        else:
            history_year = int(query["history_year"]) if query.get("history_year") else None
            result = importer.import_history(
                connection, course_id, int(user["id"]), filename, content, history_year
            )
        audit(
            connection,
            int(user["id"]),
            f"import.{kind}",
            "course",
            course_id,
            {"filename": filename, **result},
        )
        return result, 201, None

    def api_delete_import(self, connection: sqlite3.Connection, import_id: int):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        item = connection.execute(
            "SELECT i.*,c.tenant_id FROM imports i JOIN courses c ON c.id=i.course_id WHERE i.id=?",
            (import_id,),
        ).fetchone()
        if not item or item["tenant_id"] != user["tenant_id"]:
            raise ApiError(404, "导入记录不存在")

        # 同一课程、同一类型的重复导入会覆盖唯一键。删除其中一条时，
        # 先把仍被该记录引用的数据重新挂到另一条重复导入上，避免误删有效数据。
        duplicate = connection.execute(
            "SELECT id FROM imports WHERE course_id=? AND kind=? AND id<>? "
            "ORDER BY CASE WHEN status='success' THEN 0 ELSE 1 END, id LIMIT 1",
            (item["course_id"], item["kind"], import_id),
        ).fetchone()
        if item["kind"] == "history":
            if duplicate:
                connection.execute(
                    "UPDATE historical_scores SET import_id=? WHERE import_id=?",
                    (duplicate["id"], import_id),
                )
            else:
                connection.execute(
                    "DELETE FROM historical_scores WHERE import_id=?", (import_id,)
                )
        else:
            if duplicate:
                connection.execute(
                    "UPDATE measures SET import_id=? WHERE import_id=?",
                    (duplicate["id"], import_id),
                )
            else:
                connection.execute("DELETE FROM measures WHERE import_id=?", (import_id,))
        connection.execute("DELETE FROM imports WHERE id=?", (import_id,))
        audit(
            connection,
            int(user["id"]),
            "import.delete",
            "import",
            import_id,
            {
                "kind": item["kind"],
                "filename": item["filename"],
                "reassigned_to": duplicate["id"] if duplicate else None,
            },
        )
        return {"ok": True, "reassigned_to": duplicate["id"] if duplicate else None}, 200, None

    def api_generate_report(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        body = self._read_json()
        course_id = int(body.get("course_id") or 0)
        period_date = str(body.get("period_date") or "")
        if not course_id or not period_date:
            raise ApiError(400, "course_id 和 period_date 必填")
        course = connection.execute(
            "SELECT id FROM courses WHERE id=? AND tenant_id=?",
            (course_id, user["tenant_id"]),
        ).fetchone()
        if not course:
            raise ApiError(404, "课程不存在或无权限")
        report_id = generate_report(connection, course_id, period_date, int(user["id"]))
        audit(
            connection,
            int(user["id"]),
            "report.generate",
            "report",
            report_id,
            {"course_id": course_id, "period_date": period_date},
        )
        return {"report_id": report_id}, 201, None

    def api_reports(self, connection: sqlite3.Connection):
        user = self._user(connection)
        query = self._query()
        sql = (
            "SELECT r.id,r.course_id,r.period_date,r.status,r.created_at,r.updated_at,r.published_at,"
            "r.reviewed_at,r.summary_json,c.name AS course_name,c.code AS course_code "
            "FROM reports r JOIN courses c ON c.id=r.course_id WHERE c.tenant_id=?"
        )
        params: list[Any] = [user["tenant_id"]]
        if user["role"] == "student":
            if not user["student_id"]:
                return {"items": []}, 200, None
            sql += " AND r.status='published' AND EXISTS (SELECT 1 FROM report_items ri WHERE ri.report_id=r.id AND ri.student_id=?)"
            params.append(user["student_id"])
        if query.get("course_id"):
            sql += " AND r.course_id=?"
            params.append(int(query["course_id"]))
        if query.get("status"):
            sql += " AND r.status=?"
            params.append(query["status"])
        sql += " ORDER BY r.period_date DESC,r.id DESC LIMIT 300"
        rows = connection.execute(sql, params).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["summary"] = json.loads(item.pop("summary_json") or "{}")
            items.append(item)
        return {"items": items}, 200, None

    def api_report_detail(self, connection: sqlite3.Connection, report_id: int):
        user = self._user(connection)
        detail = report_detail(connection, report_id)
        if not detail or detail["tenant_id"] != user["tenant_id"]:
            raise ApiError(404, "报表不存在")
        if user["role"] == "student":
            if detail["status"] != "published":
                raise ApiError(403, "报表尚未发布")
            if not user["student_id"]:
                raise ApiError(403, "请先绑定学号")
            detail["items"] = [
                item for item in detail["items"] if item["student_id"] == user["student_id"]
            ]
            if not detail["items"]:
                raise ApiError(404, "报表不存在")
        detail.pop("tenant_id", None)
        return detail, 200, None

    def api_review_report(self, connection: sqlite3.Connection, report_id: int):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        body = self._read_json()
        if body.get("confirmed") is not True:
            raise ApiError(400, "教师核定必须显式 confirmed=true")
        report = connection.execute(
            "SELECT r.* FROM reports r JOIN courses c ON c.id=r.course_id "
            "WHERE r.id=? AND c.tenant_id=?",
            (report_id, user["tenant_id"]),
        ).fetchone()
        if not report:
            raise ApiError(404, "报表不存在")
        if report["status"] == "published":
            raise ApiError(409, "报表已发布，不能重复核定")
        connection.execute(
            "UPDATE reports SET status='reviewed',reviewed_by=?,reviewed_at=?,updated_at=? WHERE id=?",
            (user["id"], iso_now(), iso_now(), report_id),
        )
        audit(connection, int(user["id"]), "report.review", "report", report_id, body)
        return {"ok": True, "status": "reviewed"}, 200, None

    def api_publish_report(self, connection: sqlite3.Connection, report_id: int):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        report = connection.execute(
            "SELECT r.* FROM reports r JOIN courses c ON c.id=r.course_id "
            "WHERE r.id=? AND c.tenant_id=?",
            (report_id, user["tenant_id"]),
        ).fetchone()
        if not report:
            raise ApiError(404, "报表不存在")
        if report["status"] == "published":
            return {"ok": True, "status": "published", "idempotent": True}, 200, None
        if report["status"] != "reviewed":
            raise ApiError(403, "后端拒绝发布未核定报表")
        connection.execute(
            "UPDATE reports SET status='published',published_by=?,published_at=?,updated_at=? WHERE id=?",
            (user["id"], iso_now(), iso_now(), report_id),
        )
        audit(connection, int(user["id"]), "report.publish", "report", report_id)
        return {"ok": True, "status": "published"}, 200, None

    def api_revoke_report(self, connection: sqlite3.Connection, report_id: int):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        body = self._read_json()
        report = connection.execute(
            "SELECT r.* FROM reports r JOIN courses c ON c.id=r.course_id "
            "WHERE r.id=? AND c.tenant_id=?",
            (report_id, user["tenant_id"]),
        ).fetchone()
        if not report:
            raise ApiError(404, "报表不存在")
        if report["status"] == "revoked":
            return {"ok": True, "status": "revoked", "idempotent": True}, 200, None
        if report["status"] != "published" or not report["published_at"]:
            raise ApiError(409, "只有已发布报表可撤回")
        published = parse_iso(report["published_at"])
        if datetime.now(timezone.utc) - published > timedelta(hours=24):
            raise ApiError(403, "已超过 24 小时撤回窗口")
        connection.execute(
            "UPDATE reports SET status='revoked',revoked_by=?,revoked_at=?,revoke_reason=?,updated_at=? WHERE id=?",
            (user["id"], iso_now(), str(body.get("reason", "")), iso_now(), report_id),
        )
        audit(connection, int(user["id"]), "report.revoke", "report", report_id, body)
        return {"ok": True, "status": "revoked"}, 200, None

    def api_export_report(self, connection: sqlite3.Connection, report_id: int):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        query = self._query()
        if query.get("confirm_low_stakes") != "true":
            raise ApiError(400, "导出前必须确认低利害用途（confirm_low_stakes=true）")
        detail = report_detail(connection, report_id)
        if not detail or detail["tenant_id"] != user["tenant_id"]:
            raise ApiError(404, "报表不存在")
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            [
                "班级", "学号", "姓名", "课程百分得分", "课件阅读系数", "公告阅读系数",
                "视频观看系数", "互动系数", "即时动态成绩IDG", "平均动态成绩ADG",
                "三色", "状态类型", "分子分母证据", "预警与标记", "成长指引",
            ]
        )
        for item in detail["items"]:
            writer.writerow(
                [
                    item["class_name"],
                    item["student_no"],
                    item["name"],
                    item["course_percent"],
                    item["material_coeff"],
                    item["announcement_coeff"],
                    "" if item["video_coeff"] is None else item["video_coeff"],
                    "" if item["interaction_coeff"] is None else item["interaction_coeff"],
                    item["idg"],
                    item["adg"],
                    item["score_color"],
                    item["state_type"],
                    json.dumps(item["evidence"], ensure_ascii=False),
                    json.dumps(item["flags"], ensure_ascii=False),
                    item["guidance"],
                ]
            )
        audit(
            connection,
            int(user["id"]),
            "report.export",
            "report",
            report_id,
            {"low_stakes_confirmed": True, "ranking": "disabled"},
        )
        content = output.getvalue().encode("utf-8-sig")
        filename = f"report-{report_id}-low-stakes.csv"
        return content, 200, {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{filename}"',
        }

    def api_template(self, connection: sqlite3.Connection):
        content = (
            "学号,所属学院,所属行政班,姓名,学生实得分,课程总分,有效阅读页数,课件总页数,"
            "有效交互次数,标准交互次数,完整阅读公告条数,公告总条数,已看视频秒数,视频总秒数,"
            "单页停留秒数,全部页面完成秒数,公告阅读停留秒数,公告图文加载完成\n"
            "S20250001,示例学院,25示例班,张三,95,100,29,30,18,20,19,20,120,120,35,900,300,true\n"
        ).encode("utf-8-sig")
        return content, 200, {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": 'attachment; filename="dds-import-template.csv"',
        }

    def api_warnings(self, connection: sqlite3.Connection):
        user = self._user(connection)
        query = self._query()
        sql = (
            "SELECT ri.id,ri.report_id,r.course_id,r.period_date,r.status,ri.student_id,ri.flags_json,ri.idg,ri.adg,"
            "s.student_no,s.name,s.class_name,c.name AS course_name "
            "FROM report_items ri JOIN reports r ON r.id=ri.report_id "
            "JOIN students s ON s.id=ri.student_id JOIN courses c ON c.id=r.course_id "
            "WHERE c.tenant_id=? AND r.status IN ('reviewed','published')"
        )
        params: list[Any] = [user["tenant_id"]]
        if user["role"] == "student":
            sql += " AND ri.student_id=? AND r.status='published'"
            params.append(user["student_id"])
        if query.get("course_id"):
            sql += " AND r.course_id=?"
            params.append(int(query["course_id"]))
        sql += " ORDER BY r.period_date DESC,s.class_name,s.student_no LIMIT 1000"
        rows = connection.execute(sql, params).fetchall()
        grouped: dict[int, dict[str, Any]] = {}
        for row in rows:
            flags = json.loads(row["flags_json"] or "[]")
            actionable = [
                flag
                for flag in flags
                if flag.get("scope") in {"coefficient", "pre_warning", "observation"}
                or flag.get("type")
                in {"rush_learning", "invalid_short_stay", "announcement_media_not_loaded"}
            ]
            if not actionable:
                continue
            grouped[row["id"]] = {
                "report_item_id": row["id"],
                "report_id": row["report_id"],
                "course_id": row["course_id"],
                "course_name": row["course_name"],
                "period_date": row["period_date"],
                "student_id": row["student_id"],
                "student_no": row["student_no"],
                "name": row["name"],
                "class_name": row["class_name"],
                "idg": row["idg"],
                "adg": row["adg"],
                "warnings": actionable,
            }
        warnings = list(grouped.values())
        return {"items": warnings}, 200, None

    def api_student_growth_by_no(self, connection: sqlite3.Connection):
        query = self._query()
        student_no = str(query.get("student_no") or "").strip()
        if not student_no:
            raise ApiError(400, "student_no 必填")
        user = self._user(connection)
        student = connection.execute(
            "SELECT id FROM students WHERE tenant_id=? AND student_no=?",
            (user["tenant_id"], student_no),
        ).fetchone()
        if not student:
            raise ApiError(404, "学生不存在")
        return self.api_student_growth(connection, int(student["id"]))

    def api_student_growth(self, connection: sqlite3.Connection, student_id: int):
        user = self._user(connection)
        if user["role"] == "student" and int(user["student_id"] or 0) != student_id:
            raise ApiError(403, "只能查看自己的成长曲线")
        student = connection.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
        if not student or student["tenant_id"] != user["tenant_id"]:
            raise ApiError(404, "学生不存在")
        query = self._query()
        course_id = int(query.get("course_id") or 0)
        history = connection.execute(
            "SELECT period_date,idg FROM historical_scores WHERE student_id=? "
            + ("AND course_id=? " if course_id else "")
            + "ORDER BY period_date",
            (student_id, course_id) if course_id else (student_id,),
        ).fetchall()
        points = {row["period_date"]: row["idg"] for row in history}
        report_rows = connection.execute(
            "SELECT r.period_date,ri.idg,ri.adg,r.status,ri.profile_json FROM report_items ri "
            "JOIN reports r ON r.id=ri.report_id WHERE ri.student_id=? "
            + ("AND r.course_id=? " if course_id else "")
            + "ORDER BY r.period_date",
            (student_id, course_id) if course_id else (student_id,),
        ).fetchall()
        latest_profile = {}
        for row in report_rows:
            if user["role"] == "student" and row["status"] != "published":
                continue
            points[row["period_date"]] = row["idg"]
            latest_profile = json.loads(row["profile_json"] or "{}")
        baseline = min(points.values()) if points else None
        trend = [
            {"date": period, "idg": round(float(value), 2), "baseline": baseline}
            for period, value in sorted(points.items())
        ]
        return {
            "student": row_dict(student),
            "baseline": baseline,
            "trend": trend,
            "profile": latest_profile,
            "note": "趋势只与自身基线比较，不提供班级排名或位次。",
        }, 200, None

    def api_interventions(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        rows = connection.execute(
            "SELECT i.*,s.student_no,s.name,s.class_name,c.name AS course_name "
            "FROM interventions i JOIN students s ON s.id=i.student_id "
            "JOIN courses c ON c.id=i.course_id WHERE c.tenant_id=? "
            "ORDER BY i.updated_at DESC LIMIT 300",
            (user["tenant_id"],)
        ).fetchall()
        return {"items": [dict(row) for row in rows]}, 200, None

    def api_create_intervention(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        body = self._read_json()
        report_item_id = body.get("report_item_id")
        student_id = int(body.get("student_id") or 0)
        course_id = int(body.get("course_id") or 0)
        if report_item_id:
            item = connection.execute(
                "SELECT ri.student_id,ri.report_id FROM report_items ri "
                "JOIN reports r ON r.id=ri.report_id JOIN courses c ON c.id=r.course_id "
                "WHERE ri.id=? AND c.tenant_id=?",
                (report_item_id, user["tenant_id"]),
            ).fetchone()
            if not item:
                raise ApiError(400, "关联报表记录不存在")
            student_id = student_id or int(item["student_id"])
            if not course_id:
                report = connection.execute(
                    "SELECT course_id FROM reports WHERE id=?", (item["report_id"],)
                ).fetchone()
                course_id = int(report["course_id"]) if report else course_id
        if not student_id and body.get("student_no"):
            student = connection.execute(
                "SELECT id FROM students WHERE tenant_id=? AND student_no=?",
                (user["tenant_id"], str(body["student_no"]).strip()),
            ).fetchone()
            student_id = int(student["id"]) if student else 0
        student = connection.execute(
            "SELECT id FROM students WHERE id=? AND tenant_id=?",
            (student_id, user["tenant_id"]),
        ).fetchone()
        course = connection.execute(
            "SELECT id FROM courses WHERE id=? AND tenant_id=?",
            (course_id, user["tenant_id"]),
        ).fetchone()
        if not student or not course:
            raise ApiError(400, "缺少有效的学生或课程，无法创建干预")
        cursor = connection.execute(
            "INSERT INTO interventions(report_item_id,student_id,course_id,mode,category,content,owner_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                report_item_id,
                student_id,
                course_id,
                str(body.get("mode", "navigation")),
                str(body.get("category", "")),
                str(body.get("content", "")),
                user["id"],
                iso_now(),
                iso_now(),
            ),
        )
        referral_id = None
        if body.get("create_referral") is True:
            due_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
            referral_cursor = connection.execute(
                "INSERT INTO referrals(student_id,course_id,source,reason,risk_level,status,due_at,handler,assigned_to,notes,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'open',?,?,?,?,?,?)",
                (
                    student_id,
                    course_id,
                    "intervention",
                    str(body.get("category", "干预")),
                    "high",
                    due_at,
                    user["display_name"],
                    user["id"],
                    str(body.get("content", "")),
                    iso_now(),
                    iso_now(),
                ),
            )
            referral_id = referral_cursor.lastrowid
        audit(connection, int(user["id"]), "intervention.create", "intervention", cursor.lastrowid, body)
        return {"id": cursor.lastrowid, "referral_id": referral_id}, 201, None

    def api_update_intervention(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        body = self._read_json()
        intervention_id = int(body.get("id") or 0)
        if not intervention_id:
            raise ApiError(400, "id 必填")
        existing = connection.execute(
            "SELECT i.id FROM interventions i JOIN courses c ON c.id=i.course_id "
            "WHERE i.id=? AND c.tenant_id=?",
            (intervention_id, user["tenant_id"]),
        ).fetchone()
        if not existing:
            raise ApiError(404, "干预记录不存在")
        connection.execute(
            "UPDATE interventions SET status=COALESCE(?,status),result=COALESCE(?,result),updated_at=? WHERE id=?",
            (body.get("status"), body.get("result"), iso_now(), intervention_id),
        )
        audit(connection, int(user["id"]), "intervention.update", "intervention", intervention_id, body)
        return {"ok": True}, 200, None

    def api_questionnaires(self, connection: sqlite3.Connection):
        user = self._user(connection)
        sql = (
            "SELECT q.id,q.course_id,q.title,q.description,q.status,q.sensitive,q.created_at,"
            "COUNT(qu.id) AS question_count FROM questionnaires q "
            "LEFT JOIN questionnaire_questions qu ON qu.questionnaire_id=q.id WHERE 1=1"
        )
        params: list[Any] = []
        if user["role"] == "student":
            sql += " AND q.status='published'"
        sql += " GROUP BY q.id ORDER BY q.id DESC"
        rows = connection.execute(sql, params).fetchall()
        return {"items": [dict(row) for row in rows]}, 200, None

    def api_create_questionnaire(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        body = self._read_json()
        title = str(body.get("title", "")).strip()
        questions = body.get("questions") or []
        if not title or not isinstance(questions, list):
            raise ApiError(400, "title 和 questions 必填")
        cursor = connection.execute(
            "INSERT INTO questionnaires(course_id,title,description,status,sensitive,created_by,created_at) "
            "VALUES(?,?,?,?,1,?,?)",
            (
                body.get("course_id"),
                title,
                str(body.get("description", "")),
                str(body.get("status", "draft")),
                user["id"],
                iso_now(),
            ),
        )
        questionnaire_id = int(cursor.lastrowid)
        for index, question in enumerate(questions, start=1):
            options = question.get("options") or ["从不", "偶尔", "有时", "经常", "总是"]
            connection.execute(
                "INSERT INTO questionnaire_questions(questionnaire_id,order_no,stem,options_json,reverse_scoring,crisis_operator,crisis_value) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    questionnaire_id,
                    index,
                    str(question.get("stem", "")),
                    json.dumps(options, ensure_ascii=False),
                    1 if question.get("reverse_scoring") else 0,
                    str(question.get("crisis_operator", "")),
                    question.get("crisis_value"),
                ),
            )
        audit(connection, int(user["id"]), "questionnaire.create", "questionnaire", questionnaire_id, {"title": title})
        return {"id": questionnaire_id}, 201, None

    def api_questionnaire_detail(self, connection: sqlite3.Connection, questionnaire_id: int):
        user = self._user(connection)
        questionnaire = connection.execute(
            "SELECT * FROM questionnaires WHERE id=?", (questionnaire_id,)
        ).fetchone()
        if not questionnaire or (
            user["role"] == "student" and questionnaire["status"] != "published"
        ):
            raise ApiError(404, "量表不存在或未发布")
        questions = connection.execute(
            "SELECT id,order_no,stem,options_json,reverse_scoring FROM questionnaire_questions "
            "WHERE questionnaire_id=? ORDER BY order_no",
            (questionnaire_id,),
        ).fetchall()
        result = dict(questionnaire)
        result["questions"] = []
        for question in questions:
            item = dict(question)
            item["options"] = json.loads(item.pop("options_json") or "[]")
            result["questions"].append(item)
        return result, 200, None

    def api_submit_questionnaire(self, connection: sqlite3.Connection, questionnaire_id: int):
        user = self._user(connection)
        self._require_role(user, ("student",))
        questionnaire = connection.execute(
            "SELECT * FROM questionnaires WHERE id=? AND status='published'", (questionnaire_id,)
        ).fetchone()
        if not questionnaire:
            raise ApiError(404, "量表不存在或未开放")
        body = self._read_json()
        answers = body.get("answers")
        if not isinstance(answers, dict):
            raise ApiError(400, "answers 必须是题目 ID 到选项下标的对象")
        questions = connection.execute(
            "SELECT * FROM questionnaire_questions WHERE questionnaire_id=? ORDER BY order_no",
            (questionnaire_id,),
        ).fetchall()
        normalized: dict[str, int] = {}
        score = 0.0
        crisis = False
        for question in questions:
            key = str(question["id"])
            if key not in answers:
                raise ApiError(400, f"第 {question['order_no']} 题未作答")
            options = json.loads(question["options_json"])
            try:
                option_index = int(answers[key])
            except (TypeError, ValueError):
                raise ApiError(400, "选项下标必须是整数")
            if option_index < 0 or option_index >= len(options):
                raise ApiError(400, "选项超出范围")
            value = len(options) - 1 - option_index if question["reverse_scoring"] else option_index
            score += value
            normalized[key] = option_index
            operator = question["crisis_operator"]
            threshold = question["crisis_value"]
            if operator and threshold is not None:
                if operator == "lte" and value <= float(threshold):
                    crisis = True
                elif operator == "lt" and value < float(threshold):
                    crisis = True
                elif operator == "gte" and value >= float(threshold):
                    crisis = True
                elif operator == "gt" and value > float(threshold):
                    crisis = True
        cursor = connection.execute(
            "INSERT INTO questionnaire_responses(questionnaire_id,student_user_id,student_id,answers_json,score,crisis_flag,submitted_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                questionnaire_id,
                user["id"],
                user["student_id"],
                json.dumps(normalized),
                score,
                1 if crisis else 0,
                iso_now(),
            ),
        )
        if crisis and user["student_id"]:
            due_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
            connection.execute(
                "INSERT INTO referrals(student_id,course_id,source,reason,risk_level,status,due_at,created_at,updated_at) "
                "VALUES(?,?, 'questionnaire','量表触发静默危机规则','high','open',?,?,?)",
                (user["student_id"], questionnaire["course_id"], due_at, iso_now(), iso_now()),
            )
        audit(
            connection,
            int(user["id"]),
            "questionnaire.submit",
            "questionnaire_response",
            cursor.lastrowid,
            {"crisis": crisis},
        )
        return {
            "ok": True,
            "message": "已提交。量表结果仅进入静默通道，不会进入成绩报表；如需立即帮助，请直接联系老师或学校心理中心。",
        }, 201, None

    def api_questionnaire_responses(self, connection: sqlite3.Connection, questionnaire_id: int):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        rows = connection.execute(
            "SELECT qr.*,u.display_name,s.student_no,s.name,s.class_name FROM questionnaire_responses qr "
            "JOIN questionnaires q ON q.id=qr.questionnaire_id "
                "JOIN users u ON u.id=qr.student_user_id LEFT JOIN students s ON s.id=qr.student_id "
                "LEFT JOIN courses c ON c.id=q.course_id "
                "WHERE q.id=? AND (q.course_id IS NULL OR c.tenant_id=?) "
                "ORDER BY qr.submitted_at DESC",
                (questionnaire_id, user["tenant_id"]),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["answers"] = json.loads(item.pop("answers_json") or "{}")
            items.append(item)
        return {"items": items}, 200, None

    def api_silent_requests(self, connection: sqlite3.Connection):
        user = self._user(connection)
        if user["role"] == "student":
            rows = connection.execute(
                "SELECT * FROM silent_requests WHERE student_user_id=? ORDER BY id DESC",
                (user["id"],),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT sr.*,u.display_name,s.student_no,s.name,s.class_name FROM silent_requests sr "
                "JOIN users u ON u.id=sr.student_user_id LEFT JOIN students s ON s.id=sr.student_id "
                "LEFT JOIN courses c ON c.id=sr.course_id WHERE u.tenant_id=? "
                "AND (sr.course_id IS NULL OR c.tenant_id=?) "
                "ORDER BY sr.status='pending' DESC,sr.id DESC LIMIT 300",
                (user["tenant_id"], user["tenant_id"]),
            ).fetchall()
        return {"items": [dict(row) for row in rows]}, 200, None

    def api_create_silent_request(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("student",))
        body = self._read_json()
        cursor = connection.execute(
            "INSERT INTO silent_requests(student_user_id,student_id,course_id,reason,created_at) "
            "VALUES(?,?,?,?,?)",
            (user["id"], user["student_id"], body.get("course_id"), str(body.get("reason", "")), iso_now()),
        )
        audit(connection, int(user["id"]), "silent.request", "silent_request", cursor.lastrowid)
        return {"id": cursor.lastrowid, "status": "pending"}, 201, None

    def api_update_silent_request(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        body = self._read_json()
        request_id = int(body.get("id") or 0)
        status = str(body.get("status", ""))
        if status not in {"approved", "rejected"}:
            raise ApiError(400, "状态只能是 approved 或 rejected")
        existing = connection.execute(
            "SELECT sr.id FROM silent_requests sr JOIN users u ON u.id=sr.student_user_id "
            "LEFT JOIN courses c ON c.id=sr.course_id WHERE sr.id=? AND u.tenant_id=? "
            "AND (sr.course_id IS NULL OR c.tenant_id=?)",
            (request_id, user["tenant_id"], user["tenant_id"]),
        ).fetchone()
        if not existing:
            raise ApiError(404, "静默申请不存在")
        connection.execute(
            "UPDATE silent_requests SET status=?,decided_by=?,decided_at=? WHERE id=?",
            (status, user["id"], iso_now(), request_id),
        )
        audit(connection, int(user["id"]), "silent.decide", "silent_request", request_id, body)
        return {"ok": True}, 200, None

    def api_referrals(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        now = iso_now()
        overdue = connection.execute(
            "SELECT rf.id FROM referrals rf JOIN students s ON s.id=rf.student_id "
            "WHERE rf.status='open' AND rf.due_at<? AND s.tenant_id=?",
            (now, user["tenant_id"]),
        ).fetchall()
        for row in overdue:
            connection.execute(
                "UPDATE referrals SET status='escalated',updated_at=? WHERE id=?",
                (now, row["id"]),
            )
            audit(connection, None, "referral.auto_escalate", "referral", row["id"])
        visibility = ""
        params: list[Any] = []
        if user["role"] == "teacher":
            visibility = " AND (rf.assigned_to IS NULL OR rf.assigned_to=?)"
            params.append(user["id"])
        params.insert(0, user["tenant_id"])
        rows = connection.execute(
            "SELECT rf.*,s.student_no,s.name,s.class_name,u.display_name AS assigned_name "
            "FROM referrals rf JOIN students s ON s.id=rf.student_id "
            "LEFT JOIN users u ON u.id=rf.assigned_to WHERE s.tenant_id=?" + visibility +
            " ORDER BY rf.status='open' DESC,rf.created_at DESC LIMIT 300",
            params,
        ).fetchall()
        assignable = connection.execute(
            "SELECT id,username,display_name,role FROM users "
            "WHERE tenant_id=? AND active=1 AND role IN ('admin','teacher') "
            "ORDER BY display_name,username",
            (user["tenant_id"],)
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "assignable_users": [dict(row) for row in assignable],
        }, 200, None

    def api_create_referral(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        body = self._read_json()
        student_id = int(body.get("student_id") or 0)
        if not student_id and body.get("student_no"):
            student = connection.execute(
                "SELECT id FROM students WHERE tenant_id=? AND student_no=?",
                (user["tenant_id"], str(body["student_no"]).strip()),
            ).fetchone()
            student_id = int(student["id"]) if student else 0
        if not student_id:
            raise ApiError(400, "student_id 或 student_no 必填")
        student = connection.execute(
            "SELECT id FROM students WHERE id=? AND tenant_id=?",
            (student_id, user["tenant_id"]),
        ).fetchone()
        if not student:
            raise ApiError(400, "学生不存在或无权限")
        course_id = body.get("course_id")
        if course_id is not None:
            course = connection.execute(
                "SELECT id FROM courses WHERE id=? AND tenant_id=?",
                (course_id, user["tenant_id"]),
            ).fetchone()
            if not course:
                raise ApiError(400, "课程不存在或无权限")
        assigned_to = body.get("assigned_to") or user["id"]
        assignee = connection.execute(
            "SELECT * FROM users WHERE id=? AND tenant_id=? AND active=1 "
            "AND role IN ('admin','teacher')",
            (assigned_to, user["tenant_id"]),
        ).fetchone()
        if not assignee:
            raise ApiError(400, "分派账号不存在或无处理权限")
        due_hours = float(body.get("due_hours", 2))
        due_at = (datetime.now(timezone.utc) + timedelta(hours=due_hours)).isoformat()
        cursor = connection.execute(
            "INSERT INTO referrals(student_id,course_id,source,reason,risk_level,status,due_at,handler,assigned_to,notes,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'open',?,?,?,?,?,?)",
            (
                student_id,
                course_id,
                str(body.get("source", "teacher")),
                str(body.get("reason", "")),
                str(body.get("risk_level", "high")),
                due_at,
                str(body.get("handler", "") or user["display_name"]),
                assigned_to,
                str(body.get("notes", "")),
                iso_now(),
                iso_now(),
            ),
        )
        audit(connection, int(user["id"]), "referral.create", "referral", cursor.lastrowid, body)
        return {"id": cursor.lastrowid, "due_at": due_at}, 201, None

    def api_update_referral(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin", "teacher"))
        body = self._read_json()
        referral_id = int(body.get("id") or 0)
        status = body.get("status")
        if status and status not in {"open", "reported", "completed", "escalated"}:
            raise ApiError(400, "非法转介状态")
        referral = connection.execute(
            "SELECT rf.* FROM referrals rf JOIN students s ON s.id=rf.student_id "
            "WHERE rf.id=? AND s.tenant_id=?",
            (referral_id, user["tenant_id"]),
        ).fetchone()
        if not referral:
            raise ApiError(404, "转介工单不存在")
        if user["role"] == "teacher" and referral["assigned_to"] not in (None, user["id"]):
            raise ApiError(403, "该工单已分派给其他老师")
        assigned_to = body.get("assigned_to")
        if assigned_to:
            assignee = connection.execute(
                "SELECT id FROM users WHERE id=? AND tenant_id=? AND active=1 "
                "AND role IN ('admin','teacher')",
                (assigned_to, user["tenant_id"]),
            ).fetchone()
            if not assignee:
                raise ApiError(400, "分派账号不存在或无处理权限")
        connection.execute(
            "UPDATE referrals SET status=COALESCE(?,status),handler=COALESCE(?,handler),"
            "assigned_to=COALESCE(?,assigned_to),notes=COALESCE(?,notes),updated_at=? WHERE id=?",
            (
                status,
                body.get("handler"),
                assigned_to,
                body.get("notes"),
                iso_now(),
                referral_id,
            ),
        )
        audit(connection, int(user["id"]), "referral.update", "referral", referral_id, body)
        return {"ok": True}, 200, None

    def api_audit(self, connection: sqlite3.Connection):
        user = self._user(connection)
        self._require_role(user, ("admin",))
        rows = connection.execute(
            "SELECT a.id,a.action,a.object_type,a.object_id,a.detail_json,a.created_at,u.username "
            "FROM audit_events a LEFT JOIN users u ON u.id=a.user_id "
            "WHERE u.tenant_id=? ORDER BY a.id DESC LIMIT 500",
            (user["tenant_id"],)
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json") or "{}")
            items.append(item)
        return {"items": items}, 200, None


def run(host: str = "127.0.0.1", port: int = 8000) -> None:
    from .database import migrate

    migrate()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"双动赋能双驱育心平台已启动：http://{host}:{port}")
    print("默认演示账号：admin/admin123456，teacher/teacher123456，student/student123456（请尽快修改）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
