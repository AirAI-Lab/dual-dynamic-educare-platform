"""SQLite 数据层：建表、默认参数、账号与审计基础能力。"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "platform" / "dds.sqlite3"


DEFAULT_SETTINGS = {
    "green_threshold": 90.0,
    "blue_threshold": 60.0,
    "coefficient_green_threshold": 0.80,
    "coefficient_blue_threshold": 0.80,
    "coefficient_red_threshold": 0.50,
    "observation_threshold": 1.00,
    "material_page_weight": 0.60,
    "material_interaction_weight": 0.40,
    "adg_window_days": 42,
    "adg_weights_recent_to_old": [0.50, 0.30, 0.20],
    "idg_period_days": 3,
    "idg_final_term_weight": 0.40,
    "anti_cheat_min_page_stay_seconds": 5,
    "anti_cheat_rush_seconds": 30,
    "announcement_min_stay_seconds": 15,
    "low_stakes_mode": True,
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','teacher','student')),
    display_name TEXT NOT NULL,
    student_id INTEGER REFERENCES students(id),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(tenant_id, username)
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    code TEXT NOT NULL,
    source_platform TEXT NOT NULL DEFAULT '雨课堂',
    academic_year TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    UNIQUE(tenant_id, code)
);

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    student_no TEXT NOT NULL,
    name TEXT NOT NULL,
    college TEXT NOT NULL DEFAULT '',
    class_name TEXT NOT NULL DEFAULT '',
    anonymous_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(tenant_id, student_no)
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    user_id INTEGER REFERENCES users(id),
    kind TEXT NOT NULL CHECK(kind IN ('realtime','history')),
    filename TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    status TEXT NOT NULL,
    period_date TEXT,
    rows_read INTEGER NOT NULL DEFAULT 0,
    rows_imported INTEGER NOT NULL DEFAULT 0,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS measures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    student_id INTEGER NOT NULL REFERENCES students(id),
    import_id INTEGER REFERENCES imports(id),
    period_date TEXT NOT NULL,
    total_score REAL,
    full_score REAL,
    effective_pages REAL,
    total_pages REAL,
    interaction_count REAL,
    interaction_standard REAL,
    announcement_complete REAL,
    total_announcements REAL,
    announcement_read_seconds REAL,
    announcement_media_loaded INTEGER,
    video_seconds REAL,
    total_video_seconds REAL,
    page_stay_seconds REAL,
    page_completion_seconds REAL,
    source_fields_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(course_id, student_id, period_date)
);

CREATE TABLE IF NOT EXISTS historical_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    student_id INTEGER NOT NULL REFERENCES students(id),
    import_id INTEGER REFERENCES imports(id),
    period_date TEXT NOT NULL,
    idg REAL NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(course_id, student_id, period_date)
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    period_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','reviewed','published','revoked')),
    generated_by INTEGER REFERENCES users(id),
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TEXT,
    published_by INTEGER REFERENCES users(id),
    published_at TEXT,
    revoked_by INTEGER REFERENCES users(id),
    revoked_at TEXT,
    revoke_reason TEXT NOT NULL DEFAULT '',
    summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(course_id, period_date)
);

CREATE TABLE IF NOT EXISTS report_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES students(id),
    course_percent REAL NOT NULL,
    material_coeff REAL NOT NULL,
    announcement_coeff REAL NOT NULL,
    video_coeff REAL,
    interaction_coeff REAL,
    idg REAL NOT NULL,
    adg REAL NOT NULL,
    score_color TEXT NOT NULL,
    state_type TEXT NOT NULL,
    flags_json TEXT NOT NULL DEFAULT '[]',
    profile_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    guidance TEXT NOT NULL DEFAULT '',
    UNIQUE(report_id, student_id)
);

CREATE TABLE IF NOT EXISTS interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_item_id INTEGER REFERENCES report_items(id) ON DELETE SET NULL,
    student_id INTEGER NOT NULL REFERENCES students(id),
    course_id INTEGER NOT NULL REFERENCES courses(id),
    mode TEXT NOT NULL DEFAULT 'navigation',
    category TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','contacted','closed')),
    owner_id INTEGER REFERENCES users(id),
    result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questionnaires (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER REFERENCES courses(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','closed')),
    sensitive INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questionnaire_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    questionnaire_id INTEGER NOT NULL REFERENCES questionnaires(id) ON DELETE CASCADE,
    order_no INTEGER NOT NULL,
    stem TEXT NOT NULL,
    options_json TEXT NOT NULL,
    reverse_scoring INTEGER NOT NULL DEFAULT 0,
    crisis_operator TEXT NOT NULL DEFAULT '',
    crisis_value REAL,
    UNIQUE(questionnaire_id, order_no)
);

CREATE TABLE IF NOT EXISTS questionnaire_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    questionnaire_id INTEGER NOT NULL REFERENCES questionnaires(id),
    student_user_id INTEGER NOT NULL REFERENCES users(id),
    student_id INTEGER REFERENCES students(id),
    answers_json TEXT NOT NULL,
    score REAL,
    crisis_flag INTEGER NOT NULL DEFAULT 0,
    submitted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS silent_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_user_id INTEGER NOT NULL REFERENCES users(id),
    student_id INTEGER REFERENCES students(id),
    course_id INTEGER REFERENCES courses(id),
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
    decided_by INTEGER REFERENCES users(id),
    decided_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id),
    course_id INTEGER REFERENCES courses(id),
    source TEXT NOT NULL DEFAULT 'questionnaire',
    reason TEXT NOT NULL DEFAULT '',
    risk_level TEXT NOT NULL DEFAULT 'high',
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','reported','completed','escalated')),
    due_at TEXT NOT NULL,
    handler TEXT NOT NULL DEFAULT '',
    assigned_to INTEGER REFERENCES users(id),
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    object_type TEXT NOT NULL DEFAULT '',
    object_id TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK(id=1),
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_measures_course_date ON measures(course_id, period_date);
CREATE INDEX IF NOT EXISTS idx_history_course_date ON historical_scores(course_id, period_date);
CREATE INDEX IF NOT EXISTS idx_report_items_report ON report_items(report_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(created_at);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso_now() -> str:
    return utc_now().isoformat()


def db_path() -> Path:
    return Path(os.environ.get("DDS_DB_PATH", DEFAULT_DB_PATH))


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return f"pbkdf2${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, salt_hex, digest_hex = stored.split("$", 2)
        if algorithm != "pbkdf2":
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 120000
        )
        return hmac.compare_digest(derived.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def get_settings(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute("SELECT value_json FROM settings WHERE id=1").fetchone()
    result = dict(DEFAULT_SETTINGS)
    if row:
        result.update(json.loads(row["value_json"]))
    return result


def save_settings(connection: sqlite3.Connection, values: dict[str, Any]) -> dict[str, Any]:
    current = get_settings(connection)
    current.update(values)
    connection.execute(
        "INSERT INTO settings(id,value_json,updated_at) VALUES(1,?,?) "
        "ON CONFLICT(id) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
        (json.dumps(current, ensure_ascii=False), iso_now()),
    )
    return current


def audit(
    connection: sqlite3.Connection,
    user_id: int | None,
    action: str,
    object_type: str = "",
    object_id: str | int = "",
    detail: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        "INSERT INTO audit_events(user_id,action,object_type,object_id,detail_json,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (
            user_id,
            action,
            object_type,
            str(object_id),
            json.dumps(detail or {}, ensure_ascii=False, default=str),
            iso_now(),
        ),
    )


def migrate() -> None:
    with connect() as connection:
        connection.executescript(SCHEMA)
        # 兼容已创建的 V2.1 轻量库：为心理转介工单补充分派教师。
        referral_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(referrals)").fetchall()
        }
        if "assigned_to" not in referral_columns:
            connection.execute(
                "ALTER TABLE referrals ADD COLUMN assigned_to INTEGER REFERENCES users(id)"
            )
        now = iso_now()
        connection.execute(
            "INSERT OR IGNORE INTO tenants(id,name,code,created_at) "
            "VALUES(1,'默认学校','default',?)",
            (now,),
        )

        row = connection.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        if not row["count"]:
            seeded = [
                ("admin", "admin123456", "admin", "系统管理员"),
                ("teacher", "teacher123456", "teacher", "演示教师"),
                ("student", "student123456", "student", "演示学生"),
            ]
            for username, password, role, display_name in seeded:
                connection.execute(
                    "INSERT INTO users(tenant_id,username,password_hash,role,display_name,created_at) "
                    "VALUES(1,?,?,?,?,?)",
                    (username, hash_password(password), role, display_name, now),
                )

        save_settings(connection, {})

        # 示例量表：仅演示计分/反向计分/危机触发与白名单机制；
        # CD-RISC-25 等正式量表条目应由学校按授权导入，不在代码中内置。
        questionnaire_count = connection.execute(
            "SELECT COUNT(*) AS count FROM questionnaires"
        ).fetchone()["count"]
        if not questionnaire_count:
            cursor = connection.execute(
                "INSERT INTO questionnaires(course_id,title,description,status,sensitive,created_by,created_at) "
                "VALUES(NULL,'学习状态与支持需求自评（示例）','用于演示量表引擎；结果仅进入静默通道，不进入成绩报表。','published',1,NULL,?)",
                (now,),
            )
            questionnaire_id = cursor.lastrowid
            options = json.dumps(
                ["从不", "偶尔", "有时", "经常", "总是"], ensure_ascii=False
            )
            questions = [
                (1, "最近一周，我能按计划开始学习任务。", options, 0, "", None),
                (2, "最近一周，我很难坚持完成学习计划。", options, 1, "", None),
                (3, "遇到学习困难时，我愿意向老师或同学求助。", options, 0, "", None),
                (4, "最近一周，我感到身边有可以支持我的人。", options, 0, "", None),
                (
                    5,
                    "最近一周，我感到基本安全和稳定。",
                    options,
                    0,
                    "lte",
                    1,
                ),
            ]
            connection.executemany(
                "INSERT INTO questionnaire_questions(questionnaire_id,order_no,stem,options_json,reverse_scoring,crisis_operator,crisis_value) "
                "VALUES(?,?,?,?,?,?,?)",
                [(questionnaire_id, *question) for question in questions],
            )

        # 清理过期会话。
        connection.execute(
            "DELETE FROM sessions WHERE expires_at < ?", (iso_now(),)
        )


def create_session(connection: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (utc_now() + timedelta(hours=12)).isoformat()
    connection.execute(
        "INSERT INTO sessions(token,user_id,expires_at,created_at) VALUES(?,?,?,?)",
        (token, user_id, expires, iso_now()),
    )
    return token
