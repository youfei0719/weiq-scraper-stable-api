import asyncio
import base64
import json
import os
import queue
import re
import shlex
import signal
import shutil
import sqlite3
import socket
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
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
    extract_post_trend_from_echarts_options,
    extract_post_trend_from_payloads,
    extract_metrics,
    has_usable_storage_state,
    init_browser,
    infer_post_extraction_issue,
    run_crawl,
)
from playwright.async_api import async_playwright
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
AUTH_WAITING_STATUSES = {"pending", "waiting_credentials", "waiting_code", "logging_in"}
BROWSER_WORKER_LOCK = threading.RLock()
BROWSER_WORKER_RUNTIME: dict[str, Any] = {}
BROWSER_DISPLAY_UNAVAILABLE_MESSAGE = (
    "Browser Worker display is unavailable. Please open browser session first or restart stable-api."
)

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

@asynccontextmanager
async def app_lifespan(_: FastAPI):
    ensure_single_worker_mode()
    init_db()
    start_worker()
    recover_incomplete_tasks()
    try:
        yield
    finally:
        await BROWSER_WORKER_CONTROLLER.close()


app = FastAPI(title="WEIQ Scraper API", version="0.2.0", lifespan=app_lifespan)


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


class CreateContentTrendRequest(BaseModel):
    uid: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=20, ge=1, le=50)


class TaskControlResponse(BaseModel):
    task_id: str | None = None
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


class BrowserAuthStatusResponse(BaseModel):
    auth_mode: str
    authenticated: bool
    state_json_exists: bool
    worker_available: bool
    login_status: str
    browser_session_running: bool = False
    novnc_running: bool = False
    browser_display: str | None = None
    display_ready: bool = False
    xvfb_running: bool = False
    x11vnc_running: bool = False
    websockify_running: bool = False
    crawl_will_use_display: bool = False
    session_id: str | None = None
    novnc_local_url: str | None = None
    novnc_proxy_path: str | None = None
    expires_at: str | None = None
    blocked: bool = False
    page_title: str | None = None
    saved_auth_state_present: bool = False
    live_auth_verified: bool = False
    runtime_state: str = "closed"
    message: str | None = None


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


def get_auth_mode() -> str:
    raw = os.getenv("WEIQ_BROWSER_AUTH_MODE", "").strip() or os.getenv("WEIQ_AUTH_MODE", "per_task").strip()
    return raw.lower() or "per_task"


def get_legacy_state_json_path() -> Path:
    raw = os.getenv("WEIQ_LEGACY_STATE_JSON", "").strip()
    path = Path(raw or (get_runtime_dir() / "state.json")).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_browser_user_data_dir() -> Path:
    raw = os.getenv("WEIQ_BROWSER_USER_DATA_DIR", "").strip()
    path = Path(raw or (get_runtime_dir() / "browser_profile")).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_browser_headless() -> bool:
    raw = os.getenv("WEIQ_BROWSER_HEADLESS")
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def get_browser_display() -> str:
    return os.getenv("WEIQ_BROWSER_DISPLAY", ":99").strip() or ":99"


def get_browser_screen_geometry() -> str:
    return os.getenv("WEIQ_BROWSER_SCREEN", "1600x900x24").strip() or "1600x900x24"


def get_browser_session_ttl_seconds() -> int:
    raw = os.getenv("WEIQ_BROWSER_SESSION_TTL_SECONDS", "").strip()
    if raw.isdigit():
        return max(int(raw), 120)
    return 600


def get_browser_vnc_port() -> int:
    raw = os.getenv("WEIQ_BROWSER_VNC_PORT", "5901").strip()
    if raw.isdigit():
        return max(int(raw), 1024)
    return 5901


def get_browser_novnc_port() -> int:
    raw = os.getenv("WEIQ_BROWSER_NOVNC_PORT", "6080").strip()
    if raw.isdigit():
        return max(int(raw), 1024)
    return 6080


def get_browser_proxy_path() -> str:
    raw = os.getenv("WEIQ_BROWSER_PROXY_PATH", "/browser-session/").strip() or "/browser-session/"
    if not raw.startswith("/"):
        raw = f"/{raw}"
    if not raw.endswith("/"):
        raw = f"{raw}/"
    return raw


def get_novnc_web_dir() -> Path:
    raw = os.getenv("WEIQ_NOVNC_WEB_DIR", "/opt/noVNC").strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def get_auth_session_ttl_seconds() -> int:
    raw = os.getenv("WEIQ_AUTH_SESSION_TTL_SECONDS", "").strip()
    if raw.isdigit():
        return max(int(raw), 60)
    return 600


def keep_auth_state_for_debug() -> bool:
    return os.getenv("WEIQ_KEEP_AUTH_STATE_FOR_DEBUG", "false").strip().lower() in {"1", "true", "yes", "on"}


def get_auth_state_dir() -> Path:
    raw = os.getenv("WEIQ_AUTH_STATE_DIR", "").strip()
    base_dir = Path(raw or (get_runtime_dir() / "auth_sessions")).expanduser()
    if not base_dir.is_absolute():
        base_dir = (Path.cwd() / base_dir).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


def get_task_runtime_dir(task_id: str) -> Path:
    target = get_runtime_dir() / "tasks" / task_id
    target.mkdir(parents=True, exist_ok=True)
    return target


def get_auth_session_dir(session_id: str) -> Path:
    target = get_auth_state_dir() / session_id
    target.mkdir(parents=True, exist_ok=True)
    return target


def build_auth_session_paths(session_id: str) -> dict[str, str]:
    session_dir = get_auth_session_dir(session_id)
    return {
        "session_dir": str(session_dir.resolve()),
        "state_storage": str((session_dir / "storage_state.json").resolve()),
        "preview_image_path": str((session_dir / "preview.png").resolve()),
        "meta_path": str((session_dir / "meta.json").resolve()),
    }


def build_task_paths(task_id: str) -> dict[str, str]:
    task_dir = get_runtime_dir() / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    return {
        "task_dir": str(task_dir.resolve()),
        "blocked_screenshot_path": str((task_dir / "blocked_auth.png").resolve()),
    }


def ensure_single_worker_mode() -> None:
    if get_auth_mode() not in {"per_task", "browser_worker"}:
        raise RuntimeError("当前 WEIQ API 仅支持 per_task 或 browser_worker 两种认证模式")
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
                    current_url TEXT,
                    page_title TEXT,
                    screenshot_path TEXT,
                    blocked_reason TEXT,
                    resolution TEXT,
                    can_resume INTEGER NOT NULL DEFAULT 0,
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
                    task_type TEXT NOT NULL DEFAULT 'metrics',
                    target_uid TEXT,
                    content_limit INTEGER,
                    content_json TEXT,
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
                    state_storage TEXT,
                    preview_image_path TEXT,
                    message TEXT,
                    expires_at TEXT,
                    consumed_at TEXT,
                    expired_at TEXT,
                    cleanup_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            task_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            auth_cols = {row["name"] for row in conn.execute("PRAGMA table_info(auth_sessions)").fetchall()}
            task_column_defs = {
                "current_url": "TEXT",
                "page_title": "TEXT",
                "screenshot_path": "TEXT",
                "blocked_reason": "TEXT",
                "resolution": "TEXT",
                "can_resume": "INTEGER NOT NULL DEFAULT 0",
                "state_json": "TEXT NOT NULL DEFAULT ''",
                "state_storage": "TEXT NOT NULL DEFAULT ''",
                "headless": "INTEGER NOT NULL DEFAULT 0",
                "cooldown_every": "INTEGER NOT NULL DEFAULT 50",
                "cooldown_seconds": "INTEGER NOT NULL DEFAULT 180",
                "retry_times": "INTEGER NOT NULL DEFAULT 1",
                "retry_backoff_seconds": "INTEGER NOT NULL DEFAULT 3",
                "resume": "INTEGER NOT NULL DEFAULT 1",
                "run_id": "TEXT",
                "total_accounts": "INTEGER NOT NULL DEFAULT 0",
                "processed_accounts": "INTEGER NOT NULL DEFAULT 0",
                "success_accounts": "INTEGER NOT NULL DEFAULT 0",
                "failed_accounts": "INTEGER NOT NULL DEFAULT 0",
                "skipped_accounts": "INTEGER NOT NULL DEFAULT 0",
                "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
                "resume_requested": "INTEGER NOT NULL DEFAULT 0",
                "login_session_id": "TEXT",
                "accepted_at": "TEXT",
                "picked_up_at": "TEXT",
                "task_type": "TEXT NOT NULL DEFAULT 'metrics'",
                "target_uid": "TEXT",
                "content_limit": "INTEGER",
                "content_json": "TEXT",
            }
            for column_name, column_def in task_column_defs.items():
                if column_name not in task_cols:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {column_name} {column_def}")

            auth_column_defs = {
                "qr_image_base64": "TEXT",
                "state_storage": "TEXT",
                "preview_image_path": "TEXT",
                "consumed_at": "TEXT",
                "expired_at": "TEXT",
                "cleanup_at": "TEXT",
            }
            for column_name, column_def in auth_column_defs.items():
                if column_name not in auth_cols:
                    conn.execute(f"ALTER TABLE auth_sessions ADD COLUMN {column_name} {column_def}")
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
                session_id, task_id, status, login_url, qr_image_base64, state_storage, preview_image_path,
                message, expires_at, consumed_at, expired_at, cleanup_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                updates.get("task_id"),
                updates.get("status", "pending"),
                updates.get("login_url"),
                updates.get("qr_image_base64"),
                updates.get("state_storage"),
                updates.get("preview_image_path"),
                updates.get("message"),
                updates.get("expires_at"),
                updates.get("consumed_at"),
                updates.get("expired_at"),
                updates.get("cleanup_at"),
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


def is_auth_session_expired(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    expires_at = parse_iso(str(row.get("expires_at") or ""))
    return expires_at is not None and expires_at <= datetime.now()


def write_auth_session_meta(session_id: str, payload: dict[str, Any]) -> None:
    meta_path = Path(build_auth_session_paths(session_id)["meta_path"])
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def capture_login_preview(session_id: str, page) -> tuple[str | None, str | None]:
    try:
        image_bytes = page.screenshot(type="png", full_page=False)
    except Exception:
        return None, None
    paths = build_auth_session_paths(session_id)
    preview_path = Path(paths["preview_image_path"])
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_bytes(image_bytes)
    return str(preview_path.resolve()), base64.b64encode(image_bytes).decode("utf-8")


def capture_task_blocked_screenshot(task_id: str, page) -> str | None:
    try:
        image_bytes = page.screenshot(type="png", full_page=False)
    except Exception:
        return None
    screenshot_path = Path(build_task_paths(task_id)["blocked_screenshot_path"])
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path.write_bytes(image_bytes)
    return str(screenshot_path.resolve())


def _blocked_auth_message(reason_code: str | None = None) -> str:
    if reason_code == ErrorCode.CAPTCHA_REQUIRED:
        return "WEIQ 要求安全验证，请在远端浏览器中完成验证后继续。"
    return "WEIQ 要求安全验证，请在远端浏览器中完成验证后继续。"


def _capture_blocked_auth_context(task_id: str, page) -> dict[str, Any]:
    current_url = None
    page_title = None
    if page is not None:
        try:
            current_url = str(page.url or "").strip() or None
        except Exception:
            current_url = None
        try:
            page_title = str(page.title() or "").strip() or None
        except Exception:
            page_title = None
    return {
        "current_url": current_url,
        "page_title": page_title,
        "screenshot_path": capture_task_blocked_screenshot(task_id, page) if page is not None else None,
    }


def _clear_blocked_auth_context(task_id: str) -> None:
    screenshot_path = Path(build_task_paths(task_id)["blocked_screenshot_path"])
    if screenshot_path.exists():
        try:
            screenshot_path.unlink()
        except Exception:
            pass


def _set_task_blocked_auth(
    task_id: str,
    *,
    reason_code: str,
    page=None,
    context=None,
    message: str | None = None,
) -> dict[str, Any]:
    if context is not None:
        try:
            context.storage_state(path=str(get_legacy_state_json_path()))
        except Exception:
            pass
    snapshot = _capture_blocked_auth_context(task_id, page)
    updates = {
        "status": TaskStatus.BLOCKED_AUTH,
        "blocked_reason": reason_code,
        "error_code": "BLOCKED_AUTH",
        "message": message or _blocked_auth_message(reason_code),
        "current_url": snapshot["current_url"],
        "page_title": snapshot["page_title"],
        "screenshot_path": snapshot["screenshot_path"],
        "resolution": "open_browser_session" if get_auth_mode() == "browser_worker" else "submit_login_session",
        "can_resume": 1,
        "finished_at": None,
    }
    upsert_task_event(task_id, updates)
    row = fetch_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    return build_status_payload(row or {"task_id": task_id, **updates})


def cleanup_auth_session_directory(session_id: str) -> None:
    if keep_auth_state_for_debug():
        return
    session_dir = get_auth_state_dir() / session_id
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)


def expire_auth_session(session_id: str, *, message: str | None = None) -> dict[str, Any]:
    row = fetch_auth_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="登录会话不存在")
    unregister_active_session(session_id)
    now = now_iso()
    updates = {
        "status": "expired",
        "message": message or "本次临时登录会话已过期，请重新创建。",
        "expired_at": now,
        "expires_at": now,
    }
    if not keep_auth_state_for_debug():
        cleanup_auth_session_directory(session_id)
        updates["cleanup_at"] = now
    upsert_auth_session(session_id, updates)
    refreshed = fetch_auth_session(session_id)
    assert refreshed is not None
    return refreshed


def mark_auth_session_cleaned(session_id: str, *, status: str, message: str) -> None:
    now = now_iso()
    updates: dict[str, Any] = {
        "status": status,
        "message": message,
        "consumed_at": now if status == "consumed" else None,
        "expired_at": now if status == "expired" else None,
    }
    unregister_active_session(session_id)
    if not keep_auth_state_for_debug():
        cleanup_auth_session_directory(session_id)
        updates["cleanup_at"] = now
    upsert_auth_session(session_id, updates)


def finalize_task_auth_session(task_id: str, task_status: str) -> None:
    task = fetch_one("SELECT login_session_id FROM tasks WHERE task_id = ?", (task_id,))
    session_id = str(task.get("login_session_id") or "").strip() if task else ""
    if not session_id:
        return
    row = fetch_auth_session(session_id)
    if row is None:
        return
    if task_status == TaskStatus.CANCELLED:
        mark_auth_session_cleaned(session_id, status="expired", message="本次临时登录态已清理")
        return
    status = "consumed" if str(row.get("status") or "") == "authenticated" else "expired"
    mark_auth_session_cleaned(session_id, status=status, message="本次临时登录态已清理")


def materialize_task_request(payload: CreateTaskRequest) -> dict[str, Any]:
    runtime_dir = get_runtime_dir()
    task_key = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    task_runtime_dir = get_task_runtime_dir(task_key)
    progress_state = str((task_runtime_dir / "crawl_progress.json").resolve())
    placeholder_auth_state = str((task_runtime_dir / "task_auth_placeholder.json").resolve())

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
            "state_json": progress_state,
            "state_storage": placeholder_auth_state,
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
    state_json = str(payload.state_json or progress_state).strip()
    state_storage = str(payload.state_storage or placeholder_auth_state).strip()
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
        or auth_session_status in AUTH_WAITING_STATUSES
    )
    resolution = str(row.get("resolution") or "").strip() or None
    if status == TaskStatus.BLOCKED_AUTH and not resolution:
        resolution = "open_browser_session" if get_auth_mode() == "browser_worker" else "submit_login_session"
    return {
        **row,
        "status_zh": STATUS_ZH.get(status, status),
        "error_message_zh": error_message_zh,
        "auth_waiting": needs_login,
        "export_file": export_file,
        "accepted_at": accepted_at,
        "picked_up_at": picked_up_at,
        "queue_age_seconds": queue_age_seconds,
        "worker_alive": worker_alive,
        "queue_size": queue_size,
        "auth_session_status": auth_session_status,
        "needs_login": needs_login,
        "current_url": str(row.get("current_url") or "").strip() or None,
        "page_title": str(row.get("page_title") or "").strip() or None,
        "screenshot_path": str(row.get("screenshot_path") or "").strip() or None,
        "resolution": resolution,
        "can_resume": bool(row.get("can_resume", 0)),
    }


def _capture_json_responses(page, payloads: list[Any]) -> Any:
    def on_response(response) -> None:
        try:
            if "json" not in str(response.headers.get("content-type") or "").lower():
                return
            payload = response.json()
            if isinstance(payload, (dict, list)):
                payloads.append(payload)
        except Exception:
            return

    page.on("response", on_response)
    return on_response


def _extract_echarts_post_payloads(page) -> list[Any]:
    script = """
    () => {
      const output = [];
      const api = window.echarts;
      if (!api || typeof api.getInstanceByDom !== 'function') return output;
      for (const node of Array.from(document.querySelectorAll('div, canvas'))) {
        const chart = api.getInstanceByDom(node);
        if (!chart) continue;
        const option = chart.getOption();
        if (JSON.stringify(option).includes('阅读') || JSON.stringify(option).includes('read')) output.push(option);
      }
      return output;
    }
    """
    try:
        payloads = page.evaluate(script)
        return payloads if isinstance(payloads, list) else []
    except Exception:
        return []


def run_content_trend_task(task_id: str, row: dict[str, Any], *, state_storage: str, display: str | None) -> None:
    uid = str(row.get("target_uid") or "").strip()
    if not uid:
        raise RuntimeError("内容趋势任务缺少 uid")
    playwright_ctx = browser = context = page = handler = None
    try:
        playwright_ctx = sync_playwright().start()
        browser, context, page = init_browser(playwright_ctx, state_storage, bool(row.get("headless")), display=display)
        payloads: list[Any] = []
        handler = _capture_json_responses(page, payloads)
        upsert_task_event(task_id, {"current_account": f"UID: {uid}", "message": "正在读取最近微博内容趋势"})
        response = page.goto(f"https://weiq.com/client/product/weibo/detail?account_uid={uid}", timeout=45000, wait_until="domcontentloaded")
        if response is None or response.status >= 400:
            raise RuntimeError(f"WEIQ 页面访问异常: {response.status if response else 'null'}")
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        time.sleep(2)
        needs_auth, reason_code = detect_auth_or_challenge(page)
        if needs_auth:
            _set_task_blocked_auth(task_id, reason_code=reason_code, page=page, context=context)
            return
        limit = max(1, min(int(row.get("content_limit") or 20), 50))
        posts = extract_post_trend_from_payloads(payloads, limit=limit)
        if not posts:
            posts = extract_post_trend_from_echarts_options(_extract_echarts_post_payloads(page), limit=limit)
        if not posts:
            raise RuntimeError("未识别到最近微博趋势数据，可能是 WEIQ 页面结构已变化")
        upsert_task_event(task_id, {"status": TaskStatus.SUCCESS, "progress": 1.0, "processed_accounts": 1, "total_accounts": 1, "success_accounts": 1, "content_json": json.dumps(posts, ensure_ascii=False), "finished_at": now_iso(), "message": f"已采集 {len(posts)} 篇微博内容趋势", "error_code": ErrorCode.NONE})
    except Exception as exc:
        upsert_task_event(task_id, {"status": TaskStatus.FAILED, "finished_at": now_iso(), "error_code": "CONTENT_TREND_EXTRACTION_FAILED", "message": str(exc)})
    finally:
        if page is not None and handler is not None:
            try:
                page.remove_listener("response", handler)
            except Exception:
                pass
        for resource, method in ((context, "close"), (browser, "close"), (playwright_ctx, "stop")):
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception:
                    pass


def _is_process_running(proc: object | None) -> bool:
    if proc is None:
        return False
    poll = getattr(proc, "poll", None)
    if callable(poll):
        try:
            return poll() is None
        except Exception:
            return False
    pid = _normalize_pid(proc)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _normalize_pid(value: object) -> int | None:
    if isinstance(value, subprocess.Popen):
        return value.pid
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value or "").strip()
    if text.isdigit():
        pid = int(text)
        return pid if pid > 0 else None
    return None


def _display_socket_path(display: str | None) -> Path | None:
    text = str(display or "").strip()
    if not text.startswith(":"):
        return None
    display_number = text[1:].split(".", 1)[0].strip()
    if not display_number.isdigit():
        return None
    return Path("/tmp/.X11-unix") / f"X{display_number}"


def _is_display_ready(display: str | None) -> bool:
    socket_path = _display_socket_path(display)
    return socket_path is not None and socket_path.exists()


def _read_process_table() -> list[tuple[int, str]]:
    try:
        completed = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return []

    rows: list[tuple[int, str]] = []
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if not parts or not parts[0].isdigit():
            continue
        rows.append((int(parts[0]), parts[1] if len(parts) > 1 else ""))
    return rows


def _find_process_pid(*needles: str) -> int | None:
    required = [needle for needle in needles if needle]
    if not required:
        return None
    executable = required[0]
    for pid, args in _read_process_table():
        try:
            argv = shlex.split(args)
        except ValueError:
            argv = args.split()
        if not any(Path(token).name == executable for token in argv[:2]):
            continue
        if all(needle in args for needle in required[1:]):
            return pid
    return None


def _discover_existing_browser_worker_runtime(existing: dict[str, Any] | None = None) -> dict[str, Any] | None:
    seed = dict(existing or {})
    display = str(seed.get("display") or get_browser_display()).strip() or get_browser_display()
    vnc_port = int(seed.get("vnc_port") or get_browser_vnc_port())
    novnc_port = int(seed.get("novnc_port") or get_browser_novnc_port())
    xvfb_proc = seed.get("xvfb_proc")
    x11vnc_proc = seed.get("x11vnc_proc")
    websockify_proc = seed.get("websockify_proc")

    if not _is_process_running(xvfb_proc):
        xvfb_proc = _find_process_pid("Xvfb", display)
    if not _is_process_running(x11vnc_proc):
        x11vnc_proc = _find_process_pid("x11vnc", display, str(vnc_port))
    if not _is_process_running(websockify_proc):
        websockify_proc = _find_process_pid("websockify", f"127.0.0.1:{novnc_port}", f"127.0.0.1:{vnc_port}")

    if not any((xvfb_proc, x11vnc_proc, websockify_proc)):
        return None

    runtime_env = dict(seed.get("runtime_env") or os.environ.copy())
    runtime_env["DISPLAY"] = display
    runtime = {
        "session_id": seed.get("session_id"),
        "playwright": seed.get("playwright"),
        "context": seed.get("context"),
        "page": seed.get("page"),
        "opened_at": seed.get("opened_at") or now_iso(),
        "expires_at": seed.get("expires_at"),
        "display": display,
        "runtime_env": runtime_env,
        "vnc_port": vnc_port,
        "novnc_port": novnc_port,
        "novnc_local_url": seed.get("novnc_local_url") or f"http://127.0.0.1:{novnc_port}/vnc.html?path=websockify",
        "xvfb_proc": xvfb_proc,
        "x11vnc_proc": x11vnc_proc,
        "websockify_proc": websockify_proc,
    }
    return runtime


def _wait_for_local_port(port: int, *, timeout_seconds: float = 8.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"本地端口 127.0.0.1:{port} 未能按时启动：{last_error}")


def _terminate_process(proc: object | None) -> None:
    if proc is None:
        return
    terminate = getattr(proc, "terminate", None)
    wait = getattr(proc, "wait", None)
    kill = getattr(proc, "kill", None)
    poll = getattr(proc, "poll", None)
    if callable(terminate) and callable(wait) and callable(kill) and callable(poll):
        try:
            if poll() is None:
                terminate()
                wait(timeout=5)
        except Exception:
            try:
                kill()
                wait(timeout=3)
            except Exception:
                pass
        return
    if not isinstance(proc, subprocess.Popen):
        pid = _normalize_pid(proc)
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        deadline = time.time() + 5
        while time.time() < deadline:
            if not _is_process_running(pid):
                return
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass


def _launch_local_process(cmd: list[str], *, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        env=env,
        text=True,
    )


def _ensure_browser_worker_support_files() -> None:
    for binary in ("Xvfb", "x11vnc", "websockify"):
        if shutil.which(binary) is None:
            raise RuntimeError(f"缺少依赖：{binary}")
    novnc_dir = get_novnc_web_dir()
    if not (novnc_dir / "vnc.html").exists():
        raise RuntimeError(f"noVNC 静态目录不可用：{novnc_dir}")


def _close_browser_worker_display_runtime() -> None:
    with BROWSER_WORKER_LOCK:
        runtime = BROWSER_WORKER_RUNTIME.pop("session", None)
    if not runtime:
        return
    for key in ("websockify_proc", "x11vnc_proc", "xvfb_proc"):
        _terminate_process(runtime.get(key))


def _browser_worker_runtime_snapshot() -> dict[str, Any] | None:
    with BROWSER_WORKER_LOCK:
        runtime = BROWSER_WORKER_RUNTIME.get("session")
        snapshot = dict(runtime) if runtime is not None else None

    discovered = _discover_existing_browser_worker_runtime(snapshot)
    if discovered is None:
        return snapshot

    with BROWSER_WORKER_LOCK:
        current = BROWSER_WORKER_RUNTIME.get("session")
        if current is None:
            BROWSER_WORKER_RUNTIME["session"] = discovered
            current = BROWSER_WORKER_RUNTIME["session"]
        else:
            current.update(
                {
                    "display": discovered.get("display"),
                    "runtime_env": discovered.get("runtime_env"),
                    "vnc_port": discovered.get("vnc_port"),
                    "novnc_port": discovered.get("novnc_port"),
                    "novnc_local_url": discovered.get("novnc_local_url"),
                    "xvfb_proc": discovered.get("xvfb_proc"),
                    "x11vnc_proc": discovered.get("x11vnc_proc"),
                    "websockify_proc": discovered.get("websockify_proc"),
                }
            )
        return dict(current)


def _browser_worker_processes_running(runtime: dict[str, Any] | None) -> bool:
    if not runtime:
        return False
    return all(_is_process_running(runtime.get(key)) for key in ("xvfb_proc", "x11vnc_proc", "websockify_proc"))


def _browser_worker_browser_running(runtime: dict[str, Any] | None) -> bool:
    controller = globals().get("BROWSER_WORKER_CONTROLLER")
    snapshot = controller.public_snapshot() if controller is not None else {}
    return bool(snapshot.get("browser_session_running")) and bool(
        runtime and _is_process_running(runtime.get("xvfb_proc"))
    )


def _browser_worker_status_payload(*, message: str | None = None) -> dict[str, Any]:
    state_path = get_legacy_state_json_path()
    saved_auth_state_present = has_usable_storage_state(str(state_path))
    runtime = _browser_worker_runtime_snapshot()
    controller = globals().get("BROWSER_WORKER_CONTROLLER")
    controller_state = controller.public_snapshot() if controller is not None else {}
    worker_open = _browser_worker_browser_running(runtime)
    configured_display = get_browser_display() if get_auth_mode() == "browser_worker" else None
    browser_display = str(runtime.get("display") or configured_display or "").strip() if runtime else configured_display
    xvfb_running = _is_process_running(runtime.get("xvfb_proc")) if runtime else False
    x11vnc_running = _is_process_running(runtime.get("x11vnc_proc")) if runtime else False
    websockify_running = _is_process_running(runtime.get("websockify_proc")) if runtime else False
    display_ready = xvfb_running and _is_display_ready(browser_display)
    novnc_running = x11vnc_running and websockify_running
    crawl_will_use_display = get_auth_mode() == "browser_worker" and not get_browser_headless()
    live_auth_verified = bool(controller_state.get("live_auth_verified")) and worker_open
    login_status = (
        "authenticated"
        if live_auth_verified
        else "waiting_login"
        if worker_open
        else "saved_state_unverified"
        if saved_auth_state_present
        else "auth_required"
    )
    effective_message = message
    if not effective_message:
        if live_auth_verified:
            effective_message = "已实时验证 WEIQ 登录，Browser Worker 可用"
        elif worker_open:
            effective_message = "远端浏览器已打开，请完成 WEIQ 登录或安全验证后再检查"
        elif saved_auth_state_present:
            effective_message = "发现可复用登录态，但尚未通过远端浏览器实时验证"
        else:
            effective_message = "请在 Browser Worker 登录窗口中完成 WEIQ 登录"
    return {
        "auth_mode": get_auth_mode(),
        "authenticated": saved_auth_state_present,
        "state_json_exists": state_path.exists(),
        "saved_auth_state_present": saved_auth_state_present,
        "live_auth_verified": live_auth_verified,
        "runtime_state": str(controller_state.get("runtime_state") or ("open" if worker_open else "closed")),
        "worker_available": True,
        "login_status": login_status,
        "browser_session_running": worker_open,
        "novnc_running": novnc_running,
        "browser_display": browser_display or None,
        "display_ready": display_ready,
        "xvfb_running": xvfb_running,
        "x11vnc_running": x11vnc_running,
        "websockify_running": websockify_running,
        "crawl_will_use_display": crawl_will_use_display,
        "session_id": str(controller_state.get("session_id") or "") if worker_open else None,
        "novnc_local_url": str(runtime.get("novnc_local_url") or "") if runtime else None,
        "novnc_proxy_path": get_browser_proxy_path(),
        "expires_at": str(controller_state.get("expires_at") or "") or None,
        "blocked": bool(controller_state.get("blocked")),
        "page_title": controller_state.get("page_title"),
        "message": effective_message,
    }


def _ensure_browser_worker_display_runtime() -> dict[str, Any]:
    runtime = _browser_worker_runtime_snapshot() or {}
    _ensure_browser_worker_support_files()
    display = str(runtime.get("display") or get_browser_display()).strip() or get_browser_display()
    screen = get_browser_screen_geometry()
    vnc_port = int(runtime.get("vnc_port") or get_browser_vnc_port())
    novnc_port = int(runtime.get("novnc_port") or get_browser_novnc_port())
    runtime_env = dict(runtime.get("runtime_env") or os.environ.copy())
    runtime_env["DISPLAY"] = display

    started_handles: list[object] = []
    try:
        xvfb_proc = runtime.get("xvfb_proc")
        if not _is_process_running(xvfb_proc):
            xvfb_proc = _launch_local_process(["Xvfb", display, "-screen", "0", screen, "-nolisten", "tcp"])
            started_handles.append(xvfb_proc)
            time.sleep(1.0)
            if not _is_process_running(xvfb_proc):
                raise RuntimeError("Xvfb 启动失败")
        runtime["xvfb_proc"] = xvfb_proc

        x11vnc_proc = runtime.get("x11vnc_proc")
        if not _is_process_running(x11vnc_proc):
            x11vnc_proc = _launch_local_process(
                [
                    "x11vnc",
                    "-display",
                    display,
                    "-localhost",
                    "-rfbport",
                    str(vnc_port),
                    "-forever",
                    "-shared",
                    "-nopw",
                    "-xkb",
                ],
                env=runtime_env,
            )
            started_handles.append(x11vnc_proc)
            _wait_for_local_port(vnc_port)
        runtime["x11vnc_proc"] = x11vnc_proc

        websockify_proc = runtime.get("websockify_proc")
        if not _is_process_running(websockify_proc):
            websockify_proc = _launch_local_process(
                [
                    "websockify",
                    f"127.0.0.1:{novnc_port}",
                    f"127.0.0.1:{vnc_port}",
                    "--web",
                    str(get_novnc_web_dir()),
                ],
                env=runtime_env,
            )
            started_handles.append(websockify_proc)
            _wait_for_local_port(novnc_port)
        runtime["websockify_proc"] = websockify_proc

        runtime.update(
            {
                "opened_at": runtime.get("opened_at") or now_iso(),
                "display": display,
                "runtime_env": runtime_env,
                "vnc_port": vnc_port,
                "novnc_port": novnc_port,
                "novnc_local_url": runtime.get("novnc_local_url")
                or f"http://127.0.0.1:{novnc_port}/vnc.html?path=websockify",
            }
        )
        with BROWSER_WORKER_LOCK:
            current = BROWSER_WORKER_RUNTIME.get("session")
            if current is None:
                BROWSER_WORKER_RUNTIME["session"] = runtime
                current = BROWSER_WORKER_RUNTIME["session"]
            else:
                current.update(runtime)
            return current
    except Exception:
        for handle in reversed(started_handles):
            _terminate_process(handle)
        raise


def ensure_browser_display() -> str:
    if get_auth_mode() != "browser_worker" or get_browser_headless():
        return ""
    try:
        runtime = _ensure_browser_worker_display_runtime()
    except Exception as exc:
        raise RuntimeError(BROWSER_DISPLAY_UNAVAILABLE_MESSAGE) from exc

    display = str(runtime.get("display") or "").strip()
    if not display or not _is_process_running(runtime.get("xvfb_proc")) or not _is_display_ready(display):
        raise RuntimeError(BROWSER_DISPLAY_UNAVAILABLE_MESSAGE)
    return display


class BrowserWorkerStartupError(RuntimeError):
    def __init__(self, stage: str, cause: Exception) -> None:
        self.stage = stage
        self.cause = cause
        super().__init__(f"Browser Worker 启动失败（{stage}）：{cause}")


async def _infer_async_browser_auth_requirement(page: Any) -> tuple[bool, str, str | None]:
    page_title = None
    body_text = ""
    try:
        page_title = str(await page.title() or "").strip() or None
    except Exception:
        pass
    try:
        body_text = str(await page.locator("body").inner_text(timeout=2000) or "")
    except Exception:
        pass
    page_url = str(getattr(page, "url", "") or "")
    merged = f"{page_url}\n{page_title or ''}\n{body_text}".lower()
    if any(marker in merged for marker in ("安全验证", "验证码", "captcha", "challenge", "url you requested has been blocked")):
        return True, ErrorCode.CAPTCHA_REQUIRED, page_title
    try:
        login_fields = await page.locator(
            "input[type='password'], input[placeholder*='登录'], input[placeholder*='手机号'], input[placeholder*='验证码']"
        ).count()
    except Exception:
        login_fields = 0
    if login_fields or any(marker in page_url.lower() for marker in ("login", "passport", "signin")):
        return True, ErrorCode.AUTH_REQUIRED, page_title
    return False, ErrorCode.NONE, page_title


class BrowserWorkerController:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._expiry_task: asyncio.Task[None] | None = None
        self._state: dict[str, Any] = {
            "runtime_state": "closed",
            "browser_session_running": False,
            "live_auth_verified": False,
        }

    def public_snapshot(self) -> dict[str, Any]:
        return dict(self._state)

    def _set_state(self, **updates: Any) -> None:
        self._state.update(updates)

    async def open(self) -> dict[str, Any]:
        async with self._lock:
            if self._context is not None and self._page is not None:
                self._renew_expiry_locked()
                return _browser_worker_status_payload(message="远端浏览器窗口已打开")

            await self._close_locked(close_display=True)
            stage = "display"
            self._set_state(runtime_state="starting", browser_session_running=False, live_auth_verified=False)
            try:
                runtime = await asyncio.to_thread(_ensure_browser_worker_display_runtime)
                display = str(runtime.get("display") or get_browser_display()).strip() or get_browser_display()
                runtime_env = dict(runtime.get("runtime_env") or os.environ.copy())
                runtime_env["DISPLAY"] = display

                stage = "playwright"
                self._playwright = await async_playwright().start()
                stage = "chromium"
                self._context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(get_browser_user_data_dir()),
                    headless=False,
                    args=["--start-maximized"],
                    env=runtime_env,
                )
                pages = list(self._context.pages)
                self._page = pages[0] if pages else await self._context.new_page()
                stage = "navigation"
                try:
                    await self._page.goto("https://www.weiq.com/", timeout=45000, wait_until="domcontentloaded")
                except Exception:
                    pass

                session_id = uuid4().hex
                self._set_state(
                    session_id=session_id,
                    runtime_state="open",
                    browser_session_running=True,
                    live_auth_verified=False,
                    blocked=False,
                    page_title=None,
                )
                self._renew_expiry_locked()
                return _browser_worker_status_payload(message="Browser Worker 登录窗口已打开，请在远程浏览器里完成 WEIQ 登录")
            except Exception as exc:
                await self._close_locked(close_display=True)
                self._set_state(runtime_state="failed", last_error=str(exc))
                raise BrowserWorkerStartupError(stage, exc) from exc

    async def status(self) -> dict[str, Any]:
        async with self._lock:
            return _browser_worker_status_payload()

    async def check(self) -> dict[str, Any]:
        async with self._lock:
            if self._context is None or self._page is None:
                self._set_state(live_auth_verified=False, runtime_state="closed")
                return _browser_worker_status_payload()

            state_path = get_legacy_state_json_path()
            try:
                await self._context.storage_state(path=str(state_path))
            except Exception:
                pass
            needs_auth, reason_code, page_title = await _infer_async_browser_auth_requirement(self._page)
            verified = not needs_auth and has_usable_storage_state(str(state_path))
            self._set_state(
                live_auth_verified=verified,
                blocked=reason_code in {ErrorCode.HTTP_BLOCKED, ErrorCode.CAPTCHA_REQUIRED},
                page_title=page_title,
                runtime_state="verified" if verified else "waiting_login",
            )
            return _browser_worker_status_payload(
                message="已实时验证 WEIQ 登录，Browser Worker 已保存长期登录态"
                if verified
                else "远端浏览器仍在等待 WEIQ 登录或安全验证"
            )

    async def close(self, *, session_id: str | None = None) -> dict[str, Any]:
        async with self._lock:
            if session_id and session_id != self._state.get("session_id"):
                return _browser_worker_status_payload()
            await self._close_locked(close_display=True)
            return _browser_worker_status_payload(message="Browser Worker 登录窗口已关闭")

    async def resume_task_after_auth(self, task_id: str) -> dict[str, Any]:
        async with self._lock:
            if self._context is None or self._page is None:
                raise HTTPException(status_code=409, detail="远端浏览器会话未打开，请先打开 WEIQ 验证窗口。")
            row = fetch_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            if row is None:
                raise HTTPException(status_code=404, detail="任务不存在")

            target_url = str(row.get("current_url") or "").strip()
            target_host = urlparse(target_url).hostname or ""
            should_prepare_target = self._state.get("verification_task_id") != task_id
            if should_prepare_target and target_url and (target_host == "weiq.com" or target_host.endswith(".weiq.com")):
                try:
                    await self._page.goto(target_url, timeout=45000, wait_until="domcontentloaded")
                    await self._page.wait_for_timeout(1500)
                except Exception:
                    pass
                self._set_state(verification_task_id=task_id)
            try:
                await self._context.storage_state(path=str(get_legacy_state_json_path()))
            except Exception:
                pass
            needs_auth, reason_code, page_title = await _infer_async_browser_auth_requirement(self._page)
            verified = not needs_auth and has_usable_storage_state(str(get_legacy_state_json_path()))
            self._set_state(
                live_auth_verified=verified,
                blocked=reason_code in {ErrorCode.HTTP_BLOCKED, ErrorCode.CAPTCHA_REQUIRED},
                page_title=page_title,
                runtime_state="verified" if verified else "waiting_login",
            )
            if not verified:
                updates = {
                    "status": TaskStatus.BLOCKED_AUTH,
                    "blocked_reason": reason_code if needs_auth else ErrorCode.AUTH_REQUIRED,
                    "error_code": "BLOCKED_AUTH",
                    "message": "WEIQ 验证尚未完成，请先在远端浏览器中完成验证后再继续抓取。",
                    "current_url": str(getattr(self._page, "url", "") or "").strip() or None,
                    "page_title": page_title,
                    "resolution": "open_browser_session",
                    "can_resume": 1,
                    "finished_at": None,
                }
                upsert_task_event(task_id, updates)
                row = fetch_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
                return build_status_payload(row or {"task_id": task_id, **updates})

            _clear_blocked_auth_context(task_id)
            self._set_state(verification_task_id=None)
            upsert_task_event(
                task_id,
                {
                    "status": TaskStatus.PENDING,
                    "blocked_reason": None,
                    "error_code": ErrorCode.NONE,
                    "message": "已完成 WEIQ 安全验证，任务已重新入队继续执行。",
                    "current_url": str(getattr(self._page, "url", "") or "").strip() or None,
                    "page_title": page_title,
                    "screenshot_path": None,
                    "resolution": None,
                    "can_resume": 0,
                    "finished_at": None,
                    "resume_requested": 1,
                    "cancel_requested": 0,
                },
            )
            enqueue_task(task_id)
            row = fetch_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            return build_status_payload(row or {"task_id": task_id, "status": TaskStatus.PENDING})

    def _renew_expiry_locked(self) -> None:
        if self._expiry_task is not None:
            self._expiry_task.cancel()
        ttl_seconds = get_browser_session_ttl_seconds()
        expires_at = (datetime.now() + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        session_id = str(self._state.get("session_id") or "")
        self._set_state(expires_at=expires_at)
        self._expiry_task = asyncio.create_task(self._expire_after(session_id, ttl_seconds))

    async def _expire_after(self, session_id: str, ttl_seconds: int) -> None:
        try:
            await asyncio.sleep(ttl_seconds)
            await self.close(session_id=session_id)
        except asyncio.CancelledError:
            return

    async def _close_locked(self, *, close_display: bool) -> None:
        current_task = asyncio.current_task()
        if self._expiry_task is not None and self._expiry_task is not current_task:
            self._expiry_task.cancel()
        self._expiry_task = None
        context, playwright_ctx = self._context, self._playwright
        self._context = None
        self._page = None
        self._playwright = None
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if playwright_ctx is not None:
            try:
                await playwright_ctx.stop()
            except Exception:
                pass
        if close_display:
            await asyncio.to_thread(_close_browser_worker_display_runtime)
        self._state = {
            "runtime_state": "closed",
            "browser_session_running": False,
            "live_auth_verified": False,
        }


BROWSER_WORKER_CONTROLLER = BrowserWorkerController()


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
    state_storage: str,
    headless: bool,
    login_url: str = "https://www.weiq.com/",
) -> dict[str, Any]:
    playwright_ctx = sync_playwright().start()
    browser, context, page = init_browser(playwright_ctx, state_storage, headless)
    try:
        page.goto(login_url, timeout=45000, wait_until="domcontentloaded")
    except Exception:
        pass

    preview_image_path, preview_base64 = capture_login_preview(session_id, page)
    is_authenticated, reason_code, pending_message = resolve_auth_completion(page, state_storage)
    if is_authenticated:
        try:
            context.storage_state(path=state_storage)
        except Exception:
            pass
        row = {
            "task_id": task_id,
            "status": "authenticated",
            "login_url": page.url or login_url,
            "state_storage": state_storage,
            "preview_image_path": preview_image_path,
            "qr_image_base64": None,
            "message": "本次抓取临时 WEIQ 登录成功",
            "expires_at": expiry_iso(max(30, get_auth_session_ttl_seconds() // 60)),
        }
        upsert_auth_session(session_id, row)
        write_auth_session_meta(session_id, {"session_id": session_id, "task_id": task_id, "state_storage": state_storage})
        _close_active_session_runtime({"browser": browser, "playwright": playwright_ctx})
        return fetch_auth_session(session_id) or row

    status_text, message = infer_auth_session_state(page, reason_code)
    row = {
        "task_id": task_id,
        "status": status_text,
        "login_url": page.url or login_url,
        "state_storage": state_storage,
        "preview_image_path": preview_image_path,
        "qr_image_base64": preview_base64,
        "message": pending_message or message or "本机 WEIQ 页面只用于参考。服务器抓取环境需要通过本次远端登录会话完成登录。",
        "expires_at": expiry_iso(max(1, get_auth_session_ttl_seconds() // 60)),
    }
    upsert_auth_session(session_id, row)
    write_auth_session_meta(session_id, {"session_id": session_id, "task_id": task_id, "state_storage": state_storage})
    register_active_session(
        session_id,
        task_id=task_id or "",
        page=page,
        context=context,
        state_storage=state_storage,
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
        return False, ErrorCode.AUTH_REQUIRED, "本次任务尚未获得服务器端 WEIQ 登录态。本机 Chrome 登录不会同步到服务器。"
    return True, ErrorCode.NONE, None


def _block_task_for_login(task_id: str, session_id: str, reason_code: str, message: str | None = None) -> None:
    if not task_id:
        return
    upsert_task_event(
        task_id,
        {
            "login_session_id": session_id,
            "status": TaskStatus.BLOCKED_AUTH,
            "blocked_reason": reason_code,
            "message": message or "本次抓取需要登录 WEIQ，请提交本次任务专属登录信息",
        },
    )


def _resume_task_if_authenticated(task_id: str, session_id: str) -> None:
    if not task_id:
        return
    task_row = fetch_one("SELECT status FROM tasks WHERE task_id = ?", (task_id,))
    if task_row is None:
        return
    current_status = str(task_row.get("status") or TaskStatus.PENDING)
    if current_status not in {TaskStatus.BLOCKED_AUTH, TaskStatus.PENDING, TaskStatus.RUNNING}:
        return
    upsert_task_event(
        task_id,
        {
            "login_session_id": session_id,
            "status": TaskStatus.PENDING,
            "blocked_reason": None,
            "message": "已获取本次任务临时登录态，抓取将继续",
            "cancel_requested": 0,
        },
    )
    enqueue_task(task_id)


def ensure_auth_session_runtime(session_id: str) -> dict[str, Any]:
    row = fetch_auth_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="登录会话不存在")
    if is_auth_session_expired(row):
        row = expire_auth_session(session_id)
        return row
    with ACTIVE_AUTH_LOCK:
        session = ACTIVE_AUTH_SESSIONS.get(session_id)
    if session is not None:
        return row

    state_storage = str(row.get("state_storage") or build_auth_session_paths(session_id)["state_storage"]).strip()
    login_url = str(row.get("login_url") or "https://www.weiq.com/").strip() or "https://www.weiq.com/"
    task_id = str(row.get("task_id") or "").strip()
    task = fetch_one("SELECT headless FROM tasks WHERE task_id = ?", (task_id,)) if task_id else None
    headless = bool(task.get("headless")) if task else True
    playwright_ctx = sync_playwright().start()
    browser, context, page = init_browser(playwright_ctx, state_storage, headless)
    try:
        page.goto(login_url, timeout=45000, wait_until="domcontentloaded")
    except Exception:
        pass
    preview_image_path, preview_base64 = capture_login_preview(session_id, page)
    upsert_auth_session(
        session_id,
        {
            "task_id": task_id or row.get("task_id"),
            "login_url": page.url or login_url,
            "state_storage": state_storage,
            "preview_image_path": preview_image_path,
            "qr_image_base64": preview_base64,
            "message": row.get("message") or "本机 WEIQ 页面只用于参考。服务器抓取环境需要通过本次远端登录会话完成登录。",
            "expires_at": row.get("expires_at") or expiry_iso(max(1, get_auth_session_ttl_seconds() // 60)),
        },
    )
    write_auth_session_meta(
        session_id,
        {
            "session_id": session_id,
            "task_id": task_id or None,
            "state_storage": state_storage,
            "login_url": page.url or login_url,
        },
    )
    register_active_session(
        session_id,
        task_id=task_id,
        page=page,
        context=context,
        state_storage=state_storage,
        login_url=page.url or login_url,
        browser=browser,
        playwright=playwright_ctx,
    )
    refreshed = fetch_auth_session(session_id)
    assert refreshed is not None
    return refreshed


def sanitize_account_text(value: str | None) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def submit_auth_session_inputs(session_id: str, payload: AuthSubmitRequest) -> dict[str, Any]:
    session_row = fetch_auth_session(session_id)
    if session_row is None:
        raise HTTPException(status_code=404, detail="登录会话不存在")
    if is_auth_session_expired(session_row):
        session_row = expire_auth_session(session_id)

    if session_row.get("status") == "expired":
        raise HTTPException(status_code=409, detail="本次临时登录会话已过期，请重新创建")

    ensure_auth_session_runtime(session_id)
    with ACTIVE_AUTH_LOCK:
        session = ACTIVE_AUTH_SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=409, detail="当前登录会话还未进入可操作状态，请等待抓取任务进入登录页后重试")

    page = session["page"]
    context = session["context"]
    state_storage = session["state_storage"]
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
                preview_image_path, preview_base64 = capture_login_preview(session_id, page)
                if not click_first_text(targets, SEND_CODE_TEXTS):
                    raise HTTPException(status_code=422, detail="未找到发送验证码按钮，请检查当前 WEIQ 登录页")
                time.sleep(1)
                row = {
                    "status": "waiting_code",
                    "login_url": page.url or session.get("login_url") or "https://www.weiq.com/",
                    "preview_image_path": preview_image_path,
                    "qr_image_base64": preview_base64,
                    "message": "验证码已尝试发送，请输入收到的短信验证码后继续。",
                    "expires_at": expiry_iso(max(1, get_auth_session_ttl_seconds() // 60)),
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

        preview_image_path, preview_base64 = capture_login_preview(session_id, page)
        upsert_auth_session(
            session_id,
            {
                "status": "logging_in",
                "login_url": page.url or session.get("login_url") or "https://www.weiq.com/",
                "preview_image_path": preview_image_path,
                "qr_image_base64": preview_base64,
                "message": "已提交登录信息，正在等待 WEIQ 返回结果。",
                "expires_at": expiry_iso(max(1, get_auth_session_ttl_seconds() // 60)),
            },
        )

        if not click_first_text(targets, SUBMIT_LOGIN_TEXTS):
            raise HTTPException(status_code=422, detail="未找到登录提交按钮，请检查当前 WEIQ 登录页")
        wait_for_page_settle(page)

        is_authenticated, reason_code, pending_message = resolve_auth_completion(page, state_storage)
        if is_authenticated:
            try:
                context.storage_state(path=state_storage)
            except Exception:
                pass
            upsert_auth_session(
                session_id,
                {
                    "status": "authenticated",
                    "login_url": page.url,
                    "preview_image_path": None,
                    "qr_image_base64": None,
                    "message": "本次抓取临时 WEIQ 登录成功",
                    "expires_at": expiry_iso(max(30, get_auth_session_ttl_seconds() // 60)),
                },
            )
            unregister_active_session(session_id)
            _resume_task_if_authenticated(task_id, session_id)
            row = fetch_auth_session(session_id)
            assert row is not None
            return row

    status_text, message = infer_auth_session_state(page, reason_code)
    preview_image_path, preview_base64 = capture_login_preview(session_id, page)
    row = {
        "status": status_text,
        "login_url": page.url or session.get("login_url") or "https://www.weiq.com/",
        "preview_image_path": preview_image_path,
        "qr_image_base64": preview_base64,
        "message": pending_message or message,
        "expires_at": expiry_iso(max(1, get_auth_session_ttl_seconds() // 60)),
    }
    upsert_auth_session(session_id, row)
    _block_task_for_login(task_id, session_id, reason_code, pending_message or message)
    return fetch_auth_session(session_id) or row


def register_active_session(
    session_id: str,
    *,
    task_id: str,
    page,
    context,
    state_storage: str,
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
            "state_storage": state_storage,
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
    paths = build_auth_session_paths(session_id)
    status_text, message = infer_auth_session_state(page, reason_code)
    preview_image_path, qr_image_base64 = capture_login_preview(session_id, page)
    upsert_auth_session(
        session_id,
        {
            "task_id": task_id,
            "status": status_text,
            "login_url": page_url or "https://www.weiq.com/",
            "state_storage": paths["state_storage"],
            "preview_image_path": preview_image_path,
            "qr_image_base64": qr_image_base64,
            "message": message or "本次抓取需要登录 WEIQ，请提交本次任务专属登录信息",
            "expires_at": expiry_iso(max(1, get_auth_session_ttl_seconds() // 60)),
        },
    )
    unregister_active_session(session_id)
    write_auth_session_meta(
        session_id,
        {
            "session_id": session_id,
            "task_id": task_id,
            "state_storage": paths["state_storage"],
            "source_page_url": page_url or "https://www.weiq.com/",
            "captured_from_runtime": True,
        },
    )
    return session_id


def inspect_active_auth_session(session_id: str) -> Optional[dict[str, Any]]:
    with ACTIVE_AUTH_LOCK:
        session = ACTIVE_AUTH_SESSIONS.get(session_id)
    if not session:
        return None
    page = session["page"]
    context = session["context"]
    state_storage = session["state_storage"]
    with session["lock"]:
        is_authenticated, reason_code, pending_message = resolve_auth_completion(page, state_storage)
        if is_authenticated:
            try:
                context.storage_state(path=state_storage)
            except Exception:
                pass
            upsert_auth_session(
                session_id,
                {
                    "status": "authenticated",
                    "login_url": page.url,
                    "preview_image_path": None,
                    "qr_image_base64": None,
                    "message": "本次抓取临时 WEIQ 登录成功",
                    "expires_at": expiry_iso(max(30, get_auth_session_ttl_seconds() // 60)),
                },
            )
            unregister_active_session(session_id)
            _resume_task_if_authenticated(str(session["task_id"]), session_id)
        else:
            status_text, message = infer_auth_session_state(page, reason_code)
            preview_image_path, preview_base64 = capture_login_preview(session_id, page)
            upsert_auth_session(
                session_id,
                {
                    "status": status_text,
                    "login_url": page.url or session.get("login_url") or "https://www.weiq.com/",
                    "preview_image_path": preview_image_path,
                    "qr_image_base64": preview_base64,
                    "message": pending_message or message,
                    "expires_at": expiry_iso(max(1, get_auth_session_ttl_seconds() // 60)),
                },
            )
    return fetch_auth_session(session_id)


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
            _set_task_blocked_auth(
                task_id,
                reason_code=str(event.get("reason_code") or ErrorCode.AUTH_REQUIRED),
                message=_blocked_auth_message(str(event.get("reason_code") or ErrorCode.AUTH_REQUIRED)),
            )

    def should_stop() -> bool:
        return is_cancel_requested(task_id)

    def on_auth_required(reason_code: str, page_url: str, page, context, state_json: str) -> bool:
        if get_auth_mode() != "browser_worker":
            session_id = ensure_auth_session_for_task(task_id, reason_code, page_url, page, context, state_json)
            _block_task_for_login(task_id, session_id, reason_code, f"等待处理登录风控: {page_url}")
        _set_task_blocked_auth(task_id, reason_code=reason_code, page=page, context=context)
        return False

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
        finalize_task_auth_session(task_id, TaskStatus.CANCELLED)
        return

    if get_auth_mode() == "browser_worker":
        session_id = ""
        auth_state_storage = str(get_legacy_state_json_path())
        if not has_usable_storage_state(auth_state_storage):
            _set_task_blocked_auth(
                task_id,
                reason_code=ErrorCode.AUTH_REQUIRED,
                message="Browser Worker 登录态无效，请先打开远端浏览器并完成 WEIQ 安全验证后继续。",
            )
            return
        display = None
        if not bool(row["headless"]):
            try:
                display = ensure_browser_display()
            except RuntimeError as exc:
                upsert_task_event(
                    task_id,
                    {
                        "status": TaskStatus.FAILED,
                        "finished_at": now_iso(),
                        "error_code": "DISPLAY_UNAVAILABLE",
                        "message": str(exc) or BROWSER_DISPLAY_UNAVAILABLE_MESSAGE,
                    },
                )
                return
    else:
        session_id = str(row.get("login_session_id") or "").strip()
        auth_session = fetch_auth_session(session_id) if session_id else None
        display = None
        if not session_id:
            created = create_auth_session(AuthSessionCreateRequest(task_id=task_id, eager=True))
            session_id = created.session_id
            auth_session = fetch_auth_session(session_id)
            row = fetch_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,)) or row
        elif auth_session and is_auth_session_expired(auth_session):
            auth_session = expire_auth_session(session_id)

        auth_state_storage = str(auth_session.get("state_storage") or "").strip() if auth_session else ""
        auth_status = str(auth_session.get("status") or "").strip() if auth_session else ""
        if auth_status != "authenticated" or not auth_state_storage or not has_usable_storage_state(auth_state_storage):
            if auth_session is not None:
                waiting_message = str(auth_session.get("message") or "").strip() or "本次抓取需要登录 WEIQ，请提交本次任务专属登录信息"
            else:
                waiting_message = "本次抓取需要登录 WEIQ，请提交本次任务专属登录信息"
            _block_task_for_login(task_id, session_id, ErrorCode.AUTH_REQUIRED, waiting_message)
            return

    if str(row.get("task_type") or "metrics") == "content_trend":
        upsert_task_event(task_id, {"status": TaskStatus.RUNNING, "started_at": now_iso(), "picked_up_at": now_iso(), "message": "WEIQ 执行器已接单，开始采集微博内容趋势", "current_url": None, "page_title": None, "screenshot_path": None, "resolution": None, "can_resume": 0})
        run_content_trend_task(task_id, row, state_storage=auth_state_storage, display=display)
        latest = fetch_one("SELECT status FROM tasks WHERE task_id = ?", (task_id,))
        if latest and latest["status"] in {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            finalize_task_auth_session(task_id, latest["status"])
        return

    config = CrawlConfig(
        input_excel=row["input_excel"],
        output_excel=row["output_excel"],
        output_dir=row["output_dir"],
        state_json=row["state_json"],
        state_storage=auth_state_storage,
        headless=bool(row["headless"]),
        cooldown_every=row["cooldown_every"],
        cooldown_seconds=row["cooldown_seconds"],
        retry_times=max(1, row["retry_times"]),
        retry_backoff_seconds=max(0, row["retry_backoff_seconds"]),
        resume=bool(row["resume"]),
        run_id=str(row.get("run_id") or "").strip() or None,
        display=display,
    )

    hooks = build_hooks(task_id)

    upsert_task_event(
        task_id,
        {
            "status": TaskStatus.RUNNING,
            "started_at": now_iso(),
            "picked_up_at": now_iso(),
            "message": "WEIQ 执行器已接单，开始抓取",
            "current_url": None,
            "page_title": None,
            "screenshot_path": None,
            "resolution": None,
            "can_resume": 0,
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
                    else _blocked_auth_message(result.error_code)
                ),
                "resolution": "open_browser_session" if result.status == TaskStatus.BLOCKED_AUTH and get_auth_mode() == "browser_worker" else None,
                "can_resume": 1 if result.status == TaskStatus.BLOCKED_AUTH else 0,
            },
        )
        if result.status in {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            finalize_task_auth_session(task_id, result.status)
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
        finalize_task_auth_session(task_id, TaskStatus.FAILED)


async def _resume_browser_worker_task_after_auth(task_id: str) -> dict[str, Any]:
    return await BROWSER_WORKER_CONTROLLER.resume_task_after_auth(task_id)


def _resume_per_task_after_auth(task_id: str, row: dict[str, Any]) -> dict[str, Any]:
    session_id = str(row.get("login_session_id") or "").strip()
    if not session_id:
        return {
            "task_id": task_id,
            "status": "TASK_NOT_RESUMABLE",
            "message": "当前任务没有可恢复的登录会话，请重新发起抓取任务。",
        }
    session_row = fetch_auth_session(session_id)
    if session_row is None:
        return {
            "task_id": task_id,
            "status": "TASK_NOT_RESUMABLE",
            "message": "当前任务登录会话已失效，请重新发起抓取任务。",
        }
    if str(session_row.get("status") or "").strip() != "authenticated":
        upsert_task_event(
            task_id,
            {
                "status": TaskStatus.BLOCKED_AUTH,
                "blocked_reason": ErrorCode.AUTH_REQUIRED,
                "error_code": "BLOCKED_AUTH",
                "message": str(session_row.get("message") or "").strip() or "WEIQ 验证尚未完成，请先完成验证后再继续抓取。",
                "resolution": "submit_login_session",
                "can_resume": 1,
            },
        )
        latest = fetch_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        return build_status_payload(latest or row)

    _clear_blocked_auth_context(task_id)
    upsert_task_event(
        task_id,
        {
            "status": TaskStatus.PENDING,
            "blocked_reason": None,
            "error_code": ErrorCode.NONE,
            "message": "已完成 WEIQ 安全验证，任务已重新入队继续执行。",
            "current_url": None,
            "page_title": None,
            "screenshot_path": None,
            "resolution": None,
            "can_resume": 0,
            "finished_at": None,
            "resume_requested": 1,
            "cancel_requested": 0,
        },
    )
    enqueue_task(task_id)
    latest = fetch_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    return build_status_payload(latest or {"task_id": task_id, "status": TaskStatus.PENDING, "message": "任务已重新入队"})


def worker_loop() -> None:
    global LAST_WORKER_ERROR
    while True:
        task_id = TASK_QUEUE.get()
        with QUEUE_LOCK:
            QUEUED_TASK_IDS.discard(task_id)
            ACTIVE_TASK_IDS.add(task_id)
        # The authenticated browser state is shared. Process one task at a time
        # so concurrent navigations do not trigger WEIQ's risk controls.
        try:
            _run_task_in_background(task_id)
        finally:
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "time": now_iso()}


@app.get("/v1/debug/env")
async def debug_env() -> dict[str, Any]:
    browser_payload = await BROWSER_WORKER_CONTROLLER.status() if get_auth_mode() == "browser_worker" else {}
    return {
        "auth_mode": get_auth_mode(),
        "legacy_state_json": str(get_legacy_state_json_path()),
        "browser_user_data_dir": str(get_browser_user_data_dir()),
        "browser_headless": get_browser_headless(),
        "browser_display": browser_payload.get("browser_display"),
        "display_ready": browser_payload.get("display_ready", False),
        "xvfb_running": browser_payload.get("xvfb_running", False),
        "x11vnc_running": browser_payload.get("x11vnc_running", False),
        "websockify_running": browser_payload.get("websockify_running", False),
        "browser_worker_headless": get_browser_headless(),
        "crawl_will_use_display": browser_payload.get("crawl_will_use_display", False),
        "db_path": str(DB_PATH),
        "runtime_dir": str(get_runtime_dir()),
        "auth_state_dir": str(get_auth_state_dir()),
    }


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


@app.get("/v1/auth/browser/status", response_model=BrowserAuthStatusResponse)
async def get_browser_auth_status() -> BrowserAuthStatusResponse:
    return BrowserAuthStatusResponse(**(await BROWSER_WORKER_CONTROLLER.status()))


@app.post("/v1/auth/browser/open", response_model=BrowserAuthStatusResponse)
async def open_browser_auth() -> BrowserAuthStatusResponse:
    if get_auth_mode() != "browser_worker":
        raise HTTPException(status_code=409, detail="当前服务未启用 browser_worker 模式")
    try:
        payload = await BROWSER_WORKER_CONTROLLER.open()
    except BrowserWorkerStartupError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "BROWSER_WORKER_START_FAILED",
                "stage": exc.stage,
                "message": str(exc),
            },
        ) from exc
    return BrowserAuthStatusResponse(**payload)


@app.post("/v1/auth/browser/check", response_model=BrowserAuthStatusResponse)
async def check_browser_auth() -> BrowserAuthStatusResponse:
    if get_auth_mode() != "browser_worker":
        raise HTTPException(status_code=409, detail="当前服务未启用 browser_worker 模式")
    return BrowserAuthStatusResponse(**(await BROWSER_WORKER_CONTROLLER.check()))


@app.post("/v1/auth/browser/close", response_model=BrowserAuthStatusResponse)
async def close_browser_auth() -> BrowserAuthStatusResponse:
    if get_auth_mode() != "browser_worker":
        raise HTTPException(status_code=409, detail="当前服务未启用 browser_worker 模式")
    return BrowserAuthStatusResponse(**(await BROWSER_WORKER_CONTROLLER.close()))


@app.post("/v1/tasks/crawl", response_model=TaskControlResponse)
def create_task(payload: CreateTaskRequest) -> TaskControlResponse:
    if get_auth_mode() == "browser_worker":
        auth_payload = _browser_worker_status_payload()
        if not auth_payload["authenticated"]:
            return TaskControlResponse(task_id=None, status="AUTH_REQUIRED", message=auth_payload["message"] or "需要先登录 WEIQ")
    task_id = uuid4().hex
    created_at = now_iso()
    materialized = materialize_task_request(payload)
    if get_auth_mode() == "browser_worker":
        materialized["state_storage"] = str(get_legacy_state_json_path())
        materialized["headless"] = get_browser_headless()

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


@app.post("/v1/content-trends", response_model=TaskControlResponse)
def create_content_trend_task(payload: CreateContentTrendRequest) -> TaskControlResponse:
    if get_auth_mode() == "browser_worker":
        auth_payload = _browser_worker_status_payload()
        if not auth_payload["authenticated"]:
            return TaskControlResponse(task_id=None, status="AUTH_REQUIRED", message=auth_payload["message"] or "需要先登录 WEIQ")
    task_id = uuid4().hex
    created_at = now_iso()
    runtime_dir = get_task_runtime_dir(task_id)
    state_storage = str(get_legacy_state_json_path()) if get_auth_mode() == "browser_worker" else str(runtime_dir / "task_auth_placeholder.json")
    execute(
        """
        INSERT INTO tasks (
            task_id, status, progress, current_account, blocked_reason, error_code, message,
            input_excel, output_excel, output_dir, state_json, state_storage,
            headless, cooldown_every, cooldown_seconds, retry_times, retry_backoff_seconds, resume,
            login_session_id, accepted_at, picked_up_at, created_at,
            task_type, target_uid, content_limit, content_json
        ) VALUES (?, ?, 0, NULL, NULL, ?, ?, '', '', ?, ?, ?, ?, 0, 0, 1, 0, 1, NULL, ?, NULL, ?, ?, ?, ?, NULL)
        """,
        (task_id, TaskStatus.PENDING, ErrorCode.NONE, "内容趋势任务已受理，等待执行器接单", str(runtime_dir.resolve()), str((runtime_dir / "content_trend_progress.json").resolve()), state_storage, int(get_browser_headless()) if get_auth_mode() == "browser_worker" else 1, created_at, created_at, "content_trend", payload.uid.strip(), payload.limit),
    )
    enqueue_task(task_id)
    return TaskControlResponse(task_id=task_id, status=TaskStatus.PENDING, message="内容趋势任务已受理")


@app.get("/v1/content-trends/{task_id}")
def get_content_trend_task(task_id: str) -> dict[str, Any]:
    row = fetch_one("SELECT * FROM tasks WHERE task_id = ? AND task_type = 'content_trend'", (task_id,))
    if not row:
        raise HTTPException(status_code=404, detail="内容趋势任务不存在")
    payload = build_status_payload(row)
    try:
        posts = json.loads(str(row.get("content_json") or "[]"))
    except Exception:
        posts = []
    payload["posts"] = posts if isinstance(posts, list) else []
    return payload


@app.get("/v1/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    row = fetch_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    return build_status_payload(row)


@app.post("/v1/tasks/{task_id}/resume-after-auth")
async def resume_task_after_auth(task_id: str) -> dict[str, Any]:
    row = fetch_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["status"] != TaskStatus.BLOCKED_AUTH:
        return {
            "task_id": task_id,
            "status": "TASK_NOT_RESUMABLE",
            "message": "当前任务不在等待人工验证状态，请重新发起抓取任务。",
        }
    if get_auth_mode() == "browser_worker":
        return await _resume_browser_worker_task_after_auth(task_id)
    return _resume_per_task_after_auth(task_id, row)


@app.post("/v1/tasks/{task_id}/cancel", response_model=TaskControlResponse)
def cancel_task(task_id: str) -> TaskControlResponse:
    row = fetch_one("SELECT status, login_session_id FROM tasks WHERE task_id = ?", (task_id,))
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")

    if row["status"] in {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}:
        return TaskControlResponse(task_id=task_id, status=row["status"], message="任务已是终态")

    upsert_task_event(task_id, {"cancel_requested": 1, "message": "已请求取消任务"})
    if row.get("login_session_id"):
        mark_auth_session_cleaned(str(row["login_session_id"]), status="expired", message="本次临时登录态已清理")
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


@app.get("/v1/tasks/{task_id}/blocked-screenshot")
def download_task_blocked_screenshot(task_id: str):
    row = fetch_one("SELECT screenshot_path FROM tasks WHERE task_id = ?", (task_id,))
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    screenshot_path = str(row.get("screenshot_path") or "").strip()
    if not screenshot_path:
        raise HTTPException(status_code=404, detail="当前任务没有可用截图")
    screenshot_file = Path(screenshot_path).resolve()
    if not screenshot_file.exists() or not screenshot_file.is_file():
        raise HTTPException(status_code=404, detail="当前任务截图不存在")
    return FileResponse(path=str(screenshot_file), media_type="image/png", filename=screenshot_file.name)


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
    headless = True
    task_id = payload.task_id
    paths = build_auth_session_paths(session_id)
    if task_id:
        task = fetch_one("SELECT task_id, headless FROM tasks WHERE task_id = ?", (task_id,))
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        headless = bool(task.get("headless"))
    upsert_auth_session(
        session_id,
        {
            "task_id": task_id,
            "status": "waiting_credentials",
            "login_url": "https://www.weiq.com/",
            "state_storage": paths["state_storage"],
            "preview_image_path": None,
            "qr_image_base64": None,
            "message": "本机 WEIQ 页面只用于参考。服务器抓取环境需要通过本次远端登录会话完成登录。",
            "expires_at": expiry_iso(max(1, get_auth_session_ttl_seconds() // 60)),
        },
    )
    write_auth_session_meta(
        session_id,
        {
            "session_id": session_id,
            "task_id": task_id,
            "state_storage": paths["state_storage"],
        },
    )
    if task_id:
        upsert_task_event(task_id, {"login_session_id": session_id})
    if payload.eager:
        row = _build_eager_auth_session_state(
            session_id=session_id,
            task_id=task_id,
            state_storage=paths["state_storage"],
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
    if is_auth_session_expired(row):
        row = expire_auth_session(session_id)
    return build_auth_session_response(row)


@app.post("/v1/auth/session/{session_id}/attach-task", response_model=AuthSessionResponse)
def attach_auth_session_to_task(session_id: str, payload: AuthAttachTaskRequest) -> AuthSessionResponse:
    task = fetch_one("SELECT task_id, login_session_id, status, headless FROM tasks WHERE task_id = ?", (payload.task_id,))
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    existing = fetch_auth_session(session_id)
    if existing is None:
        paths = build_auth_session_paths(session_id)
        upsert_auth_session(
            session_id,
            {
                "task_id": payload.task_id,
                "status": "waiting_credentials",
                "login_url": "https://www.weiq.com/",
                "state_storage": paths["state_storage"],
                "qr_image_base64": None,
                "message": "本机 WEIQ 页面只用于参考。服务器抓取环境需要通过本次远端登录会话完成登录。",
                "expires_at": expiry_iso(max(1, get_auth_session_ttl_seconds() // 60)),
            },
        )

    upsert_task_event(payload.task_id, {"login_session_id": session_id})
    upsert_auth_session(session_id, {"task_id": payload.task_id, "message": "本次登录会话已绑定当前抓取任务"})
    if session_id not in ACTIVE_AUTH_SESSIONS:
        session_row = fetch_auth_session(session_id)
        if session_row and str(session_row.get("status") or "") in AUTH_WAITING_STATUSES:
            _build_eager_auth_session_state(
                session_id=session_id,
                task_id=payload.task_id,
                state_storage=str(session_row.get("state_storage") or build_auth_session_paths(session_id)["state_storage"]),
                headless=bool(task.get("headless")),
            )
    return get_auth_session(session_id)


@app.post("/v1/auth/session/{session_id}/submit", response_model=AuthSessionResponse)
def submit_auth_session(session_id: str, payload: AuthSubmitRequest) -> AuthSessionResponse:
    row = submit_auth_session_inputs(session_id, payload)
    return build_auth_session_response(row)


@app.post("/v1/auth/session/{session_id}/check", response_model=AuthSessionResponse)
def check_auth_session(session_id: str) -> AuthSessionResponse:
    row = inspect_active_auth_session(session_id) or fetch_auth_session(session_id)
    if not row:
        raise HTTPException(status_code=404, detail="登录会话不存在")
    if is_auth_session_expired(row):
        row = expire_auth_session(session_id)
        return build_auth_session_response(row)

    state_storage = str(row.get("state_storage") or "").strip()
    if state_storage and has_usable_storage_state(state_storage):
        upsert_auth_session(
            session_id,
            {
                "status": "authenticated",
                "message": "本次抓取临时 WEIQ 登录成功",
                "preview_image_path": None,
                "qr_image_base64": None,
                "expires_at": expiry_iso(max(30, get_auth_session_ttl_seconds() // 60)),
            },
        )
        row = fetch_auth_session(session_id) or row
        _resume_task_if_authenticated(str(row.get("task_id") or ""), session_id)
        return build_auth_session_response(row)

    waiting_message = "本次任务尚未获得服务器端 WEIQ 登录态。本机 Chrome 登录不会同步到服务器。"
    upsert_auth_session(
        session_id,
        {
            "status": "waiting_credentials",
            "message": waiting_message,
            "expires_at": row.get("expires_at") or expiry_iso(max(1, get_auth_session_ttl_seconds() // 60)),
        },
    )
    row = fetch_auth_session(session_id) or row
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
