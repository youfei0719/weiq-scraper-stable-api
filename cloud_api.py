import base64
import os
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
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from analytics import incremental_changes, load_result_df, quality_report
from scraper_runtime import (
    CrawlConfig,
    CrawlHooks,
    CrawlRunResult,
    ErrorCode,
    TaskStatus,
    detect_auth_or_challenge,
    extract_metrics,
    has_usable_storage_state,
    init_browser,
    infer_post_extraction_issue,
    run_crawl,
)
from playwright.sync_api import sync_playwright

DB_PATH = Path("weiq_local.db").resolve()
TASK_QUEUE: "queue.Queue[str]" = queue.Queue()
DB_LOCK = threading.Lock()
ACTIVE_AUTH_LOCK = threading.Lock()
ACTIVE_AUTH_SESSIONS: dict[str, dict[str, Any]] = {}
TASK_TO_SESSION: dict[str, str] = {}
QUEUE_LOCK = threading.Lock()
QUEUED_TASK_IDS: set[str] = set()
ACTIVE_TASK_IDS: set[str] = set()
WORKER_THREAD: threading.Thread | None = None
WORKER_STARTED_AT: str | None = None
LAST_WORKER_ERROR: str | None = None

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


class AccountInput(BaseModel):
    nickname: str | None = None
    uid: str
    account_id: str | None = None


class CreateTaskRequest(BaseModel):
    accounts: list[AccountInput] | None = None
    input_excel: str | None = Field(default=None)
    output_excel: str | None = Field(default=None)
    output_dir: str | None = Field(default=None)
    state_json: str | None = Field(default=None)
    state_storage: str | None = Field(default=None)
    headless: bool = Field(default=True)
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


class AuthSessionCreateRequest(BaseModel):
    task_id: str | None = None
    eager: bool = Field(default=False)


class AuthAttachTaskRequest(BaseModel):
    task_id: str


class AuthSubmitRequest(BaseModel):
    login_type: str = Field(default="password")
    action: str = Field(default="submit")
    username: str | None = None
    password: str | None = None
    phone: str | None = None
    verification_code: str | None = None


class WorkerHealthResponse(BaseModel):
    worker_alive: bool
    worker_started_at: str | None = None
    queue_size: int
    queued_task_ids: list[str]
    pending_count: int
    running_count: int
    last_worker_error: str | None = None
    process_id: int
    db_path: str


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def expiry_iso(minutes: int = 10) -> str:
    return (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def get_runtime_dir() -> Path:
    runtime_dir = Path(os.getenv("WEIQ_API_RUNTIME_DIR", "./runtime")).expanduser()
    if not runtime_dir.is_absolute():
        runtime_dir = (Path.cwd() / runtime_dir).resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def ensure_single_worker_mode() -> None:
    for env_name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        raw = os.getenv(env_name, "").strip()
        if raw.isdigit() and int(raw) > 1:
            raise RuntimeError("当前 WEIQ API 仅支持单 worker 进程模式启动，请使用 --workers 1")


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
                    accepted_at TEXT,
                    picked_up_at TEXT,
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
            if "accepted_at" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN accepted_at TEXT")
            if "picked_up_at" not in task_cols:
                conn.execute("ALTER TABLE tasks ADD COLUMN picked_up_at TEXT")
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


def fetch_value(sql: str, params: tuple[Any, ...] = ()) -> int:
    with DB_LOCK:
        conn = get_conn()
        try:
            row = conn.execute(sql, params).fetchone()
            if row is None:
                return 0
            return int(row[0] or 0)
        finally:
            conn.close()


def enqueue_task(task_id: str) -> bool:
    with QUEUE_LOCK:
        if task_id in QUEUED_TASK_IDS or task_id in ACTIVE_TASK_IDS:
            return False
        QUEUED_TASK_IDS.add(task_id)
        TASK_QUEUE.put(task_id)
        return True


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


def materialize_task_request(payload: CreateTaskRequest) -> dict[str, Any]:
    runtime_dir = get_runtime_dir()
    task_key = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"

    if payload.accounts:
        accounts = []
        for item in payload.accounts:
            uid = str(item.uid or "").strip()
            if not uid:
                continue
            account_id = str(item.account_id or item.nickname or uid).strip() or uid
            accounts.append({"账号ID": account_id, "uid": uid})
        if not accounts:
            raise HTTPException(status_code=400, detail="accounts 不能为空，且每个账号必须带 uid")

        input_dir = runtime_dir / "inputs"
        output_dir = runtime_dir / "outputs" / task_key
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        input_excel = input_dir / f"accounts_{task_key}.xlsx"
        pd.DataFrame(accounts).to_excel(input_excel, index=False)
        return {
            "input_excel": str(input_excel.resolve()),
            "output_excel": f"weiq_results_{task_key}.xlsx",
            "output_dir": str(output_dir.resolve()),
            "state_json": str((runtime_dir / "state.json").resolve()),
            "state_storage": str((runtime_dir / "crawl_state.json").resolve()),
            "headless": payload.headless if payload.headless is not None else True,
            "cooldown_every": payload.cooldown_every,
            "cooldown_seconds": payload.cooldown_seconds,
            "retry_times": payload.retry_times,
            "retry_backoff_seconds": payload.retry_backoff_seconds,
            "resume": payload.resume,
        }

    input_excel = str(payload.input_excel or "").strip()
    if not input_excel:
        raise HTTPException(status_code=400, detail="缺少 input_excel，或请改用 accounts JSON 模式")
    output_dir = str(payload.output_dir or runtime_dir).strip() or str(runtime_dir)
    output_excel = str(payload.output_excel or f"weiq_results_{task_key}.xlsx").strip()
    state_json = str(payload.state_json or (runtime_dir / "state.json")).strip()
    state_storage = str(payload.state_storage or (runtime_dir / "crawl_state.json")).strip()
    return {
        "input_excel": input_excel,
        "output_excel": output_excel,
        "output_dir": output_dir,
        "state_json": state_json,
        "state_storage": state_storage,
        "headless": payload.headless if payload.headless is not None else True,
        "cooldown_every": payload.cooldown_every,
        "cooldown_seconds": payload.cooldown_seconds,
        "retry_times": payload.retry_times,
        "retry_backoff_seconds": payload.retry_backoff_seconds,
        "resume": payload.resume,
    }


def build_status_payload(row: dict[str, Any]) -> dict[str, Any]:
    status = row.get("status", TaskStatus.PENDING)
    error_code = row.get("error_code") or ErrorCode.NONE
    message = row.get("message") or None
    auth_session_status = None
    login_session_id = str(row.get("login_session_id") or "").strip()
    if login_session_id:
        auth_session = fetch_auth_session(login_session_id)
        auth_session_status = str(auth_session.get("status") or "").strip() if auth_session else None
    export_file = None
    output_excel = str(row.get("output_excel") or "").strip()
    output_dir = str(row.get("output_dir") or "").strip()
    if output_excel:
        output_path = Path(output_excel)
        if not output_path.is_absolute():
            output_path = Path(output_dir or ".") / output_path
        export_file = str(output_path.resolve())
    if message and (error_code == ErrorCode.NONE or status in {TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.BLOCKED_AUTH}):
        error_message_zh = message
    else:
        error_message_zh = ERROR_MESSAGES_ZH.get(error_code, message or error_code)
    accepted_at = str(row.get("accepted_at") or row.get("created_at") or "").strip() or None
    picked_up_at = str(row.get("picked_up_at") or row.get("started_at") or "").strip() or None
    queue_age_seconds = None
    accepted_dt = parse_iso(accepted_at)
    picked_up_dt = parse_iso(picked_up_at)
    if accepted_dt is not None:
        if status == TaskStatus.PENDING and picked_up_dt is None:
            queue_age_seconds = max(0, int((datetime.now() - accepted_dt).total_seconds()))
        elif picked_up_dt is not None:
            queue_age_seconds = max(0, int((picked_up_dt - accepted_dt).total_seconds()))
    with QUEUE_LOCK:
        queue_size = TASK_QUEUE.qsize()
    worker_alive = bool(WORKER_THREAD and WORKER_THREAD.is_alive())
    needs_login = bool(
        status == TaskStatus.BLOCKED_AUTH
        or auth_session_status in {"pending", "waiting_credentials", "waiting_code", "logging_in"}
    )
    return {
        **row,
        "status_zh": STATUS_ZH.get(status, status),
        "error_message_zh": error_message_zh,
        "auth_waiting": status == TaskStatus.BLOCKED_AUTH,
        "export_file": export_file,
        "accepted_at": accepted_at,
        "picked_up_at": picked_up_at,
        "queue_age_seconds": queue_age_seconds,
        "worker_alive": worker_alive,
        "queue_size": queue_size,
        "auth_session_status": auth_session_status,
        "needs_login": needs_login,
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


def _close_active_session_runtime(session: dict[str, Any] | None) -> None:
    if not session:
        return
    for resource in (session.get("browser"), session.get("playwright")):
        if resource is None:
            continue
        try:
            close_fn = getattr(resource, "close", None) or getattr(resource, "stop", None)
            if callable(close_fn):
                close_fn()
        except Exception:
            pass


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


def _build_eager_auth_session_state(
    *,
    session_id: str,
    task_id: str | None,
    state_json: str,
    headless: bool,
    login_url: str = "https://www.weiq.com/",
) -> dict[str, Any]:
    playwright_ctx = sync_playwright().start()
    browser, context, page = init_browser(playwright_ctx, state_json, headless)
    try:
        page.goto(login_url, timeout=45000, wait_until="domcontentloaded")
    except Exception:
        pass

    is_authenticated, reason_code, pending_message = resolve_auth_completion(page, state_json)
    if is_authenticated:
        try:
            context.storage_state(path=state_json)
        except Exception:
            pass
        row = {
            "task_id": task_id,
            "status": "authenticated",
            "login_url": page.url or login_url,
            "qr_image_base64": None,
            "message": "已检测到可用的 WEIQ 登录态，任务可直接继续。",
            "expires_at": expiry_iso(30),
        }
        upsert_auth_session(session_id, row)
        _close_active_session_runtime({"browser": browser, "playwright": playwright_ctx})
        return fetch_auth_session(session_id) or row

    status_text, message = infer_auth_session_state(page, reason_code)
    row = {
        "task_id": task_id,
        "status": status_text,
        "login_url": page.url or login_url,
        "qr_image_base64": capture_login_screen_base64(page),
        "message": pending_message or message,
        "expires_at": expiry_iso(),
    }
    upsert_auth_session(session_id, row)
    register_active_session(
        session_id,
        task_id=task_id or "",
        page=page,
        context=context,
        state_json=state_json,
        login_url=page.url or login_url,
        browser=browser,
        playwright=playwright_ctx,
    )
    return fetch_auth_session(session_id) or row


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


def infer_runtime_auth_requirement(page) -> tuple[bool, str]:
    needs_auth, reason_code = detect_auth_or_challenge(page)
    if needs_auth:
        return True, reason_code

    page_text = ""
    try:
        page_text = page.locator("body").inner_text(timeout=1500) or ""
    except Exception:
        page_text = ""

    extracted_data = extract_metrics(page)
    post_issue = infer_post_extraction_issue(
        page_url=page.url,
        page_text=page_text,
        has_login_form=find_first_visible(iter_login_targets(page), USERNAME_SELECTORS + PASSWORD_SELECTORS + CODE_SELECTORS) is not None,
        extracted_data=extracted_data,
    )
    if post_issue in {ErrorCode.AUTH_REQUIRED, ErrorCode.CAPTCHA_REQUIRED}:
        return True, post_issue
    return False, ErrorCode.NONE


def resolve_auth_completion(page, state_json: str) -> tuple[bool, str, str | None]:
    needs_auth, reason_code = infer_runtime_auth_requirement(page)
    if needs_auth:
        return False, reason_code, None
    if not has_usable_storage_state(state_json):
        return False, ErrorCode.AUTH_REQUIRED, "尚未检测到有效的 WEIQ 登录态，请先完成登录后再继续抓取。"
    return True, ErrorCode.NONE, None


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
                raise HTTPException(status_code=422, detail="当前 WEIQ 登录页未切换到手机号验证码模式，请改用账号密码登录，或先切换到手机验证码登录后重试")
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

        is_authenticated, reason_code, pending_message = resolve_auth_completion(page, state_json)
        if is_authenticated:
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
            "message": pending_message or message,
            "expires_at": expiry_iso(),
        }
        upsert_auth_session(session_id, row)
        upsert_task_event(
            task_id,
            {
                "status": TaskStatus.BLOCKED_AUTH,
                "blocked_reason": reason_code,
                "message": pending_message or message,
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
    browser=None,
    playwright=None,
) -> None:
    with ACTIVE_AUTH_LOCK:
        previous = ACTIVE_AUTH_SESSIONS.get(session_id)
        ACTIVE_AUTH_SESSIONS[session_id] = {
            "task_id": task_id,
            "page": page,
            "context": context,
            "state_json": state_json,
            "login_url": login_url,
            "browser": browser,
            "playwright": playwright,
            "lock": threading.RLock(),
        }
        if task_id:
            TASK_TO_SESSION[task_id] = session_id
    if previous is not None and previous.get("page") is not page:
        _close_active_session_runtime(previous)


def unregister_active_session(session_id: str) -> None:
    with ACTIVE_AUTH_LOCK:
        session = ACTIVE_AUTH_SESSIONS.pop(session_id, None)
        if session:
            TASK_TO_SESSION.pop(str(session.get("task_id") or ""), None)
    _close_active_session_runtime(session)


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
        is_authenticated, reason_code, pending_message = resolve_auth_completion(page, state_json)
        if is_authenticated:
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
                    "message": pending_message or message,
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
            if has_usable_storage_state(state_json):
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

            upsert_task_event(
                task_id,
                {
                    "resume_requested": 0,
                    "status": TaskStatus.BLOCKED_AUTH,
                    "blocked_reason": ErrorCode.AUTH_REQUIRED,
                    "message": "尚未检测到有效的 WEIQ 登录态，请先完成登录后再继续。",
                },
            )
            upsert_auth_session(
                session_id,
                {
                    "status": "waiting_credentials",
                    "message": "尚未检测到有效的 WEIQ 登录态，请先完成登录后再继续。",
                    "expires_at": expiry_iso(),
                },
            )
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
    if row["status"] in {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}:
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
            "picked_up_at": now_iso(),
            "message": "WEIQ 执行器已接单，开始抓取",
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
                "message": (
                    "任务已完成"
                    if result.status == TaskStatus.SUCCESS
                    else "任务执行失败"
                    if result.status == TaskStatus.FAILED
                    else "任务已结束"
                ),
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
    global LAST_WORKER_ERROR
    while True:
        task_id = TASK_QUEUE.get()
        with QUEUE_LOCK:
            QUEUED_TASK_IDS.discard(task_id)
            ACTIVE_TASK_IDS.add(task_id)
        threading.Thread(
            target=_run_task_in_background,
            args=(task_id,),
            daemon=True,
            name=f"weiq-task-{task_id[:8]}",
        ).start()
        TASK_QUEUE.task_done()


def _run_task_in_background(task_id: str) -> None:
    global LAST_WORKER_ERROR
    try:
        run_task(task_id)
        LAST_WORKER_ERROR = None
    except Exception as exc:
        LAST_WORKER_ERROR = f"{now_iso()} {exc}"
        upsert_task_event(
            task_id,
            {
                "status": TaskStatus.FAILED,
                "finished_at": now_iso(),
                "error_code": "INTERNAL_ERROR",
                "message": f"Worker 异常退出: {exc}",
            },
        )
    finally:
        with QUEUE_LOCK:
            ACTIVE_TASK_IDS.discard(task_id)


def start_worker() -> None:
    global WORKER_THREAD, WORKER_STARTED_AT
    if WORKER_THREAD is not None and WORKER_THREAD.is_alive():
        return
    thread = threading.Thread(target=worker_loop, daemon=True, name="weiq-task-worker")
    thread.start()
    WORKER_THREAD = thread
    WORKER_STARTED_AT = now_iso()


def recover_incomplete_tasks() -> None:
    with DB_LOCK:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT task_id, status FROM tasks WHERE status IN (?, ?)",
                (TaskStatus.PENDING, TaskStatus.RUNNING),
            ).fetchall()
        finally:
            conn.close()

    for row in rows:
        task_id = str(row["task_id"])
        status = str(row["status"])
        if status == TaskStatus.RUNNING:
            upsert_task_event(
                task_id,
                {
                    "status": TaskStatus.PENDING,
                    "message": "服务重启后重新入队",
                    "progress": 0.0,
                },
            )
        else:
            upsert_task_event(task_id, {"message": "服务重启后重新入队"})
        enqueue_task(task_id)


@app.on_event("startup")
def on_startup() -> None:
    ensure_single_worker_mode()
    init_db()
    start_worker()
    recover_incomplete_tasks()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "time": now_iso()}


@app.get("/v1/worker/health", response_model=WorkerHealthResponse)
def worker_health() -> WorkerHealthResponse:
    pending_count = fetch_value("SELECT COUNT(1) FROM tasks WHERE status = ?", (TaskStatus.PENDING,))
    running_count = fetch_value("SELECT COUNT(1) FROM tasks WHERE status = ?", (TaskStatus.RUNNING,))
    with QUEUE_LOCK:
        queued_ids = sorted(QUEUED_TASK_IDS)
    return WorkerHealthResponse(
        worker_alive=bool(WORKER_THREAD and WORKER_THREAD.is_alive()),
        worker_started_at=WORKER_STARTED_AT,
        queue_size=TASK_QUEUE.qsize(),
        queued_task_ids=queued_ids,
        pending_count=pending_count,
        running_count=running_count,
        last_worker_error=LAST_WORKER_ERROR,
        process_id=os.getpid(),
        db_path=str(DB_PATH),
    )


@app.post("/v1/tasks/crawl", response_model=TaskControlResponse)
def create_task(payload: CreateTaskRequest) -> TaskControlResponse:
    task_id = uuid4().hex
    created_at = now_iso()
    materialized = materialize_task_request(payload)

    execute(
        """
        INSERT INTO tasks (
            task_id, status, progress, current_account, blocked_reason, error_code, message,
            input_excel, output_excel, output_dir, state_json, state_storage,
            headless, cooldown_every, cooldown_seconds, retry_times, retry_backoff_seconds, resume,
            login_session_id, accepted_at, picked_up_at, created_at
        ) VALUES (?, ?, 0, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?)
        """,
        (
            task_id,
            TaskStatus.PENDING,
            ErrorCode.NONE,
            "任务已受理，等待执行器接单",
            materialized["input_excel"],
            materialized["output_excel"],
            materialized["output_dir"],
            materialized["state_json"],
            materialized["state_storage"],
            int(bool(materialized["headless"])),
            materialized["cooldown_every"],
            materialized["cooldown_seconds"],
            materialized["retry_times"],
            materialized["retry_backoff_seconds"],
            int(bool(materialized["resume"])),
            created_at,
            created_at,
        ),
    )

    enqueue_task(task_id)
    return TaskControlResponse(task_id=task_id, status=TaskStatus.PENDING, message="任务已受理")


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


@app.post("/v1/tasks/{task_id}/requeue", response_model=TaskControlResponse)
def requeue_task(task_id: str) -> TaskControlResponse:
    row = fetch_one("SELECT status FROM tasks WHERE task_id = ?", (task_id,))
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["status"] == TaskStatus.SUCCESS:
        raise HTTPException(status_code=409, detail="成功任务不允许重新入队")

    upsert_task_event(
        task_id,
        {
            "status": TaskStatus.PENDING,
            "progress": 0.0,
            "cancel_requested": 0,
            "resume_requested": 0,
            "blocked_reason": None,
            "current_account": None,
            "started_at": None,
            "finished_at": None,
            "message": "任务已重新入队",
        },
    )
    enqueue_task(task_id)
    return TaskControlResponse(task_id=task_id, status=TaskStatus.PENDING, message="任务已重新入队")


@app.post("/v1/auth/session", response_model=AuthSessionResponse)
def create_auth_session(payload: AuthSessionCreateRequest | None = None) -> AuthSessionResponse:
    payload = payload or AuthSessionCreateRequest()
    session_id = uuid4().hex
    state_json = str((get_runtime_dir() / "state.json").resolve())
    headless = True
    task_id = payload.task_id
    if task_id:
        task = fetch_one("SELECT task_id, state_json, headless FROM tasks WHERE task_id = ?", (task_id,))
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        state_json = str(task.get("state_json") or state_json)
        headless = bool(task.get("headless"))
    upsert_auth_session(
        session_id,
        {
            "task_id": task_id,
            "status": "pending",
            "login_url": "https://www.weiq.com/",
            "qr_image_base64": None,
            "message": "登录会话已创建，等待绑定抓取任务" if not payload.eager else "登录会话已创建，正在准备 WEIQ 登录页",
            "expires_at": expiry_iso(),
        },
    )
    if task_id:
        upsert_task_event(task_id, {"login_session_id": session_id})
    if payload.eager:
        row = _build_eager_auth_session_state(
            session_id=session_id,
            task_id=task_id,
            state_json=state_json,
            headless=headless,
        )
        return build_auth_session_response(row)
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
    task = fetch_one("SELECT task_id, login_session_id, status, state_json, headless FROM tasks WHERE task_id = ?", (payload.task_id,))
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
    if session_id not in ACTIVE_AUTH_SESSIONS:
        session_row = fetch_auth_session(session_id)
        if session_row and str(session_row.get("status") or "") in {"pending", "waiting_credentials", "waiting_code", "logging_in"}:
            _build_eager_auth_session_state(
                session_id=session_id,
                task_id=payload.task_id,
                state_json=str(task.get("state_json") or (get_runtime_dir() / "state.json")),
                headless=bool(task.get("headless")),
            )
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


@app.get("/v1/tasks/{task_id}/export")
def export_task_result(task_id: str):
    row = fetch_one("SELECT status, output_excel, output_dir FROM tasks WHERE task_id = ?", (task_id,))
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["status"] != TaskStatus.SUCCESS:
        raise HTTPException(status_code=409, detail="任务尚未成功完成，暂不能导出结果")

    output_excel = str(row.get("output_excel") or "").strip()
    output_dir = str(row.get("output_dir") or "").strip()
    if not output_excel:
        raise HTTPException(status_code=404, detail="结果文件不存在")
    output_path = Path(output_excel)
    if not output_path.is_absolute():
        output_path = Path(output_dir or ".") / output_path
    output_path = output_path.resolve()
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="结果文件不存在")

    return FileResponse(
        path=str(output_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=output_path.name,
    )
