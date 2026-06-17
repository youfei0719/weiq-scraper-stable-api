import json
import os
import queue
import re
import shutil
import sqlite3
import threading
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from scraper_runtime import (
    CrawlConfig,
    CrawlHooks,
    CrawlResult,
    ErrorCode,
    TaskStatus,
    get_playwright_launch_kwargs,
    has_usable_storage_state,
    init_browser,
    load_proxy_settings_from_env,
    run_crawl,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso_now() -> str:
    return _utcnow().isoformat()


def _ensure_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_browser_auth_mode(value: str | None) -> str:
    mode = (value or "").strip().lower()
    return mode if mode in {"per_task", "legacy_state"} else "per_task"


def _proxy_enabled() -> bool:
    return load_proxy_settings_from_env() is not None


def _safe_proxy_server() -> str | None:
    proxy = load_proxy_settings_from_env()
    return proxy.safe_server() if proxy else None


def _build_blocked_message() -> str:
    return "当前服务器出口访问 WEIQ 被拦截，无法进入登录页。请更换 stable-api 运行环境、配置合规代理出口，或联系 WEIQ 放行服务器出口 IP。"


def _extract_title_from_html(html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    title = re.sub(r"\s+", " ", match.group(1)).strip()
    return title or None


def _detect_blocked_text(*, title: str | None = None, body: str | None = None) -> bool:
    normalized_title = (title or "").strip().lower()
    normalized_body = (body or "").strip().lower()
    markers = [
        "the url you requested has been blocked",
        "url you requested has been blocked",
    ]
    if any(marker in normalized_title for marker in markers):
        return True
    return "blocked" in normalized_body or any(marker in normalized_body for marker in markers)


def _httpx_proxy_kwargs() -> dict[str, Any]:
    proxy = load_proxy_settings_from_env()
    if not proxy:
        return {"trust_env": False}
    return {"proxy": proxy.as_httpx_proxy(), "trust_env": False}


def _detect_title_and_blocked_from_page(page) -> tuple[str | None, bool]:
    try:
        title = page.title()
    except Exception:
        title = None
    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        try:
            body_text = page.content()
        except Exception:
            body_text = ""
    return title, _detect_blocked_text(title=title, body=body_text)


def _page_body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        try:
            return page.content()
        except Exception:
            return ""


def _legacy_page_has_visible_login_form(page) -> bool:
    try:
        inputs = page.locator("input, textarea")
        count = inputs.count()
    except Exception:
        return False

    login_tokens = ("login", "passport", "password", "密码", "验证码", "手机号", "账号", "账户")
    for index in range(min(count, 20)):
        try:
            locator = inputs.nth(index)
            tag_name = (locator.evaluate("(node) => node.tagName") or "").lower()
            input_type = (locator.get_attribute("type") or "").lower()
            placeholder = (locator.get_attribute("placeholder") or "").lower()
            name = (locator.get_attribute("name") or "").lower()
            aria_label = (locator.get_attribute("aria-label") or "").lower()
            value = " ".join([tag_name, input_type, placeholder, name, aria_label])
        except Exception:
            continue
        if "password" in value:
            return True
        if any(token in value for token in login_tokens):
            return True
    return False


def _legacy_page_status(page) -> dict[str, Any]:
    title, blocked = _detect_title_and_blocked_from_page(page)
    body_text = _page_body_text(page)
    url = (page.url or "").strip()
    normalized_body = body_text.lower()
    normalized_url = url.lower()
    login_like = "login" in normalized_url or "passport" in normalized_url or _legacy_page_has_visible_login_form(page)
    return {
        "title": title,
        "blocked": blocked,
        "login_like": login_like,
        "authenticated": not blocked and not login_like,
        "body_excerpt": body_text[:240],
        "url": url,
    }


def _requests_weiq_access_probe() -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "status_code": None,
        "final_url": None,
        "title_detected": None,
        "blocked_detected": False,
        "error": None,
    }
    try:
        with httpx.Client(follow_redirects=True, timeout=20.0, **_httpx_proxy_kwargs()) as client:
            response = client.get("https://www.weiq.com/")
        title = _extract_title_from_html(response.text)
        blocked = _detect_blocked_text(title=title, body=response.text)
        result.update(
            {
                "ok": 200 <= response.status_code < 400 and not blocked,
                "status_code": response.status_code,
                "final_url": str(response.url),
                "title_detected": title,
                "blocked_detected": blocked,
            }
        )
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _playwright_weiq_access_probe(runtime_dir: str) -> dict[str, Any]:
    debug_dir = _ensure_dir(Path(runtime_dir) / "debug")
    screenshot_path = str(debug_dir / f"weiq_access_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.png")
    result: dict[str, Any] = {
        "ok": False,
        "status_code": None,
        "final_url": None,
        "title": None,
        "blocked_detected": False,
        "screenshot_path": screenshot_path,
        "error": None,
    }
    try:
        with sync_playwright() as p:
            browser, context, page = init_browser(p, headless=True, state_storage=None)
            try:
                response = page.goto("https://www.weiq.com/", timeout=60000, wait_until="domcontentloaded")
                title, blocked = _detect_title_and_blocked_from_page(page)
                page.screenshot(path=screenshot_path, full_page=True)
                status_code = response.status if response else None
                result.update(
                    {
                        "ok": status_code is not None and 200 <= status_code < 400 and not blocked,
                        "status_code": status_code,
                        "final_url": page.url,
                        "title": title,
                        "blocked_detected": blocked,
                    }
                )
            finally:
                browser.close()
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _detect_public_ip() -> str | None:
    try:
        with httpx.Client(timeout=10.0, **_httpx_proxy_kwargs()) as client:
            response = client.get("https://api.ipify.org?format=json")
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None
    ip = payload.get("ip")
    return str(ip) if ip else None


@dataclass(slots=True)
class ApiSettings:
    db_path: str
    runtime_dir: str
    auth_state_dir: str
    auth_session_ttl_seconds: int
    keep_auth_state_for_debug: bool
    browser_auth_mode: str
    legacy_state_json: str
    legacy_headless: bool
    login_url: str
    proxy_server: str | None
    proxy_username: str | None
    proxy_password: str | None
    proxy_bypass: str | None


@dataclass(slots=True)
class LegacyLoginBrowserSession:
    playwright: Any
    browser: Any
    context: Any
    page: Any
    login_url: str
    state_json_path: str
    headless: bool
    opened_at: datetime
    last_checked_at: datetime | None = None


_legacy_login_session_lock = threading.Lock()
_legacy_login_session: LegacyLoginBrowserSession | None = None


def load_settings() -> ApiSettings:
    runtime_dir = os.environ.get("WEIQ_API_RUNTIME_DIR", "/opt/weiq-scraper-stable-api/runtime")
    auth_state_dir = os.environ.get("WEIQ_AUTH_STATE_DIR", str(Path(runtime_dir) / "auth_sessions"))
    db_path = os.environ.get("WEIQ_DB_PATH", "/opt/weiq-scraper-stable-api/weiq_local.db")
    legacy_state_json = os.environ.get("WEIQ_LEGACY_STATE_JSON", str(Path(runtime_dir) / "state.json"))
    return ApiSettings(
        db_path=db_path,
        runtime_dir=runtime_dir,
        auth_state_dir=auth_state_dir,
        auth_session_ttl_seconds=int(os.environ.get("WEIQ_AUTH_SESSION_TTL_SECONDS", "600")),
        keep_auth_state_for_debug=_as_bool(os.environ.get("WEIQ_KEEP_AUTH_STATE_FOR_DEBUG"), False),
        browser_auth_mode=_normalize_browser_auth_mode(os.environ.get("WEIQ_BROWSER_AUTH_MODE")),
        legacy_state_json=legacy_state_json,
        legacy_headless=_as_bool(os.environ.get("WEIQ_LEGACY_HEADLESS"), False),
        login_url=os.environ.get("WEIQ_LOGIN_URL", "https://www.weiq.com/"),
        proxy_server=os.environ.get("WEIQ_PROXY_SERVER", "").strip() or None,
        proxy_username=os.environ.get("WEIQ_PROXY_USERNAME", "").strip() or None,
        proxy_password=os.environ.get("WEIQ_PROXY_PASSWORD", "").strip() or None,
        proxy_bypass=os.environ.get("WEIQ_PROXY_BYPASS", "").strip() or None,
    )


def _assert_single_worker() -> None:
    for env_name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        raw = os.environ.get(env_name)
        if raw and raw.isdigit() and int(raw) > 1:
            raise RuntimeError("cloud_api.py 目前只支持单 worker 进程，请使用 --workers 1")


def _connect(settings: ApiSettings) -> sqlite3.Connection:
    _ensure_dir(Path(settings.db_path).parent)
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_conn(settings: ApiSettings):
    conn = _connect(settings)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


AUTH_SESSION_COLUMNS: dict[str, str] = {
    "session_id": "TEXT PRIMARY KEY",
    "task_id": "TEXT",
    "status": "TEXT NOT NULL",
    "login_url": "TEXT",
    "message": "TEXT",
    "expires_at": "TEXT",
    "state_storage": "TEXT",
    "preview_image_path": "TEXT",
    "created_at": "TEXT",
    "updated_at": "TEXT",
    "consumed_at": "TEXT",
    "expired_at": "TEXT",
    "cleanup_at": "TEXT",
}

TASK_COLUMNS: dict[str, str] = {
    "task_id": "TEXT PRIMARY KEY",
    "status": "TEXT NOT NULL",
    "progress": "REAL DEFAULT 0",
    "current_account": "TEXT",
    "total_accounts": "INTEGER DEFAULT 0",
    "processed_accounts": "INTEGER DEFAULT 0",
    "success_accounts": "INTEGER DEFAULT 0",
    "failed_accounts": "INTEGER DEFAULT 0",
    "skipped_accounts": "INTEGER DEFAULT 0",
    "message": "TEXT",
    "error_code": "TEXT",
    "input_excel": "TEXT",
    "output_excel": "TEXT",
    "output_dir": "TEXT",
    "state_storage": "TEXT",
    "headless": "INTEGER DEFAULT 1",
    "login_session_id": "TEXT",
    "accounts_json": "TEXT",
    "retry_times": "INTEGER DEFAULT 1",
    "retry_backoff_seconds": "INTEGER DEFAULT 3",
    "resume": "INTEGER DEFAULT 0",
    "created_at": "TEXT",
    "updated_at": "TEXT",
    "started_at": "TEXT",
    "finished_at": "TEXT",
}


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for column, ddl in columns.items():
        if column not in existing:
            ddl_for_alter = ddl.replace(" PRIMARY KEY", "")
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_for_alter}")


def init_db(settings: ApiSettings) -> None:
    with db_conn(settings) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_sessions (
                session_id TEXT PRIMARY KEY,
                task_id TEXT,
                status TEXT NOT NULL,
                login_url TEXT,
                message TEXT,
                expires_at TEXT,
                state_storage TEXT,
                preview_image_path TEXT,
                created_at TEXT,
                updated_at TEXT,
                consumed_at TEXT,
                expired_at TEXT,
                cleanup_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                progress REAL DEFAULT 0,
                current_account TEXT,
                total_accounts INTEGER DEFAULT 0,
                processed_accounts INTEGER DEFAULT 0,
                success_accounts INTEGER DEFAULT 0,
                failed_accounts INTEGER DEFAULT 0,
                skipped_accounts INTEGER DEFAULT 0,
                message TEXT,
                error_code TEXT,
                input_excel TEXT,
                output_excel TEXT,
                output_dir TEXT,
                state_storage TEXT,
                headless INTEGER DEFAULT 1,
                login_session_id TEXT,
                accounts_json TEXT,
                retry_times INTEGER DEFAULT 1,
                retry_backoff_seconds INTEGER DEFAULT 3,
                resume INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT,
                started_at TEXT,
                finished_at TEXT
            )
            """
        )
        _ensure_columns(conn, "auth_sessions", AUTH_SESSION_COLUMNS)
        _ensure_columns(conn, "tasks", TASK_COLUMNS)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _get_auth_session(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM auth_sessions WHERE session_id = ?", (session_id,)).fetchone()
    return _row_to_dict(row)


def _get_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return _row_to_dict(row)


def _update_row(conn: sqlite3.Connection, table: str, pk_name: str, pk_value: str, **updates: Any) -> None:
    if not updates:
        return
    updates["updated_at"] = _iso_now()
    columns = ", ".join(f"{key} = ?" for key in updates)
    values = list(updates.values()) + [pk_value]
    conn.execute(f"UPDATE {table} SET {columns} WHERE {pk_name} = ?", values)


def _cleanup_auth_session(settings: ApiSettings, session: dict[str, Any]) -> None:
    state_storage = session.get("state_storage")
    if not state_storage:
        return
    session_dir = Path(state_storage).parent
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)
    with db_conn(settings) as conn:
        conn.execute(
            "UPDATE auth_sessions SET cleanup_at = ?, updated_at = ? WHERE session_id = ?",
            (_iso_now(), _iso_now(), session["session_id"]),
        )


def _legacy_state_path(settings: ApiSettings) -> Path:
    return Path(settings.legacy_state_json)


def _legacy_state_exists(settings: ApiSettings) -> bool:
    return _legacy_state_path(settings).exists()


def _legacy_state_usable(settings: ApiSettings) -> bool:
    return has_usable_storage_state(settings.legacy_state_json)


def _close_legacy_login_session() -> None:
    global _legacy_login_session
    with _legacy_login_session_lock:
        session = _legacy_login_session
        _legacy_login_session = None
    if session is None:
        return
    for resource_name in ("context", "browser"):
        resource = getattr(session, resource_name, None)
        if resource is None:
            continue
        try:
            resource.close()
        except Exception:
            pass
    playwright = getattr(session, "playwright", None)
    if playwright is not None:
        try:
            playwright.stop()
        except Exception:
            pass


def _get_legacy_login_session() -> LegacyLoginBrowserSession | None:
    with _legacy_login_session_lock:
        session = _legacy_login_session
    if session is None:
        return None
    try:
        _ = session.page.url
        return session
    except Exception:
        _close_legacy_login_session()
        return None


def _set_legacy_login_session(session: LegacyLoginBrowserSession | None) -> None:
    global _legacy_login_session
    with _legacy_login_session_lock:
        _legacy_login_session = session


def _open_legacy_login_browser(settings: ApiSettings) -> LegacyLoginBrowserSession:
    _close_legacy_login_session()
    state_path = _legacy_state_path(settings)
    _ensure_dir(state_path.parent)
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(**get_playwright_launch_kwargs(headless=settings.legacy_headless))
    context = browser.new_context()
    page = context.new_page()
    page.goto(settings.login_url, timeout=60000, wait_until="domcontentloaded")
    session = LegacyLoginBrowserSession(
        playwright=playwright,
        browser=browser,
        context=context,
        page=page,
        login_url=settings.login_url,
        state_json_path=str(state_path),
        headless=settings.legacy_headless,
        opened_at=_utcnow(),
    )
    _set_legacy_login_session(session)
    return session


def _check_legacy_login_session(settings: ApiSettings) -> dict[str, Any]:
    state_path = _legacy_state_path(settings)
    session = _get_legacy_login_session()
    state_exists = state_path.exists()
    payload: dict[str, Any] = {
        "authenticated": False,
        "state_json_exists": state_exists,
        "state_json_path": str(state_path),
        "message": "未检测到可用的 legacy 登录浏览器",
        "blocked": False,
    }
    if session is None:
        if _legacy_state_usable(settings):
            payload.update(
                {
                    "authenticated": True,
                    "message": "已检测到可用的 legacy 登录态",
                }
            )
        return payload

    session.last_checked_at = _utcnow()
    page_status = _legacy_page_status(session.page)
    if page_status["blocked"]:
        payload["blocked"] = True
        payload["message"] = _build_blocked_message()
        return payload

    if page_status["login_like"]:
        payload["message"] = "当前浏览器仍停留在 WEIQ 登录页，请先完成人工登录"
        return payload

    try:
        _ensure_dir(state_path.parent)
        session.context.storage_state(path=str(state_path))
    except Exception as exc:
        payload["message"] = f"保存 legacy 登录态失败: {exc}"
        return payload

    if _legacy_state_usable(settings):
        payload.update(
            {
                "authenticated": True,
                "message": "已保存可用的 legacy 登录态",
            }
        )
    else:
        payload["message"] = "已尝试保存 legacy 登录态，但文件不可用"
    return payload


def _session_status_for_check(session: dict[str, Any]) -> tuple[str, str]:
    expires_at = _parse_iso(session.get("expires_at"))
    if expires_at and expires_at <= _utcnow():
        return "expired", "登录会话已过期"
    if has_usable_storage_state(session.get("state_storage")):
        return "authenticated", "本次抓取临时 WEIQ 登录成功"
    if session.get("status") == "failed":
        return "failed", session.get("message") or "登录失败"
    if session.get("status") == "waiting_code":
        return "waiting_code", session.get("message") or "等待验证码"
    return "waiting_credentials", session.get("message") or "等待提交登录信息"


def _safe_debug_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _write_accounts_excel(path: str, accounts: list[dict[str, Any]]) -> None:
    _ensure_dir(Path(path).parent)
    pd.DataFrame(accounts).to_excel(path, index=False)


def _find_first(page, selectors: list[str]):
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() > 0:
                return locator.first
        except Exception:
            continue
    return None


def _collect_visible_inputs(page) -> list[dict[str, str]]:
    js = """
    () => Array.from(document.querySelectorAll('input, textarea'))
      .filter((el) => {
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
      })
      .slice(0, 20)
      .map((el) => ({
        tag: (el.tagName || '').toLowerCase(),
        type: el.getAttribute('type') || '',
        name: el.getAttribute('name') || '',
        placeholder: el.getAttribute('placeholder') || '',
        id: el.getAttribute('id') || '',
        class: el.getAttribute('class') || '',
      }))
    """
    try:
        return page.evaluate(js)
    except Exception:
        return []


def _build_login_debug_message(page, prefix: str) -> str:
    try:
        title = page.title()
    except Exception:
        title = ""
    inputs = _collect_visible_inputs(page)
    input_preview = "; ".join(
        f"type={item.get('type','')} name={item.get('name','')} placeholder={item.get('placeholder','')} id={item.get('id','')} class={item.get('class','')}"
        for item in inputs[:8]
    )
    parts = [prefix, f"url={page.url}", f"title={title}"]
    if input_preview:
        parts.append(f"visible_inputs={input_preview}")
    return " | ".join(parts)


def _save_login_failure_artifacts(page, session: dict[str, Any]) -> str | None:
    session_dir = Path(session["state_storage"]).parent
    screenshot_path = session_dir / "login_failed.png"
    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
        return str(screenshot_path)
    except Exception:
        return None


def _submit_auth_session_with_browser(session: dict[str, Any], payload: dict[str, Any], settings: ApiSettings) -> tuple[str, str]:
    login_type = str(payload.get("login_type") or "").strip()
    action = str(payload.get("action") or "submit").strip()
    state_storage = session["state_storage"]
    _ensure_dir(Path(state_storage).parent)
    with sync_playwright() as p:
        browser, context, page = init_browser(p, headless=True, state_storage=None)
        try:
            page.goto(settings.login_url, timeout=60000, wait_until="domcontentloaded")
            _, blocked_detected = _detect_title_and_blocked_from_page(page)
            if blocked_detected:
                _save_login_failure_artifacts(page, session)
                return "failed", _build_blocked_message()
            if login_type == "password":
                username = payload.get("username") or payload.get("phone") or payload.get("account")
                password = payload.get("password")
                if not username or not password:
                    return "waiting_credentials", "缺少账号或密码"
                user_input = _find_first(page, ["input[type='text']", "input[placeholder*='手机号']", "input[placeholder*='账号']", "input[name='mobile']", "input[name='phone']", "input[name='username']"])
                pass_input = _find_first(page, ["input[type='password']", "input[placeholder*='密码']", "input[name='password']"])
                submit_btn = _find_first(page, ["button:has-text('登录')", "button:has-text('立即登录')", "button[type='submit']"])
                if user_input is None:
                    _save_login_failure_artifacts(page, session)
                    return "failed", _build_login_debug_message(page, "找不到账号输入框")
                if pass_input is None:
                    _save_login_failure_artifacts(page, session)
                    return "failed", _build_login_debug_message(page, "找不到密码输入框")
                if submit_btn is None:
                    _save_login_failure_artifacts(page, session)
                    return "failed", _build_login_debug_message(page, "找不到登录提交按钮")
                user_input.fill(str(username))
                pass_input.fill(str(password))
                submit_btn.click()
            elif login_type == "phone_code":
                phone = payload.get("phone")
                if not phone:
                    return "waiting_credentials", "缺少手机号"
                phone_input = _find_first(page, ["input[type='tel']", "input[placeholder*='手机号']", "input[name='mobile']", "input[name='phone']"])
                if phone_input is None:
                    _save_login_failure_artifacts(page, session)
                    return "failed", _build_login_debug_message(page, "找不到账号输入框")
                phone_input.fill(str(phone))
                if action == "send_code":
                    send_btn = _find_first(page, ["button:has-text('发送验证码')", "button:has-text('获取验证码')", "button:has-text('发送')"])
                    if send_btn is None:
                        _save_login_failure_artifacts(page, session)
                        return "failed", _build_login_debug_message(page, "找不到发送验证码按钮")
                    send_btn.click()
                    return "waiting_code", "验证码已发送，请提交验证码"
                code = payload.get("code") or payload.get("verification_code")
                if not code:
                    return "waiting_code", "等待验证码"
                code_input = _find_first(page, ["input[placeholder*='验证码']", "input[name='code']", "input[name='sms_code']", "input[inputmode='numeric']"])
                submit_btn = _find_first(page, ["button:has-text('登录')", "button:has-text('确认')", "button[type='submit']"])
                if code_input is None:
                    _save_login_failure_artifacts(page, session)
                    return "failed", _build_login_debug_message(page, "需要验证码")
                if submit_btn is None:
                    _save_login_failure_artifacts(page, session)
                    return "failed", _build_login_debug_message(page, "找不到验证码登录提交按钮")
                code_input.fill(str(code))
                submit_btn.click()
            else:
                return "waiting_credentials", "不支持的 login_type"

            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            context.storage_state(path=state_storage)
            if has_usable_storage_state(state_storage):
                return "authenticated", "本次抓取临时 WEIQ 登录成功"
            _save_login_failure_artifacts(page, session)
            return "waiting_credentials", _build_login_debug_message(page, "未检测到有效登录态，请重试")
        except PlaywrightTimeoutError:
            _save_login_failure_artifacts(page, session)
            return "failed", _build_login_debug_message(page, "触发人机验证或页面加载超时")
        except Exception as exc:
            _save_login_failure_artifacts(page, session)
            return "failed", _build_login_debug_message(page, f"登录执行异常: {exc}")
        finally:
            browser.close()


class AccountPayload(BaseModel):
    nickname: str | None = None
    uid: str
    account_id: str | None = None


class CreateAuthSessionRequest(BaseModel):
    task_id: str | None = None


class SubmitAuthSessionRequest(BaseModel):
    login_type: str
    action: str | None = "submit"
    username: str | None = None
    password: str | None = None
    phone: str | None = None
    code: str | None = None
    verification_code: str | None = None


class CrawlTaskRequest(BaseModel):
    accounts: list[AccountPayload]
    login_session_id: str | None = None
    headless: bool = True
    retry_times: int = 1
    retry_backoff_seconds: int = 3
    resume: bool = False


class RuntimeManager:
    def __init__(self, settings: ApiSettings):
        self.settings = settings
        self.queue: queue.Queue[str] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.last_worker_error: str | None = None

    def start(self) -> None:
        _assert_single_worker()
        _ensure_dir(self.settings.runtime_dir)
        _ensure_dir(self.settings.auth_state_dir)
        init_db(self.settings)
        with self.lock:
            if self.worker_thread and self.worker_thread.is_alive():
                return
            self.worker_thread = threading.Thread(target=self._worker_loop, name="weiq-api-worker", daemon=True)
            self.worker_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=2)

    def enqueue(self, task_id: str) -> None:
        self.queue.put(task_id)

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                task_id = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._run_task(task_id)
            except Exception as exc:
                self.last_worker_error = str(exc)
                with db_conn(self.settings) as conn:
                    _update_row(
                        conn,
                        "tasks",
                        "task_id",
                        task_id,
                        status=TaskStatus.FAILED.value,
                        message=f"worker 执行失败: {exc}",
                        error_code=ErrorCode.RUNTIME_ERROR.value,
                        finished_at=_iso_now(),
                    )
            finally:
                self.queue.task_done()

    def _run_task(self, task_id: str) -> None:
        with db_conn(self.settings) as conn:
            task = _get_task(conn, task_id)
            if task is None:
                return
            if task["status"] == TaskStatus.CANCELLED.value:
                return
            browser_auth_mode = self.settings.browser_auth_mode
            session = None
            state_storage = task.get("state_storage")
            headless = bool(task["headless"])
            if browser_auth_mode == "legacy_state":
                state_storage = self.settings.legacy_state_json
                headless = self.settings.legacy_headless
                if not has_usable_storage_state(state_storage):
                    raise RuntimeError("legacy 登录态缺少有效 storage_state")
            else:
                session_id = str(task.get("login_session_id") or "").strip()
                if not session_id:
                    raise RuntimeError("登录会话不存在")
                session = _get_auth_session(conn, session_id)
                if session is None:
                    raise RuntimeError("登录会话不存在")
                if not has_usable_storage_state(session.get("state_storage")):
                    raise RuntimeError("登录会话缺少有效 storage_state")
                state_storage = session["state_storage"]
                headless = bool(task["headless"])
            _update_row(
                conn,
                "tasks",
                "task_id",
                task_id,
                status=TaskStatus.RUNNING.value,
                started_at=_iso_now(),
                message="任务开始执行",
                state_storage=state_storage,
            )
            if session is not None:
                conn.execute(
                    "UPDATE auth_sessions SET task_id = ?, consumed_at = ?, updated_at = ? WHERE session_id = ?",
                    (task_id, _iso_now(), _iso_now(), session["session_id"]),
                )
            task = _get_task(conn, task_id)

        accounts = json.loads(task["accounts_json"] or "[]")

        def is_cancelled() -> bool:
            with db_conn(self.settings) as conn:
                latest = _get_task(conn, task_id)
                return bool(latest and latest["status"] == TaskStatus.CANCELLED.value)

        def on_status(**payload: Any) -> None:
            updates: dict[str, Any] = {}
            for key in (
                "status",
                "progress",
                "current_account",
                "total_accounts",
                "processed_accounts",
                "success_accounts",
                "failed_accounts",
                "skipped_accounts",
                "message",
            ):
                if key in payload:
                    updates[key] = payload[key]
            if updates:
                with db_conn(self.settings) as conn:
                    _update_row(conn, "tasks", "task_id", task_id, **updates)

        result = run_crawl(
            CrawlConfig(
                accounts=accounts,
                output_dir=task["output_dir"],
                output_excel=task["output_excel"],
                state_storage=state_storage,
                headless=headless,
                require_login=True,
                prompt_for_login_if_missing=False,
                wait_on_anti_spider=False,
                save_storage_state=True if browser_auth_mode == "legacy_state" else False,
            ),
            hooks=CrawlHooks(on_status=on_status, is_cancelled=is_cancelled),
        )

        with db_conn(self.settings) as conn:
            status = result.status.value
            error_code = result.error_code.value if result.error_code else None
            _update_row(
                conn,
                "tasks",
                "task_id",
                task_id,
                status=status,
                progress=1.0 if status == TaskStatus.SUCCESS.value else task.get("progress", 0),
                total_accounts=result.total_accounts,
                processed_accounts=result.processed_accounts,
                success_accounts=result.success_accounts,
                failed_accounts=result.failed_accounts,
                skipped_accounts=result.skipped_accounts,
                message=result.message,
                error_code=error_code,
                output_excel=result.output_excel,
                finished_at=_iso_now(),
            )
            if self.settings.browser_auth_mode != "legacy_state":
                login_session_id = str(task.get("login_session_id") or "").strip()
                session = _get_auth_session(conn, login_session_id) if login_session_id else None
        if session and not self.settings.keep_auth_state_for_debug:
            _cleanup_auth_session(self.settings, session)


_runtime_manager: RuntimeManager | None = None


def get_runtime_manager() -> RuntimeManager:
    global _runtime_manager
    if _runtime_manager is None:
        _runtime_manager = RuntimeManager(load_settings())
        _runtime_manager.start()
    return _runtime_manager


def _task_response(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "progress": task["progress"],
        "current_account": task["current_account"],
        "total_accounts": task["total_accounts"],
        "processed_accounts": task["processed_accounts"],
        "success_accounts": task["success_accounts"],
        "failed_accounts": task["failed_accounts"],
        "skipped_accounts": task["skipped_accounts"],
        "message": task["message"],
        "error_code": task["error_code"],
        "login_session_id": task["login_session_id"],
        "output_excel": task["output_excel"],
        "export_file": task["output_excel"],
        "created_at": task["created_at"],
        "started_at": task["started_at"],
        "finished_at": task["finished_at"],
    }


def _shutdown() -> None:
    global _runtime_manager
    _close_legacy_login_session()
    if _runtime_manager is not None:
        _runtime_manager.stop()
        _runtime_manager = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_runtime_manager()
    try:
        yield
    finally:
        _shutdown()


app = FastAPI(title="WEIQ Scraper Stable API", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "time": _iso_now()}


@app.get("/v1/worker/health")
def worker_health() -> dict[str, Any]:
    runtime = get_runtime_manager()
    with db_conn(runtime.settings) as conn:
        pending_count = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (TaskStatus.PENDING.value,)).fetchone()[0]
        running_count = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (TaskStatus.RUNNING.value,)).fetchone()[0]
    return {
        "worker_alive": bool(runtime.worker_thread and runtime.worker_thread.is_alive()),
        "queue_size": runtime.queue.qsize(),
        "pending_count": pending_count,
        "running_count": running_count,
        "last_worker_error": runtime.last_worker_error,
        "process_id": os.getpid(),
        "db_path": runtime.settings.db_path,
    }


@app.get("/v1/debug/env")
def debug_env() -> dict[str, Any]:
    runtime = get_runtime_manager()
    with db_conn(runtime.settings) as conn:
        return {
            "cwd": os.getcwd(),
            "python_executable": os.sys.executable,
            "db_path": runtime.settings.db_path,
            "db_exists": Path(runtime.settings.db_path).exists(),
            "runtime_dir": runtime.settings.runtime_dir,
            "auth_state_dir": runtime.settings.auth_state_dir,
            "auth_state_dir_exists": Path(runtime.settings.auth_state_dir).exists(),
            "auth_state_dir_writable": os.access(runtime.settings.auth_state_dir, os.W_OK) if Path(runtime.settings.auth_state_dir).exists() else False,
            "browser_auth_mode": runtime.settings.browser_auth_mode,
            "legacy_state_json": runtime.settings.legacy_state_json,
            "legacy_state_json_exists": _legacy_state_exists(runtime.settings),
            "legacy_state_json_usable": _legacy_state_usable(runtime.settings),
            "legacy_headless": runtime.settings.legacy_headless,
            "supports_legacy_state": True,
            "supports_per_task": True,
            "auth_sessions_columns": _safe_debug_columns(conn, "auth_sessions"),
            "tasks_columns": _safe_debug_columns(conn, "tasks"),
            "worker_alive": bool(runtime.worker_thread and runtime.worker_thread.is_alive()),
            "process_id": os.getpid(),
            "playwright_available": True,
            "proxy_enabled": _proxy_enabled(),
            "proxy_server": _safe_proxy_server(),
        }


@app.get("/v1/debug/weiq-access")
def debug_weiq_access() -> dict[str, Any]:
    runtime = get_runtime_manager()
    return {
        "requests": _requests_weiq_access_probe(),
        "playwright": _playwright_weiq_access_probe(runtime.settings.runtime_dir),
        "egress": {
            "public_ip": _detect_public_ip(),
            "proxy_enabled": _proxy_enabled(),
            "proxy_server": _safe_proxy_server(),
        },
    }


@app.post("/v1/auth/session")
def create_auth_session(request: CreateAuthSessionRequest) -> dict[str, Any]:
    runtime = get_runtime_manager()
    session_id = str(uuid4())
    session_dir = _ensure_dir(Path(runtime.settings.auth_state_dir) / session_id)
    state_storage = str(session_dir / "storage_state.json")
    created_at = _iso_now()
    expires_at = (_utcnow() + timedelta(seconds=runtime.settings.auth_session_ttl_seconds)).isoformat()
    with db_conn(runtime.settings) as conn:
        conn.execute(
            """
            INSERT INTO auth_sessions (
                session_id, task_id, status, login_url, message, expires_at,
                state_storage, preview_image_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                request.task_id,
                "waiting_credentials",
                runtime.settings.login_url,
                "等待提交登录信息",
                expires_at,
                state_storage,
                None,
                created_at,
                created_at,
            ),
        )
    return {
        "session_id": session_id,
        "status": "waiting_credentials",
        "login_url": runtime.settings.login_url,
        "message": "等待提交登录信息",
        "expires_at": expires_at,
        "state_storage": state_storage,
    }


@app.post("/v1/auth/session/{session_id}/submit")
def submit_auth_session(session_id: str, request: SubmitAuthSessionRequest) -> dict[str, Any]:
    runtime = get_runtime_manager()
    with db_conn(runtime.settings) as conn:
        session = _get_auth_session(conn, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")

    status, message = _submit_auth_session_with_browser(session, request.model_dump(), runtime.settings)
    with db_conn(runtime.settings) as conn:
        updates: dict[str, Any] = {"status": status, "message": message}
        if status == "expired":
            updates["expired_at"] = _iso_now()
        _update_row(conn, "auth_sessions", "session_id", session_id, **updates)
        session = _get_auth_session(conn, session_id)
    return {
        "session_id": session_id,
        "status": status,
        "message": message,
        "login_url": session["login_url"],
        "expires_at": session["expires_at"],
        "state_storage": session["state_storage"],
    }


@app.post("/v1/auth/session/{session_id}/check")
def check_auth_session(session_id: str) -> dict[str, Any]:
    runtime = get_runtime_manager()
    with db_conn(runtime.settings) as conn:
        session = _get_auth_session(conn, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        status, message = _session_status_for_check(session)
        updates: dict[str, Any] = {"status": status, "message": message}
        if status == "expired":
            updates["expired_at"] = _iso_now()
        _update_row(conn, "auth_sessions", "session_id", session_id, **updates)
        session = _get_auth_session(conn, session_id)
    return {
        "session_id": session_id,
        "status": status,
        "message": message,
        "login_url": session["login_url"],
        "expires_at": session["expires_at"],
        "state_storage": session["state_storage"],
    }


@app.post("/v1/auth/legacy/open-login")
def open_legacy_login() -> dict[str, Any]:
    runtime = get_runtime_manager()
    session = _open_legacy_login_browser(runtime.settings)
    return {
        "status": "opened",
        "message": "已打开 legacy 登录浏览器，请在窗口中完成 WEIQ 人工登录并保持窗口打开。",
        "login_url": session.login_url,
        "state_json_path": session.state_json_path,
        "headless": session.headless,
    }


@app.post("/v1/auth/legacy/check")
def check_legacy_login() -> dict[str, Any]:
    runtime = get_runtime_manager()
    payload = _check_legacy_login_session(runtime.settings)
    payload.update(
        {
            "state_json_path": str(_legacy_state_path(runtime.settings)),
            "state_json_exists": _legacy_state_exists(runtime.settings),
        }
    )
    return payload


@app.post("/v1/tasks/crawl")
def create_crawl_task(request: CrawlTaskRequest) -> dict[str, Any]:
    runtime = get_runtime_manager()
    accounts = [item.model_dump() for item in request.accounts]
    if not accounts:
        raise HTTPException(status_code=400, detail="accounts is required")

    with db_conn(runtime.settings) as conn:
        browser_auth_mode = runtime.settings.browser_auth_mode
        session = None
        state_storage = runtime.settings.legacy_state_json if browser_auth_mode == "legacy_state" else None
        headless = runtime.settings.legacy_headless if browser_auth_mode == "legacy_state" else request.headless
        if browser_auth_mode == "legacy_state":
            if not _legacy_state_usable(runtime.settings):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "BLOCKED_AUTH: 请先调用 /v1/auth/legacy/open-login，在打开的浏览器中完成 WEIQ 登录，"
                        "然后调用 /v1/auth/legacy/check 保存 state.json。"
                    ),
                )
        else:
            if not request.login_session_id:
                raise HTTPException(status_code=400, detail="login_session_id is required")
            session = _get_auth_session(conn, request.login_session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="login session not found")
            status, _ = _session_status_for_check(session)
            if status != "authenticated":
                raise HTTPException(status_code=400, detail="login session is not authenticated")
            state_storage = session["state_storage"]

        task_id = str(uuid4())
        output_dir = str(_ensure_dir(Path(runtime.settings.runtime_dir) / "tasks" / task_id))
        input_excel = str(Path(output_dir) / "accounts.xlsx")
        output_excel = str(Path(output_dir) / "weiq_results.xlsx")
        _write_accounts_excel(input_excel, accounts)
        created_at = _iso_now()
        conn.execute(
            """
            INSERT INTO tasks (
                task_id, status, progress, current_account, total_accounts, processed_accounts,
                success_accounts, failed_accounts, skipped_accounts, message, error_code,
                input_excel, output_excel, output_dir, state_storage, headless, login_session_id,
                accounts_json, retry_times, retry_backoff_seconds, resume, created_at, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                TaskStatus.PENDING.value,
                0.0,
                None,
                len(accounts),
                0,
                0,
                0,
                0,
                "任务已创建，等待 worker 执行",
                None,
                input_excel,
                output_excel,
                output_dir,
                state_storage,
                1 if headless else 0,
                request.login_session_id if browser_auth_mode != "legacy_state" else None,
                json.dumps(accounts, ensure_ascii=False),
                request.retry_times,
                request.retry_backoff_seconds,
                1 if request.resume else 0,
                created_at,
                None,
                None,
            ),
        )
        if session is not None and request.login_session_id:
            conn.execute(
                "UPDATE auth_sessions SET task_id = ?, updated_at = ? WHERE session_id = ?",
                (task_id, _iso_now(), request.login_session_id),
            )
    runtime.enqueue(task_id)
    return {"task_id": task_id, "status": TaskStatus.PENDING.value, "progress": 0.0, "message": "任务已创建"}


@app.get("/v1/tasks/{task_id}")
def get_task_status(task_id: str) -> dict[str, Any]:
    runtime = get_runtime_manager()
    with db_conn(runtime.settings) as conn:
        task = _get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
    return _task_response(task)


@app.get("/v1/tasks/{task_id}/export")
def export_task_excel(task_id: str):
    runtime = get_runtime_manager()
    with db_conn(runtime.settings) as conn:
        task = _get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
    output_excel = task.get("output_excel")
    if not output_excel or not Path(output_excel).exists():
        raise HTTPException(status_code=404, detail="export file not found")
    return FileResponse(output_excel, filename=Path(output_excel).name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/v1/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict[str, Any]:
    runtime = get_runtime_manager()
    with db_conn(runtime.settings) as conn:
        task = _get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        if task["status"] in {TaskStatus.SUCCESS.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}:
            return _task_response(task)
        _update_row(
            conn,
            "tasks",
            "task_id",
            task_id,
            status=TaskStatus.CANCELLED.value,
            error_code=ErrorCode.CANCELLED.value,
            message="任务已取消",
            finished_at=_iso_now(),
        )
        task = _get_task(conn, task_id)
    return _task_response(task)
