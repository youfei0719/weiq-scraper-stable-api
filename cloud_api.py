import base64
import queue
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from analytics import incremental_changes, load_result_df, quality_report
from scraper_runtime import (
    CrawlConfig,
    CrawlHooks,
    CrawlRunResult,
    ErrorCode,
    TaskStatus,
    detect_auth_or_challenge,
    run_crawl,
)

DB_PATH = Path("weiq_local.db").resolve()
TASK_QUEUE: "queue.Queue[str]" = queue.Queue()
DB_LOCK = threading.Lock()
ACTIVE_AUTH_LOCK = threading.Lock()
ACTIVE_AUTH_SESSIONS: dict[str, dict[str, Any]] = {}
TASK_TO_SESSION: dict[str, str] = {}

STATUS_ZH = {
    TaskStatus.PENDING: "排队中",
    TaskStatus.RUNNING: "运行中",
    TaskStatus.BLOCKED_AUTH: "等待登录",
    TaskStatus.SUCCESS: "成功",
    TaskStatus.FAILED: "失败",
    TaskStatus.CANCELLED: "已取消",
}
ERROR_MESSAGES_ZH = {
    ErrorCode.NONE: "无错误",
    ErrorCode.INVALID_UID: "账号缺少 uid 参数",
    ErrorCode.HTTP_BLOCKED: "页面被拦截或返回异常状态",
    ErrorCode.TIMEOUT: "页面响应超时",
    ErrorCode.AUTH_REQUIRED: "登录态失效，需要手动登录",
    ErrorCode.CAPTCHA_REQUIRED: "触发风控验证，需要手动处理",
    ErrorCode.EMPTY_PAGE: "页面无有效数据，账号可能失效或未收录",
    ErrorCode.NAVIGATION_ERROR: "页面读取报错",
    ErrorCode.WRITE_ERROR: "结果写入失败",
    ErrorCode.CANCELLED: "任务被取消",
}

app = FastAPI(title="WEIQ Scraper API", version="0.2.0")


class CreateTaskRequest(BaseModel):
    input_excel: str = Field(default="accounts.xlsx")
    output_excel: str = Field(default="weiq_results.xlsx")
    output_dir: str = Field(default=".")
    state_json: str = Field(default="state.json")
    state_storage: str = Field(default="crawl_state.json")
    headless: bool = Field(default=False)
    cooldown_every: int = Field(default=50)
    cooldown_seconds: int = Field(default=180)
    retry_times: int = Field(default=1)
    retry_backoff_seconds: int = Field(default=3)
    resume: bool = Field(default=True)


class TaskControlResponse(BaseModel):
    task_id: str
    status: str
    message: str


class AuthSessionResponse(BaseModel):
    session_id: str
    status: str
    login_url: str | None = None
    page_image_url: str | None = None
    page_image_base64: str | None = None
    qr_image_url: str | None = None
    qr_image_base64: str | None = None
    message: str | None = None
    expires_at: str | None = None
    task_id: str | None = None
    available_login_types: list[str] = Field(default_factory=lambda: ["password", "phone_code"])


class AuthAttachTaskRequest(BaseModel):
    task_id: str


class AuthSubmitRequest(BaseModel):
    login_type: str = Field(default="password")
    action: str = Field(default="submit")
    username: str | None = None
    password: str | None = None
    phone: str | None = None
    verification_code: str | None = None


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def expiry_iso(minutes: int = 10) -> str:
    return (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with DB_LOCK:
        conn = get_conn()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    current_account TEXT,
                    blocked_reason TEXT,
                    error_code TEXT,
                    message TEXT,
                    input_excel TEXT NOT NULL,
                    output_excel TEXT NOT NULL,
                    output_dir TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    state_storage TEXT NOT NULL,
                    headless INTEGER NOT NULL DEFAULT 0,
                    cooldown_every INTEGER NOT NULL DEFAULT 50,
                    cooldown_seconds INTEGER NOT NULL DEFAULT 180,
                    retry_times INTEGER NOT NULL DEFAULT 1,
                    retry_backoff_seconds INTEGER NOT NULL DEFAULT 3,
                    resume INTEGER NOT NULL DEFAULT 1,
                    run_id TEXT,
                    total_accounts INTEGER NOT NULL DEFAULT 0,
                    processed_accounts INTEGER NOT NULL DEFAULT 0,
                    success_accounts INTEGER NOT NULL DEFAULT 0,
                    failed_accounts INTEGER NOT NULL DEFAULT 0,
                    skipped_accounts INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    resume_requested INTEGER NOT NULL DEFAULT 0,
                    login_session_id TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    session_id TEXT PRIMARY KEY,
                    task_id TEXT,
                    status TEXT NOT NULL,
                    login_url TEXT,
                    qr_image_base64 TEXT,
                    message TEXT,
                    expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            task_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if "login_session_id" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN login_session_id TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_tasks_login_session_id ON tasks(login_session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_auth_sessions_task_id ON auth_sessions(task_id)")
            conn.commit()
        finally:
            conn.close()


def execute(sql: str, params: tuple[Any, ...] = ()) -> None:
    with DB_LOCK:
        conn = get_conn()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()


def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> Optional[dict[str, Any]]:
    with DB_LOCK:
        conn = get_conn()
        try:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def upsert_task_event(task_id: str, updates: dict[str, Any]) -> None:
    if not updates:
        return
    cols = []
    vals = []
    for key, value in updates.items():
        cols.append(f"{key} = ?")
        vals.append(value)
    vals.append(task_id)
    execute(f"UPDATE tasks SET {', '.join(cols)} WHERE task_id = ?", tuple(vals))


def upsert_auth_session(session_id: str, updates: dict[str, Any]) -> None:
    row = fetch_one("SELECT session_id FROM auth_sessions WHERE session_id = ?", (session_id,))
    if row is None:
        execute(
            """
            INSERT INTO auth_sessions (
                session_id, task_id, status, login_url, qr_image_base64, message, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                updates.get("task_id"),
                updates.get("status", "pending"),
                updates.get("login_url"),
                updates.get("qr_image_base64"),
                updates.get("message"),
                updates.get("expires_at"),
                updates.get("created_at", now_iso()),
                updates.get("updated_at", now_iso()),
            ),
        )
        return

    if not updates:
        return
    cols = []
    vals = []
    updates = {**updates, "updated_at": updates.get("updated_at", now_iso())}
    for key, value in updates.items():
        cols.append(f"{key} = ?")
        vals.append(value)
    vals.append(session_id)
    execute(f"UPDATE auth_sessions SET {', '.join(cols)} WHERE session_id = ?", tuple(vals))


def fetch_auth_session(session_id: str) -> Optional[dict[str, Any]]:
    return fetch_one("SELECT * FROM auth_sessions WHERE session_id = ?", (session_id,))


def build_status_payload(row: dict[str, Any]) -> dict[str, Any]:
    status = row.get("status", TaskStatus.PENDING)
    error_code = row.get("error_code") or ErrorCode.NONE
    return {
        **row,
        "status_zh": STATUS_ZH.get(status, status),
        "error_message_zh": ERROR_MESSAGES_ZH.get(error_code, row.get("message") or error_code),
        "auth_waiting": status == TaskStatus.BLOCKED_AUTH,
    }


def is_cancel_requested(task_id: str) -> bool:
    row = fetch_one("SELECT cancel_requested FROM tasks WHERE task_id = ?", (task_id,))
    return bool(row and row["cancel_requested"])


def capture_login_screen_base64(page) -> str | None:
    try:
        image_bytes = page.screenshot(type="png", full_page=False)
        return base64.b64encode(image_bytes).decode("utf-8")
    except Exception:
        return None


def build_auth_session_response(row: dict[str, Any]) -> AuthSessionResponse:
    image_base64 = row.get("qr_image_base64")
    return AuthSessionResponse(
        session_id=row["session_id"],
        status=row["status"],
        login_url=row.get("login_url"),
        page_image_url=None,
        page_image_base64=image_base64,
        qr_image_url=None,
        qr_image_base64=image_base64,
        message=row.get("message"),
        expires_at=row.get("expires_at"),
        task_id=row.get("task_id"),
    )


def infer_auth_session_state(page, reason_code: str) -> tuple[str, str]:
    page_text = ""
    try:
        page_text = page.locator("body").inner_text(timeout=1500)
    except Exception:
        page_text = ""
    merged = f"{page.url}\n{page_text}".lower()
    if any(keyword in merged for keyword in ["验证码", "校验码", "短信", "手机验证", "安全验证"]):
        if "发送验证码" in page_text or "获取验证码" in page_text:
            return "waiting_code", "WEIQ 需要手机验证码，请先发送验证码，再输入收到的验证码继续。"
        return "waiting_code", "WEIQ 正在等待验证码或安全验证，请完成后继续。"
    if any(keyword in merged for keyword in ["密码登录", "账号密码", "手机号登录", "登录"]):
        return "waiting_credentials", "WEIQ 需要登录，请填写账号密码，或切换到手机验证码登录。"
    if reason_code == ErrorCode.CAPTCHA_REQUIRED:
        return "waiting_code", ERROR_MESSAGES_ZH.get(reason_code, "等待验证处理")
    return "waiting_credentials", ERROR_MESSAGES_ZH.get(reason_code, "等待登录处理")


def iter_login_targets(page) -> list[Any]:
    targets: list[Any] = [page]
    try:
        for frame in page.frames:
            if frame not in targets:
                targets.append(frame)
    except Exception:
        pass
    return targets


def find_first_visible(targets: list[Any], selectors: list[str]):
    for target in targets:
        for selector in selectors:
            try:
                locator = target.locator(selector)
                count = min(locator.count(), 6)
                for index in range(count):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        return candidate
            except Exception:
                continue
    return None


def fill_first_visible(targets: list[Any], selectors: list[str], value: str) -> bool:
    locator = find_first_visible(targets, selectors)
    if locator is None:
        return False
    locator.click()
    locator.fill("")
    locator.fill(value)
    return True


def click_first_visible(targets: list[Any], selectors: list[str]) -> bool:
    locator = find_first_visible(targets, selectors)
    if locator is None:
        return False
    locator.click()
    return True


def click_first_text(targets: list[Any], texts: list[str]) -> bool:
    selector_pool: list[str] = []
    for text in texts:
        selector_pool.extend(
            [
                f"button:has-text('{text}')",
                f"[role='button']:has-text('{text}')",
                f"a:has-text('{text}')",
                f"text={text}",
            ]
        )
    return click_first_visible(targets, selector_pool)


USERNAME_SELECTORS = [
    "input[placeholder*='手机号']",
    "input[placeholder*='手机号码']",
    "input[placeholder*='账号']",
    "input[placeholder*='用户名']",
    "input[placeholder*='登录账号']",
    "input[name*='mobile' i]",
    "input[name*='phone' i]",
    "input[name*='user' i]",
    "input[name*='account' i]",
    "input[type='tel']",
    "input[type='text']",
]
PASSWORD_SELECTORS = [
    "input[type='password']",
    "input[placeholder*='密码']",
    "input[name*='password' i]",
    "input[name*='pwd' i]",
]
CODE_SELECTORS = [
    "input[placeholder*='验证码']",
    "input[placeholder*='校验码']",
    "input[name*='code' i]",
    "input[name*='verify' i]",
    "input[name*='sms' i]",
]
PASSWORD_TAB_TEXTS = ["账号密码登录", "密码登录", "账号登录"]
PHONE_TAB_TEXTS = ["手机号登录", "手机登录", "短信登录", "验证码登录"]
SEND_CODE_TEXTS = ["获取验证码", "发送验证码", "获取短信验证码", "发送短信验证码"]
SUBMIT_LOGIN_TEXTS = ["登录", "立即登录", "确认登录", "提交", "下一步", "验证并登录"]


def maybe_switch_login_mode(page, login_type: str) -> None:
    targets = iter_login_targets(page)
    if login_type == "phone_code":
        click_first_text(targets, PHONE_TAB_TEXTS)
        return
    click_first_text(targets, PASSWORD_TAB_TEXTS)


def wait_for_page_settle(page, timeout_ms: int = 2500) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        time.sleep(1)


def sanitize_account_text(value: str | None) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def submit_auth_session_inputs(session_id: str, payload: AuthSubmitRequest) -> dict[str, Any]:
    with ACTIVE_AUTH_LOCK:
        session = ACTIVE_AUTH_SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=409, detail="当前登录会话还未进入可操作状态，请等待抓取任务进入登录页后重试")

    page = session["page"]
    context = session["context"]
    state_json = session["state_json"]
    task_id = str(session["task_id"])
    login_type = (payload.login_type or "password").strip().lower()
    action = (payload.action or "submit").strip().lower()

    with session["lock"]:
        maybe_switch_login_mode(page, login_type)
        targets = iter_login_targets(page)

        if login_type == "phone_code":
            phone = sanitize_account_text(payload.phone or payload.username)
            if not phone:
                raise HTTPException(status_code=400, detail="手机号不能为空")
            if not fill_first_visible(targets, USERNAME_SELECTORS, phone):
                raise HTTPException(status_code=422, detail="未找到手机号输入框，请检查当前 WEIQ 登录页")
            if action == "send_code":
                if not click_first_text(targets, SEND_CODE_TEXTS):
                    raise HTTPException(status_code=422, detail="未找到发送验证码按钮，请检查当前 WEIQ 登录页")
                time.sleep(1)
                row = {
                    "status": "waiting_code",
                    "login_url": page.url or session.get("login_url") or "https://www.weiq.com/",
                    "qr_image_base64": capture_login_screen_base64(page),
                    "message": "验证码已尝试发送，请输入收到的短信验证码后继续。",
                    "expires_at": expiry_iso(),
                }
                upsert_auth_session(session_id, row)
                return fetch_auth_session(session_id) or row

            verification_code = sanitize_account_text(payload.verification_code)
            if not verification_code:
                raise HTTPException(status_code=400, detail="验证码不能为空")
            if not fill_first_visible(targets, CODE_SELECTORS, verification_code):
                raise HTTPException(status_code=422, detail="未找到验证码输入框，请检查当前 WEIQ 登录页")
        else:
            username = sanitize_account_text(payload.username or payload.phone)
            password = str(payload.password or "").strip()
            if not username or not password:
                raise HTTPException(status_code=400, detail="账号和密码不能为空")
            if not fill_first_visible(targets, USERNAME_SELECTORS, username):
                raise HTTPException(status_code=422, detail="未找到账号输入框，请检查当前 WEIQ 登录页")
            if not fill_first_visible(targets, PASSWORD_SELECTORS, password):
                raise HTTPException(status_code=422, detail="未找到密码输入框，请检查当前 WEIQ 登录页")

        upsert_auth_session(
            session_id,
            {
                "status": "logging_in",
                "login_url": page.url or session.get("login_url") or "https://www.weiq.com/",
                "qr_image_base64": capture_login_screen_base64(page),
                "message": "已提交登录信息，正在等待 WEIQ 返回结果。",
                "expires_at": expiry_iso(),
            },
        )

        if not click_first_text(targets, SUBMIT_LOGIN_TEXTS):
            raise HTTPException(status_code=422, detail="未找到登录提交按钮，请检查当前 WEIQ 登录页")
        wait_for_page_settle(page)

        needs_auth, reason_code = detect_auth_or_challenge(page)
        if not needs_auth:
            try:
                context.storage_state(path=state_json)
            except Exception:
                pass
            upsert_auth_session(
                session_id,
                {
                    "status": "authenticated",
                    "login_url": page.url,
                    "qr_image_base64": None,
                    "message": "登录成功，抓取任务会自动继续。",
                    "expires_at": expiry_iso(30),
                },
            )
            upsert_task_event(
                task_id,
                {
                    "status": TaskStatus.RUNNING,
                    "blocked_reason": None,
                    "message": "检测到登录成功，任务继续运行",
                },
            )
            unregister_active_session(session_id)
            row = fetch_auth_session(session_id)
            assert row is not None
            return row

        status_text, message = infer_auth_session_state(page, reason_code)
        row = {
            "status": status_text,
            "login_url": page.url or session.get("login_url") or "https://www.weiq.com/",
            "qr_image_base64": capture_login_screen_base64(page),
            "message": message,
            "expires_at": expiry_iso(),
        }
        upsert_auth_session(session_id, row)
        upsert_task_event(
            task_id,
            {
                "status": TaskStatus.BLOCKED_AUTH,
                "blocked_reason": reason_code,
                "message": message,
            },
        )
        return fetch_auth_session(session_id) or row


def register_active_session(
    session_id: str,
    *,
    task_id: str,
    page,
    context,
    state_json: str,
    login_url: str,
) -> None:
    with ACTIVE_AUTH_LOCK:
        ACTIVE_AUTH_SESSIONS[session_id] = {
            "task_id": task_id,
            "page": page,
            "context": context,
            "state_json": state_json,
            "login_url": login_url,
            "lock": threading.RLock(),
        }
        TASK_TO_SESSION[task_id] = session_id


def unregister_active_session(session_id: str) -> None:
    with ACTIVE_AUTH_LOCK:
        session = ACTIVE_AUTH_SESSIONS.pop(session_id, None)
        if session:
            TASK_TO_SESSION.pop(str(session.get("task_id") or ""), None)


def migrate_active_session(old_session_id: str, new_session_id: str, task_id: str) -> None:
    if old_session_id == new_session_id:
        return
    with ACTIVE_AUTH_LOCK:
        session = ACTIVE_AUTH_SESSIONS.pop(old_session_id, None)
        if session is None:
            return
        ACTIVE_AUTH_SESSIONS[new_session_id] = session
        TASK_TO_SESSION[task_id] = new_session_id


def ensure_auth_session_for_task(task_id: str, reason_code: str, page_url: str, page, context, state_json: str) -> str:
    row = fetch_one("SELECT login_session_id FROM tasks WHERE task_id = ?", (task_id,))
    session_id = str(row.get("login_session_id") or "").strip() if row else ""
    if not session_id:
        session_id = uuid4().hex
        upsert_task_event(task_id, {"login_session_id": session_id})
    status_text, message = infer_auth_session_state(page, reason_code)
    qr_image_base64 = capture_login_screen_base64(page)
    upsert_auth_session(
        session_id,
        {
            "task_id": task_id,
            "status": status_text,
            "login_url": page_url or "https://www.weiq.com/",
            "qr_image_base64": qr_image_base64,
            "message": message,
            "expires_at": expiry_iso(),
        },
    )
    register_active_session(
        session_id,
        task_id=task_id,
        page=page,
        context=context,
        state_json=state_json,
        login_url=page_url or "https://www.weiq.com/",
    )
    return session_id


def inspect_active_auth_session(session_id: str) -> Optional[dict[str, Any]]:
    with ACTIVE_AUTH_LOCK:
        session = ACTIVE_AUTH_SESSIONS.get(session_id)
    if not session:
        return None
    page = session["page"]
    context = session["context"]
    state_json = session["state_json"]
    with session["lock"]:
        needs_auth, reason_code = detect_auth_or_challenge(page)
        if not needs_auth:
            try:
                context.storage_state(path=state_json)
            except Exception:
                pass
            upsert_auth_session(
                session_id,
                {
                    "status": "authenticated",
                    "login_url": page.url,
                    "qr_image_base64": None,
                    "message": "登录成功，任务将自动继续",
                    "expires_at": expiry_iso(30),
                },
            )
            upsert_task_event(
                str(session["task_id"]),
                {
                    "status": TaskStatus.RUNNING,
                    "blocked_reason": None,
                    "message": "检测到登录成功，任务继续运行",
                },
            )
            unregister_active_session(session_id)
        else:
            status_text, message = infer_auth_session_state(page, reason_code)
            upsert_auth_session(
                session_id,
                {
                    "status": status_text,
                    "login_url": page.url or session.get("login_url") or "https://www.weiq.com/",
                    "qr_image_base64": capture_login_screen_base64(page),
                    "message": message,
                    "expires_at": expiry_iso(),
                },
            )
    return fetch_auth_session(session_id)


def wait_for_auth_or_cancel(task_id: str, session_id: str, page, context, state_json: str) -> bool:
    while True:
        row = fetch_one(
            "SELECT cancel_requested, resume_requested FROM tasks WHERE task_id = ?",
            (task_id,),
        )
        if not row:
            unregister_active_session(session_id)
            return False
        if row["cancel_requested"]:
            upsert_auth_session(session_id, {"status": "failed", "message": "任务已取消"})
            unregister_active_session(session_id)
            return False

        session_row = inspect_active_auth_session(session_id)
        if session_row and session_row.get("status") == "authenticated":
            return True

        if row["resume_requested"]:
            try:
                context.storage_state(path=state_json)
            except Exception:
                pass
            upsert_task_event(
                task_id,
                {
                    "resume_requested": 0,
                    "status": TaskStatus.RUNNING,
                    "blocked_reason": None,
                    "message": "已收到继续请求，恢复运行",
                },
            )
            upsert_auth_session(session_id, {"status": "authenticated", "message": "已手动请求继续"})
            unregister_active_session(session_id)
            return True
        time.sleep(1)


def build_hooks(task_id: str) -> CrawlHooks:
    def on_event(event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "task_status":
            status = event.get("status", TaskStatus.RUNNING)
            updates = {
                "status": status,
                "error_code": event.get("error_code", ErrorCode.NONE),
                "run_id": event.get("run_id"),
            }
            if status == TaskStatus.RUNNING and event.get("started_at"):
                updates["started_at"] = event["started_at"]
                updates["message"] = "任务运行中"
            if status in {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                updates["finished_at"] = event.get("finished_at", now_iso())
            upsert_task_event(task_id, updates)

        elif event_type == "progress":
            upsert_task_event(
                task_id,
                {
                    "status": TaskStatus.RUNNING,
                    "progress": event.get("progress", 0.0),
                    "current_account": event.get("current_account"),
                    "processed_accounts": event.get("processed", 0),
                    "total_accounts": event.get("total", 0),
                    "error_code": event.get("error_code", ErrorCode.NONE),
                    "message": "任务运行中",
                },
            )

        elif event_type == "auth_required":
            upsert_task_event(
                task_id,
                {
                    "status": TaskStatus.BLOCKED_AUTH,
                    "blocked_reason": event.get("reason_code"),
                    "message": "等待 WEIQ 登录或验证码处理",
                },
            )

    def should_stop() -> bool:
        return is_cancel_requested(task_id)

    def on_auth_required(reason_code: str, page_url: str, page, context, state_json: str) -> bool:
        session_id = ensure_auth_session_for_task(task_id, reason_code, page_url, page, context, state_json)
        upsert_task_event(
            task_id,
            {
                "status": TaskStatus.BLOCKED_AUTH,
                "blocked_reason": reason_code,
                "message": f"等待处理登录风控: {page_url}",
                "login_session_id": session_id,
            },
        )
        return wait_for_auth_or_cancel(task_id, session_id, page, context, state_json)

    return CrawlHooks(on_event=on_event, should_stop=should_stop, on_auth_required=on_auth_required)


def run_task(task_id: str) -> None:
    row = fetch_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    if not row:
        return

    if row["cancel_requested"]:
        upsert_task_event(
            task_id,
            {
                "status": TaskStatus.CANCELLED,
                "finished_at": now_iso(),
                "error_code": ErrorCode.CANCELLED,
                "message": "任务在启动前已取消",
            },
        )
        return

    config = CrawlConfig(
        input_excel=row["input_excel"],
        output_excel=row["output_excel"],
        output_dir=row["output_dir"],
        state_json=row["state_json"],
        state_storage=row["state_storage"],
        headless=bool(row["headless"]),
        cooldown_every=row["cooldown_every"],
        cooldown_seconds=row["cooldown_seconds"],
        retry_times=max(1, row["retry_times"]),
        retry_backoff_seconds=max(0, row["retry_backoff_seconds"]),
        resume=bool(row["resume"]),
    )

    hooks = build_hooks(task_id)

    upsert_task_event(
        task_id,
        {
            "status": TaskStatus.RUNNING,
            "started_at": now_iso(),
            "message": "任务已启动",
        },
    )

    try:
        result: CrawlRunResult = run_crawl(config=config, hooks=hooks)
        upsert_task_event(
            task_id,
            {
                "status": result.status,
                "run_id": result.run_id,
                "progress": 1.0 if result.total_accounts == 0 else result.processed_accounts / result.total_accounts,
                "processed_accounts": result.processed_accounts,
                "total_accounts": result.total_accounts,
                "success_accounts": result.success_accounts,
                "failed_accounts": result.failed_accounts,
                "skipped_accounts": result.skipped_accounts,
                "output_excel": result.output_excel,
                "error_code": result.error_code,
                "finished_at": result.finished_at,
                "message": "任务已完成" if result.status == TaskStatus.SUCCESS else "任务已结束",
            },
        )
    except Exception as exc:
        upsert_task_event(
            task_id,
            {
                "status": TaskStatus.FAILED,
                "finished_at": now_iso(),
                "error_code": "RUNTIME_CRASH",
                "message": f"任务异常崩溃: {exc}",
            },
        )


def worker_loop() -> None:
    while True:
        task_id = TASK_QUEUE.get()
        try:
            run_task(task_id)
        finally:
            TASK_QUEUE.task_done()


def start_worker() -> None:
    thread = threading.Thread(target=worker_loop, daemon=True)
    thread.start()


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    start_worker()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "time": now_iso()}


@app.post("/v1/tasks/crawl", response_model=TaskControlResponse)
def create_task(payload: CreateTaskRequest) -> TaskControlResponse:
    task_id = uuid4().hex
    created_at = now_iso()

    execute(
        """
        INSERT INTO tasks (
            task_id, status, progress, current_account, blocked_reason, error_code, message,
            input_excel, output_excel, output_dir, state_json, state_storage,
            headless, cooldown_every, cooldown_seconds, retry_times, retry_backoff_seconds, resume,
            login_session_id, created_at
        ) VALUES (?, ?, 0, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            task_id,
            TaskStatus.PENDING,
            ErrorCode.NONE,
            "任务已创建，等待执行",
            payload.input_excel,
            payload.output_excel,
            payload.output_dir,
            payload.state_json,
            payload.state_storage,
            int(payload.headless),
            payload.cooldown_every,
            payload.cooldown_seconds,
            payload.retry_times,
            payload.retry_backoff_seconds,
            int(payload.resume),
            created_at,
        ),
    )

    TASK_QUEUE.put(task_id)
    return TaskControlResponse(task_id=task_id, status=TaskStatus.PENDING, message="任务已创建")


@app.get("/v1/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    row = fetch_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    return build_status_payload(row)


@app.post("/v1/tasks/{task_id}/cancel", response_model=TaskControlResponse)
def cancel_task(task_id: str) -> TaskControlResponse:
    row = fetch_one("SELECT status, login_session_id FROM tasks WHERE task_id = ?", (task_id,))
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")

    if row["status"] in {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}:
        return TaskControlResponse(task_id=task_id, status=row["status"], message="任务已是终态")

    upsert_task_event(task_id, {"cancel_requested": 1, "message": "已请求取消任务"})
    if row.get("login_session_id"):
        upsert_auth_session(str(row["login_session_id"]), {"status": "failed", "message": "任务已取消"})
        unregister_active_session(str(row["login_session_id"]))
    return TaskControlResponse(task_id=task_id, status=TaskStatus.CANCELLED, message="取消请求已发送")


@app.post("/v1/tasks/{task_id}/resume", response_model=TaskControlResponse)
def resume_task(task_id: str) -> TaskControlResponse:
    row = fetch_one("SELECT status FROM tasks WHERE task_id = ?", (task_id,))
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")

    if row["status"] != TaskStatus.BLOCKED_AUTH:
        return TaskControlResponse(task_id=task_id, status=row["status"], message="当前任务不在等待登录状态")

    upsert_task_event(task_id, {"resume_requested": 1, "message": "已请求继续任务"})
    return TaskControlResponse(task_id=task_id, status=TaskStatus.RUNNING, message="继续请求已发送")


@app.post("/v1/auth/session", response_model=AuthSessionResponse)
def create_auth_session() -> AuthSessionResponse:
    session_id = uuid4().hex
    upsert_auth_session(
        session_id,
        {
            "task_id": None,
            "status": "pending",
            "login_url": "https://www.weiq.com/",
            "qr_image_base64": None,
            "message": "登录会话已创建，等待绑定抓取任务",
            "expires_at": expiry_iso(),
        },
    )
    row = fetch_auth_session(session_id)
    assert row is not None
    return build_auth_session_response(row)


@app.get("/v1/auth/session/{session_id}", response_model=AuthSessionResponse)
def get_auth_session(session_id: str) -> AuthSessionResponse:
    row = inspect_active_auth_session(session_id) or fetch_auth_session(session_id)
    if not row:
        raise HTTPException(status_code=404, detail="登录会话不存在")
    return build_auth_session_response(row)


@app.post("/v1/auth/session/{session_id}/attach-task", response_model=AuthSessionResponse)
def attach_auth_session_to_task(session_id: str, payload: AuthAttachTaskRequest) -> AuthSessionResponse:
    task = fetch_one("SELECT task_id, login_session_id, status FROM tasks WHERE task_id = ?", (payload.task_id,))
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    existing = fetch_auth_session(session_id)
    if existing is None:
        upsert_auth_session(
            session_id,
            {
                "task_id": payload.task_id,
                "status": "pending",
                "login_url": "https://www.weiq.com/",
                "qr_image_base64": None,
                "message": "登录会话已绑定任务",
                "expires_at": expiry_iso(),
            },
        )

    previous_session_id = str(task.get("login_session_id") or "").strip()
    if previous_session_id:
        migrate_active_session(previous_session_id, session_id, payload.task_id)
        previous_row = fetch_auth_session(previous_session_id)
        if previous_row:
            upsert_auth_session(
                session_id,
                {
                    "task_id": payload.task_id,
                    "status": previous_row.get("status", "waiting_credentials"),
                    "login_url": previous_row.get("login_url"),
                    "qr_image_base64": previous_row.get("qr_image_base64"),
                    "message": previous_row.get("message"),
                    "expires_at": previous_row.get("expires_at"),
                },
            )

    upsert_task_event(payload.task_id, {"login_session_id": session_id})
    upsert_auth_session(session_id, {"task_id": payload.task_id, "message": "登录会话已绑定任务"})
    return get_auth_session(session_id)


@app.post("/v1/auth/session/{session_id}/submit", response_model=AuthSessionResponse)
def submit_auth_session(session_id: str, payload: AuthSubmitRequest) -> AuthSessionResponse:
    row = submit_auth_session_inputs(session_id, payload)
    return build_auth_session_response(row)


@app.get("/v1/tasks/{task_id}/latest")
def get_task_latest(task_id: str, limit: int = 20) -> dict[str, Any]:
    row = fetch_one("SELECT output_excel, run_id FROM tasks WHERE task_id = ?", (task_id,))
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")

    output_excel = row.get("output_excel")
    run_id = row.get("run_id")
    if not output_excel or not Path(output_excel).exists():
        return {"task_id": task_id, "records": [], "count": 0}

    df = pd.read_excel(output_excel)
    if run_id and "run_id" in df.columns:
        df = df[df["run_id"] == run_id]

    if df.empty:
        return {"task_id": task_id, "records": [], "count": 0}

    df = df.tail(max(1, min(200, limit)))
    return {
        "task_id": task_id,
        "run_id": run_id,
        "count": len(df),
        "records": df.to_dict(orient="records"),
    }


@app.get("/v1/accounts/{uid}/changes")
def get_account_changes(uid: str, output_excel: str = "weiq_results.xlsx", limit: int = 30) -> dict[str, Any]:
    excel_path = Path(output_excel)
    if not excel_path.exists():
        raise HTTPException(status_code=404, detail="结果文件不存在")
    df = load_result_df(str(excel_path))
    return incremental_changes(df=df, uid=uid, limit=limit)


@app.get("/v1/tasks/{task_id}/quality")
def get_task_quality(task_id: str) -> dict[str, Any]:
    row = fetch_one("SELECT output_excel, run_id FROM tasks WHERE task_id = ?", (task_id,))
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    output_excel = row.get("output_excel")
    run_id = row.get("run_id")
    if not output_excel or not Path(output_excel).exists():
        raise HTTPException(status_code=404, detail="结果文件不存在")
    df = load_result_df(output_excel)
    return quality_report(df=df, run_id=run_id)
