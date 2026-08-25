import argparse
import hashlib
import json
import os
import random
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

INPUT_EXCEL = "accounts.xlsx"
OUTPUT_EXCEL = "weiq_results.xlsx"
STATE_JSON = "state.json"
STATE_STORE_JSON = "storage_state.json"
PROGRESS_STATE_JSON = "crawl_progress.json"
WEIQ_STARTUP_URLS = ("https://weiq.com/", "https://www.weiq.com/")
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

METRIC_KEYS = [
    "粉丝数",
    "直发CPM",
    "阅读中位数",
    "直发阅读中位数",
    "转发阅读中位数",
    "互动中位数",
    "直发互动中位数",
    "转发互动中位数",
    "发布博文数",
    "转发中位数",
    "评论中位数",
    "点赞中位数",
    "最低阅读量",
    "最高阅读量",
    "阅读量均值",
    "供稿直发",
    "供稿转发",
    "头条文章",
    "点评",
    "原创图文",
    "原创视频",
]
CRITICAL_METRIC_KEYS = ["粉丝数", "直发CPM", "阅读中位数", "发布博文数"]
PROFILE_RESULT_KEYS = ["认证等级"]
RESULT_KEYS = [*METRIC_KEYS, *PROFILE_RESULT_KEYS]
VERIFY_FILL_MAP = {
    ("#FFFFFF", "#F6CA45", "#FFFFFF"): "黄V",
    ("#FFFFFF", "#FF6C00", "#FFFFFF"): "橙V",
    ("#FEFF78", "#CD3620", "#FEFF78"): "金V",
}
EMPTY_METRIC_MARKERS = {
    "",
    "-",
    "--",
    "空",
    "空_无标签",
    "空_无数据",
    "暂无",
    "未收录",
    "未抓取",
    "未获取",
    "等待登录",
    "待登录",
    "登录失效",
}
LOGIN_HINT_KEYWORDS = [
    "请先登录",
    "登录后",
    "立即登录",
    "账号密码",
    "手机号登录",
    "手机验证码",
    "发送验证码",
    "获取验证码",
    "短信验证码",
]
CAPTCHA_HINT_KEYWORDS = ["滑动验证", "安全访问验证", "请输入验证码", "访问过于频繁", "安全验证"]
BROWSER_ERROR_KEYWORDS = [
    "无法访问此网站",
    "响应时间过长",
    "this site can't be reached",
    "err_timed_out",
    "err_connection_timed_out",
    "err_connection_reset",
    "err_name_not_resolved",
]
LOGIN_FORM_SELECTORS = [
    "input[type='password']",
    "input[placeholder*='密码']",
    "input[placeholder*='验证码']",
    "input[placeholder*='手机号']",
    "input[placeholder*='账号']",
    "input[placeholder*='用户名']",
]

RESULT_META_KEYS = ["run_id", "crawl_time", "account_status", "error_code", "error_message"]
WEEKLY_SHEET_NAME = "账号周指标"
POST_TREND_SHEET_NAME = "微博趋势"
POST_TREND_COLUMNS = [
    "run_id", "crawl_time", "账号ID", "uid", "source_post_id", "发布时间", "阅读量", "正文摘要",
    "封面URL", "微博链接", "博文类型", "source_order", "趋势状态", "趋势错误",
]


class TaskStatus:
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    BLOCKED_AUTH = "BLOCKED_AUTH"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AccountStatus:
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class ErrorCode:
    NONE = "NONE"
    INVALID_UID = "INVALID_UID"
    HTTP_BLOCKED = "HTTP_BLOCKED"
    TIMEOUT = "TIMEOUT"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    EMPTY_PAGE = "EMPTY_PAGE"
    NAVIGATION_ERROR = "NAVIGATION_ERROR"
    WRITE_ERROR = "WRITE_ERROR"
    POST_TREND_EMPTY = "POST_TREND_EMPTY"
    POST_TREND_PARSE = "POST_TREND_PARSE"
    CANCELLED = "CANCELLED"


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
    ErrorCode.POST_TREND_EMPTY: "最近20篇博文趋势未返回可用数据",
    ErrorCode.POST_TREND_PARSE: "最近20篇博文趋势解析失败",
    ErrorCode.CANCELLED: "任务被取消",
}


@dataclass
class CrawlConfig:
    input_excel: str = INPUT_EXCEL
    output_excel: str = OUTPUT_EXCEL
    state_json: str = STATE_JSON
    progress_state: str = PROGRESS_STATE_JSON
    output_dir: str = "."
    display: Optional[str] = None
    headless: bool = True
    cooldown_every: int = 50
    cooldown_seconds: int = 180
    wait_min_seconds: int = 2
    wait_max_seconds: int = 4
    goto_timeout_ms: int = 45000
    network_idle_timeout_ms: int = 8000
    retry_times: int = 1
    retry_backoff_seconds: int = 3
    resume: bool = True
    run_id: Optional[str] = None
    state_storage: str = STATE_STORE_JSON


@dataclass
class CrawlHooks:
    on_event: Optional[Callable[[dict[str, Any]], None]] = None
    should_stop: Optional[Callable[[], bool]] = None
    on_auth_required: Optional[Callable[[str, str, Any, Any, str], bool]] = None


@dataclass
class CrawlRunResult:
    run_id: str
    status: str
    total_accounts: int
    processed_accounts: int
    success_accounts: int
    failed_accounts: int
    skipped_accounts: int
    started_at: str
    finished_at: str
    output_excel: str
    error_code: str = ErrorCode.NONE


@dataclass
class AccountProcessResult:
    metrics: dict[str, str] = field(default_factory=dict)
    account_status: str = AccountStatus.FAILED
    error_code: str = ErrorCode.NAVIGATION_ERROR
    error_message: str = ERROR_MESSAGES_ZH[ErrorCode.NAVIGATION_ERROR]
    post_trend_rows: list[dict[str, Any]] = field(default_factory=list)
    post_trend_status: str = "not_collected"
    post_trend_error: str | None = None


class StateStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "last_run_id": None, "runs": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "last_run_id": None, "runs": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, self.path)

    def ensure_run(self, run_id: str) -> None:
        runs = self.data.setdefault("runs", {})
        if run_id not in runs:
            runs[run_id] = {
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "processed": {},
            }
        self.data["last_run_id"] = run_id
        self._save()

    def get_last_run_id(self) -> Optional[str]:
        return self.data.get("last_run_id")

    def is_processed(self, run_id: str, uid: str) -> bool:
        run = self.data.get("runs", {}).get(run_id, {})
        return uid in run.get("processed", {})

    def mark_processed(self, run_id: str, uid: str, status: str, error_code: str) -> None:
        run = self.data.setdefault("runs", {}).setdefault(
            run_id,
            {"created_at": now_iso(), "updated_at": now_iso(), "processed": {}},
        )
        run["processed"][uid] = {
            "status": status,
            "error_code": error_code,
            "updated_at": now_iso(),
        }
        run["updated_at"] = now_iso()
        self.data["last_run_id"] = run_id
        self._save()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def emit_event(hooks: CrawlHooks, event: dict[str, Any]) -> None:
    if hooks.on_event:
        hooks.on_event(event)


def should_stop(hooks: CrawlHooks) -> bool:
    if hooks.should_stop:
        return hooks.should_stop()
    return False


def _normalize_metric_text(value: Any) -> str:
    return str(value or "").strip().replace("\u3000", "").replace(" ", "").lower()


def _build_result_payload(default_value: str) -> dict[str, str]:
    return {key: default_value for key in RESULT_KEYS}


def _normalize_hex_color(raw: Any) -> str:
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    if text.startswith("#") and len(text) == 4:
        return "#" + "".join(ch * 2 for ch in text[1:])
    return text


def _extract_verification_probe(page) -> dict[str, Any]:
    js = r"""
    () => {
      const out = {
        has_profile_card: false,
        has_name_row: false,
        has_verify_icon: false,
        has_verify_text: false,
        has_unverified_hint: false,
        verify_text_value: '',
        path_fills: [],
      };

      const all = Array.from(document.querySelectorAll('*'));
      const card = all.find(el => {
        const t = (el.innerText || '').trim();
        return t.includes('UID') && t.includes('粉丝数') && t.includes('博文总数');
      });
      if (!card) return out;

      out.has_profile_card = true;
      const topLines = (card.innerText || '').split(/\n+/).map(s => s.trim()).filter(Boolean).slice(0, 20);
      const normalize = (s) => String(s || '').replace(/\s+/g, '');
      const isNegative = (s) => {
        const v = normalize(s).toLowerCase();
        if (!v) return false;
        if (/^[-—–~～_=·*xX\/]+$/.test(v)) return true;
        if (['无', '暂无', '未认证', 'none', 'null', 'na', 'n/a'].includes(v)) return true;
        return false;
      };

      let verifyTextValue = '';
      for (const line of topLines) {
        if (!line.includes('认证信息')) continue;
        const m = line.match(/认证信息\s*[：:]?\s*(.*)$/);
        const tail = m ? (m[1] || '') : line.split('认证信息').slice(1).join('');
        const cleaned = String(tail || '').trim();
        if (cleaned && !verifyTextValue) verifyTextValue = cleaned;
      }
      out.verify_text_value = verifyTextValue;
      out.has_verify_text = Boolean(verifyTextValue && !isNegative(verifyTextValue));
      out.has_unverified_hint = Boolean(verifyTextValue) && isNegative(verifyTextValue);

      const nameRow = card.querySelector('.user-name-text.pointer');
      if (!nameRow) return out;

      out.has_name_row = true;
      const svg = nameRow.querySelector('svg.gl-icon-default.icon.v.ml4');
      if (!svg) return out;

      out.has_verify_icon = true;
      out.path_fills = Array.from(svg.querySelectorAll('path'))
        .map(p => p.getAttribute('fill') || '')
        .filter(Boolean);
      return out;
    }
    """
    probe = page.evaluate(js)
    return probe if isinstance(probe, dict) else {}


def _resolve_verification_level_from_probe(probe: dict[str, Any]) -> str:
    text_value = str(probe.get("verify_text_value") or "").strip()
    normalized_text = text_value.replace("认证信息", "").replace("：", ":").replace(":", "").strip()
    if "金V" in normalized_text:
        return "金V"
    if "橙V" in normalized_text:
        return "橙V"
    if "黄V" in normalized_text:
        return "黄V"
    if probe.get("has_profile_card") and probe.get("has_name_row") and not probe.get("has_verify_icon"):
        if probe.get("has_unverified_hint") or not probe.get("has_verify_text"):
            return "无认证"

    fills = tuple(_normalize_hex_color(item) for item in probe.get("path_fills") or [] if _normalize_hex_color(item))
    if fills in VERIFY_FILL_MAP:
        return VERIFY_FILL_MAP[fills]
    return "unknown"


def extract_verification_level(page) -> str:
    try:
        probe = _extract_verification_probe(page)
    except Exception:
        return "unknown"
    return _resolve_verification_level_from_probe(probe)


def _is_empty_metric_value(value: Any) -> bool:
    return _normalize_metric_text(value) in EMPTY_METRIC_MARKERS


def _parse_metric_number(value: Any) -> float:
    text = str(value or "").strip().replace(",", "").replace("¥", "").replace("元", "")
    if not text or _is_empty_metric_value(text):
        return 0.0
    multiplier = 1.0
    if text.endswith("万") or text.lower().endswith("w"):
        multiplier = 10000.0
        text = text[:-1]
    elif text.lower().endswith("k"):
        multiplier = 1000.0
        text = text[:-1]
    elif text.endswith("亿"):
        multiplier = 100000000.0
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return 0.0


def _count_effective_metrics(extracted_data: dict[str, str], keys: list[str]) -> int:
    count = 0
    for key in keys:
        value = extracted_data.get(key)
        if _is_empty_metric_value(value):
            continue
        normalized = str(value or "").strip().replace(",", "").replace("¥", "").replace("￥", "").replace("元", "")
        if normalized.endswith(("万", "亿", "w", "W", "k", "K")):
            normalized = normalized[:-1]
        try:
            float(normalized)
            count += 1
        except ValueError:
            continue
    return count


def _safe_body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=2000) or ""
    except Exception:
        return ""


def _page_has_visible_login_form(page) -> bool:
    for selector in LOGIN_FORM_SELECTORS:
        try:
            locator = page.locator(selector)
            count = min(locator.count(), 4)
            for index in range(count):
                if locator.nth(index).is_visible():
                    return True
        except Exception:
            continue
    return False


def infer_post_extraction_issue(
    *,
    page_url: str,
    page_text: str,
    has_login_form: bool,
    extracted_data: dict[str, str],
) -> str:
    merged_text = f"{page_url}\n{page_text}".lower()
    if any(keyword.lower() in merged_text for keyword in CAPTCHA_HINT_KEYWORDS):
        return ErrorCode.CAPTCHA_REQUIRED

    core_valid_count = _count_effective_metrics(extracted_data, CRITICAL_METRIC_KEYS)
    if core_valid_count >= 2:
        return ErrorCode.NONE

    if has_login_form or any(keyword.lower() in merged_text for keyword in LOGIN_HINT_KEYWORDS):
        return ErrorCode.AUTH_REQUIRED

    overall_valid_count = _count_effective_metrics(extracted_data, METRIC_KEYS)
    if overall_valid_count == 0:
        return ErrorCode.EMPTY_PAGE

    return ErrorCode.EMPTY_PAGE


def default_auth_handler(reason_code: str, page_url: str, page=None, context=None, state_file: str = STATE_JSON) -> bool:
    print(f"\n[风控警告] 触发 {reason_code}，当前页面: {page_url}")
    print(">>>>> 请立即在浏览器中手动登录或验证，处理完成后回到终端继续 <<<<<")
    input("====> 处理完毕后按回车继续：")
    if context is not None:
        try:
            context.storage_state(path=state_file)
        except Exception:
            pass
    print("[恢复] 已收到继续信号，准备重试当前账号。\n")
    time.sleep(2)
    return True


def resolve_output_path(output_dir: str, output_excel: str) -> str:
    output_dir = output_dir or "."
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(output_excel)
    if out_path.is_absolute():
        return str(out_path)
    return str((out_dir / out_path).resolve())


def has_usable_storage_state(state_file: str) -> bool:
    path = Path(state_file)
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    cookies = payload.get("cookies")
    origins = payload.get("origins")
    return bool(cookies or origins)


def select_startup_state_file(state_storage: str, state_json: str) -> str | None:
    for candidate in (state_storage, state_json):
        if candidate and has_usable_storage_state(candidate):
            return candidate
    return None


def is_browser_session_closed_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return any(
        marker in text
        for marker in (
            "target page, context or browser has been closed",
            "browser has been closed",
            "context has been closed",
            "page has been closed",
        )
    )


def is_navigation_timeout_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return any(
        marker in text
        for marker in (
            "err_timed_out",
            "timeout",
            "timed out",
            "err_connection_timed_out",
            "err_name_not_resolved",
            "err_connection_reset",
            "err_network_changed",
        )
    )


def _browser_is_alive(browser) -> bool:
    if browser is None:
        return False
    try:
        return bool(browser.is_connected())
    except Exception:
        return False


def _context_is_alive(context) -> bool:
    if context is None:
        return False
    try:
        context.pages
        return True
    except Exception:
        return False


def _page_is_alive(page) -> bool:
    if page is None:
        return False
    try:
        return not page.is_closed()
    except Exception:
        return False


def persist_login_state(context, *, state_json: str, state_storage: str) -> None:
    saved = False
    for path in (state_json, state_storage):
        target = str(path or "").strip()
        if not target:
            continue
        try:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=target)
            saved = True
        except Exception:
            continue
    if not saved:
        raise RuntimeError("未能保存任何登录态文件")


def get_playwright_launch_kwargs(*, headless: bool = True) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"headless": headless}
    server = str(os.getenv("WEIQ_PROXY_SERVER") or "").strip()
    use_system_proxy = str(os.getenv("WEIQ_USE_SYSTEM_PROXY") or "").strip().lower() in {"1", "true", "yes", "on"}
    if not server:
        if not use_system_proxy:
            kwargs["args"] = ["--proxy-server=direct://", "--proxy-bypass-list=*"]
        return kwargs
    proxy: dict[str, str] = {"server": server}
    for env_name, key in (
        ("WEIQ_PROXY_USERNAME", "username"),
        ("WEIQ_PROXY_PASSWORD", "password"),
        ("WEIQ_PROXY_BYPASS", "bypass"),
    ):
        value = str(os.getenv(env_name) or "").strip()
        if value:
            proxy[key] = value
    kwargs["proxy"] = proxy
    return kwargs


def get_browser_context_kwargs(state_file: str | None) -> dict[str, Any]:
    context_kwargs: dict[str, Any] = {
        "user_agent": str(os.getenv("WEIQ_BROWSER_USER_AGENT") or DEFAULT_BROWSER_USER_AGENT).strip()
        or DEFAULT_BROWSER_USER_AGENT
    }
    if state_file and os.path.exists(state_file):
        context_kwargs["storage_state"] = state_file
    return context_kwargs


def load_accounts(input_excel: str) -> pd.DataFrame:
    df = pd.read_excel(input_excel)
    if "uid" not in df.columns:
        raise ValueError("输入表缺少 uid 列")
    if "账号ID" not in df.columns:
        df["账号ID"] = "未命名账号"
    return df


def to_atomic_excel(path: str, df: pd.DataFrame, post_trend_df: pd.DataFrame | None = None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(target.parent), suffix=".xlsx", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        trends = post_trend_df if post_trend_df is not None else pd.DataFrame(columns=POST_TREND_COLUMNS)
        trends = trends.reindex(columns=POST_TREND_COLUMNS)
        with pd.ExcelWriter(tmp_path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name=WEEKLY_SHEET_NAME, index=False)
            trends.to_excel(writer, sheet_name=POST_TREND_SHEET_NAME, index=False)
        os.replace(tmp_path, target)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _read_existing_output(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not path.exists():
        return pd.DataFrame(), pd.DataFrame(columns=POST_TREND_COLUMNS)
    with pd.ExcelFile(path) as workbook:
        weekly_sheet = WEEKLY_SHEET_NAME if WEEKLY_SHEET_NAME in workbook.sheet_names else workbook.sheet_names[0]
        weekly = pd.read_excel(workbook, sheet_name=weekly_sheet)
        trends = (
            pd.read_excel(workbook, sheet_name=POST_TREND_SHEET_NAME)
            if POST_TREND_SHEET_NAME in workbook.sheet_names
            else pd.DataFrame(columns=POST_TREND_COLUMNS)
        )
    return weekly, trends


def safe_append_row(path: str, row_dict: dict[str, Any], post_trend_rows: list[dict[str, Any]] | None = None) -> None:
    row_df = pd.DataFrame([row_dict])
    target = Path(path)
    old_df, old_trends = _read_existing_output(target)
    all_df = pd.concat([old_df, row_df], ignore_index=True, sort=False) if not old_df.empty else row_df
    trend_df = pd.DataFrame(post_trend_rows or [], columns=POST_TREND_COLUMNS)
    all_trends = pd.concat([old_trends, trend_df], ignore_index=True, sort=False)
    to_atomic_excel(str(target), all_df, all_trends)


def init_browser(playwright_obj, state_file: str | None, headless: bool, *, display: Optional[str] = None) -> dict[str, Any]:
    print("[初始化] 正在启动浏览器...")
    launch_kwargs = get_playwright_launch_kwargs(headless=headless)
    if display:
        launch_kwargs["env"] = {**os.environ, "DISPLAY": display}
    browser = playwright_obj.chromium.launch(**launch_kwargs)

    context_kwargs = get_browser_context_kwargs(state_file)
    if "storage_state" in context_kwargs:
        print(f"[初始化] 检测到凭证文件 {state_file}，尝试恢复会话。")
    else:
        print("[警告] 未检测到凭证文件，将以未登录状态启动。")
    context = browser.new_context(**context_kwargs)

    page = context.new_page()
    return {"browser": browser, "context": context, "page": page, "state_file": state_file}


def close_browser_session(session: dict[str, Any]) -> None:
    context = session.get("context")
    browser = session.get("browser")
    if context is not None:
        try:
            context.close()
        except Exception:
            pass
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass


def recreate_browser_session(
    playwright_obj,
    session: dict[str, Any],
    config: CrawlConfig,
    *,
    preferred_state_file: str | None = None,
) -> dict[str, Any]:
    close_browser_session(session)
    startup_state_file = preferred_state_file or session.get("state_file")
    new_session = init_browser(playwright_obj, startup_state_file, config.headless, display=config.display)
    session.clear()
    session.update(new_session)
    return session


def ensure_browser_session(
    playwright_obj,
    session: dict[str, Any],
    config: CrawlConfig,
    *,
    preferred_state_file: str | None = None,
) -> dict[str, Any]:
    if _browser_is_alive(session.get("browser")) and _context_is_alive(session.get("context")) and _page_is_alive(session.get("page")):
        return session
    print("[恢复] 浏览器会话失效，正在自动恢复...")
    return recreate_browser_session(playwright_obj, session, config, preferred_state_file=preferred_state_file)


def goto_with_recovery(
    playwright_obj,
    session: dict[str, Any],
    url: str,
    config: CrawlConfig,
    *,
    preferred_state_file: str | None = None,
    wait_until: str = "domcontentloaded",
):
    ensure_browser_session(playwright_obj, session, config, preferred_state_file=preferred_state_file)
    try:
        return session["page"].goto(url, timeout=config.goto_timeout_ms, wait_until=wait_until)
    except Exception as exc:
        if not is_browser_session_closed_error(exc):
            raise
        print("[恢复] 检测到页面句柄已失效，正在重建浏览器页面...")
        recreate_browser_session(playwright_obj, session, config, preferred_state_file=preferred_state_file)
        return session["page"].goto(url, timeout=config.goto_timeout_ms, wait_until=wait_until)


def open_weiq_startup_page(
    playwright_obj,
    session: dict[str, Any],
    config: CrawlConfig,
    *,
    preferred_state_file: str | None = None,
) -> tuple[bool, str, str]:
    last_error_code = ErrorCode.NAVIGATION_ERROR
    last_url = WEIQ_STARTUP_URLS[0]
    for login_url in WEIQ_STARTUP_URLS:
        last_url = login_url
        try:
            goto_with_recovery(
                playwright_obj,
                session,
                login_url,
                config,
                preferred_state_file=preferred_state_file,
                wait_until="domcontentloaded",
            )
            return True, login_url, ErrorCode.NONE
        except Exception as exc:
            if is_navigation_timeout_error(exc):
                print(f"[初始化] 打开 {login_url} 超时，尝试下一个入口。")
            else:
                print(f"[初始化] 打开 {login_url} 失败: {exc}")
            last_error_code = ErrorCode.NAVIGATION_ERROR
            continue
    return False, last_url, last_error_code


def is_browser_error_page(page) -> bool:
    try:
        current_url = str(getattr(page, "url", "") or "").strip().lower()
    except Exception:
        current_url = ""
    if current_url.startswith("chrome-error://"):
        return True
    page_text = _safe_body_text(page).lower()
    return any(keyword in page_text for keyword in BROWSER_ERROR_KEYWORDS)


def verify_homepage_login_state(page) -> tuple[bool, str]:
    if not _page_is_alive(page):
        return False, ErrorCode.NAVIGATION_ERROR
    current_url = str(getattr(page, "url", "") or "").strip().lower()
    if current_url.startswith("about:blank"):
        return False, ErrorCode.NAVIGATION_ERROR
    if is_browser_error_page(page):
        return False, ErrorCode.NAVIGATION_ERROR
    needs_auth, reason_code = detect_auth_or_challenge(page)
    if needs_auth:
        return False, reason_code
    page_text = _safe_body_text(page)
    if _page_has_visible_login_form(page) and any(keyword.lower() in page_text.lower() for keyword in LOGIN_HINT_KEYWORDS):
        return False, ErrorCode.AUTH_REQUIRED
    return True, ErrorCode.NONE


def ensure_authenticated_session(
    playwright_obj,
    session: dict[str, Any],
    config: CrawlConfig,
    hooks: CrawlHooks,
    *,
    total_accounts: int,
) -> tuple[dict[str, Any], bool, str]:
    login_url = WEIQ_STARTUP_URLS[0]
    auth_handler = hooks.on_auth_required or default_auth_handler
    startup_state_file = select_startup_state_file(config.state_storage, config.state_json)
    if startup_state_file != session.get("state_file"):
        session["state_file"] = startup_state_file
    print("[初始化] 正在校验长期登录态...")
    opened, login_url, startup_error = open_weiq_startup_page(
        playwright_obj,
        session,
        config,
        preferred_state_file=startup_state_file,
    )
    if not opened:
        print("[初始化] WEIQ 首页当前不可达，请检查网络或稍后重试。")
        return session, False, startup_error

    verified, reason_code = verify_homepage_login_state(session["page"])
    if verified:
        persist_login_state(session["context"], state_json=config.state_json, state_storage=config.state_storage)
        session["state_file"] = config.state_storage
        print("[初始化] 登录验证通过，开始采集。")
        return session, True, ErrorCode.NONE

    if reason_code == ErrorCode.NAVIGATION_ERROR:
        print("[初始化] WEIQ 首页当前不可达，请检查网络或稍后重试。")
        return session, False, ErrorCode.NAVIGATION_ERROR

    print("[初始化] 长期登录态失效，已打开 WEIQ，请完成登录。")
    emit_event(
        hooks,
        {
            "type": "auth_required",
            "reason_code": reason_code,
            "page_url": str(getattr(session["page"], "url", "") or login_url),
            "current_index": 0,
            "total_accounts": total_accounts,
        },
    )
    if not auth_handler(reason_code, str(getattr(session["page"], "url", "") or login_url), session["page"], session["context"], config.state_json):
        return session, False, reason_code

    opened, login_url, startup_error = open_weiq_startup_page(
        playwright_obj,
        session,
        config,
        preferred_state_file=config.state_json,
    )
    if not opened:
        print("[初始化] 登录后重新校验 WEIQ 首页失败，请检查网络。")
        return session, False, startup_error

    verified, reason_code = verify_homepage_login_state(session["page"])
    if not verified:
        print(f"[初始化] 登录验证仍未通过，原因={reason_code}。")
        return session, False, reason_code

    persist_login_state(session["context"], state_json=config.state_json, state_storage=config.state_storage)
    session["state_file"] = config.state_storage
    print("[初始化] 登录验证通过，开始采集。")
    return session, True, ErrorCode.NONE


def recover_account_session_after_auth(
    playwright_obj,
    session: dict[str, Any],
    config: CrawlConfig,
    hooks: CrawlHooks,
    *,
    issue_code: str,
    page_url: str,
    current_idx: int,
    total_accounts: int,
    progress: str,
) -> tuple[bool, str]:
    page = session["page"]
    context = session["context"]
    auth_handler = hooks.on_auth_required or default_auth_handler
    emit_event(
        hooks,
        {
            "type": "auth_required",
            "reason_code": issue_code,
            "page_url": page_url,
            "current_index": current_idx,
            "total_accounts": total_accounts,
        },
    )
    if not auth_handler(issue_code, page_url, page, context, config.state_storage):
        return False, issue_code

    session["state_file"] = config.state_storage if has_usable_storage_state(config.state_storage) else config.state_json
    opened, _, startup_error = open_weiq_startup_page(
        playwright_obj,
        session,
        config,
        preferred_state_file=session.get("state_file"),
    )
    if not opened:
        print(f"{progress} [恢复失败] 登录后无法重新打开 WEIQ 首页。")
        return False, startup_error

    verified, verify_code = verify_homepage_login_state(session["page"])
    if not verified:
        print(f"{progress} [恢复失败] 登录验证未通过，原因={verify_code}。")
        return False, verify_code

    try:
        persist_login_state(session["context"], state_json=config.state_json, state_storage=config.state_storage)
    except Exception:
        pass
    session["state_file"] = config.state_storage
    print(f"{progress} [恢复] 登录验证通过，准备重试当前账号...")
    if config.retry_backoff_seconds > 0:
        time.sleep(config.retry_backoff_seconds)
    return True, ErrorCode.NONE


def extract_metrics(page) -> dict[str, str]:
    results = {k: "空" for k in METRIC_KEYS}
    js_extract_logic = r"""
    (keyword) => {
        const target = keyword.toUpperCase();
        let elements = Array.from(document.querySelectorAll('*'))
            .filter(el => el.childElementCount === 0 && el.textContent.trim().toUpperCase() === target);

        if (elements.length === 0) {
            elements = Array.from(document.querySelectorAll('*'))
                .filter(el => el.childElementCount === 0 && el.textContent.toUpperCase().includes(target));
        }

        if (elements.length === 0) {
            const bodyInline = new RegExp(target.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*[：:]?\\s*([¥￥]?[\\d,.]+(?:[万亿wWkK])?)').exec(document.body.innerText || '');
            return bodyInline && bodyInline[1] ? bodyInline[1] : "空_无标签";
        }

        const labelEl = elements[0];

        let parent = labelEl.parentElement;
        for (let i = 0; i < 4; i++) {
            if (parent) {
                let textContent = parent.innerText || '';
                let lines = textContent.split(/[\n\r]+/).map(s => s.trim()).filter(Boolean);
                let idx = lines.findIndex(s => s.toUpperCase() === target);

                if (idx !== -1 && idx + 1 < lines.length) {
                    let candidate = lines[idx + 1];
                    if (/^[\d,.]+[万wWkK]?$/.test(candidate) || candidate === '-' || candidate.includes('%')) {
                        return candidate;
                    }
                }
                const inline = new RegExp(target.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*[：:]?\\s*([¥￥]?[\\d,.]+(?:[万亿wWkK])?)').exec(textContent);
                if (inline && inline[1]) return inline[1];
            }
            parent = parent ? parent.parentElement : null;
        }

        const bodyInline = new RegExp(target.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*[：:]?\\s*([¥￥]?[\\d,.]+(?:[万亿wWkK])?)').exec(document.body.innerText || '');
        if (bodyInline && bodyInline[1]) return bodyInline[1];

        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
        let currentNode = walker.nextNode();
        let found = false;
        while (currentNode) {
            if (currentNode.parentElement === labelEl || currentNode.nodeValue.toUpperCase().includes(target)) {
                found = true;
                break;
            }
            currentNode = walker.nextNode();
        }

        if (found) {
            currentNode = walker.nextNode();
            let attempt = 0;
            while(currentNode && attempt < 15) {
                let txt = currentNode.nodeValue.trim();
                if (txt && !['¥', '￥', ':', '：', '-', '/'].includes(txt) && txt.toUpperCase() !== target) {
                    return txt;
                }
                currentNode = walker.nextNode();
                attempt++;
            }
        }
        return "空_无数据";
    }
    """

    for key in METRIC_KEYS:
        try:
            val = page.evaluate(js_extract_logic, key)
            if val:
                results[key] = val
        except Exception:
            continue

    return results


def detect_auth_or_challenge(page) -> tuple[bool, str]:
    current_url = page.url.lower()
    if "login" in current_url or "passport" in current_url:
        return True, ErrorCode.AUTH_REQUIRED

    try:
        page_text = _safe_body_text(page)
        if any(keyword in page_text for keyword in CAPTCHA_HINT_KEYWORDS):
            return True, ErrorCode.CAPTCHA_REQUIRED
        if _page_has_visible_login_form(page) and any(keyword in page_text for keyword in LOGIN_HINT_KEYWORDS):
            return True, ErrorCode.AUTH_REQUIRED
    except Exception:
        pass

    return False, ErrorCode.NONE


def infer_blocked_response_issue(page) -> str:
    needs_auth, reason_code = detect_auth_or_challenge(page)
    if needs_auth:
        return reason_code

    page_text = _safe_body_text(page)
    has_login_form = _page_has_visible_login_form(page)
    inferred = infer_post_extraction_issue(
        page_url=page.url,
        page_text=page_text,
        has_login_form=has_login_form,
        extracted_data={key: "空" for key in METRIC_KEYS},
    )
    if inferred in {ErrorCode.AUTH_REQUIRED, ErrorCode.CAPTCHA_REQUIRED}:
        return inferred
    return ErrorCode.HTTP_BLOCKED


def perform_lazy_scroll(page) -> None:
    page.evaluate(
        """
        () => {
            return new Promise((resolve) => {
                let totalHeight = 0;
                const distance = 500;
                const timer = setInterval(() => {
                    const scrollHeight = document.body.scrollHeight;
                    window.scrollBy(0, distance);
                    totalHeight += distance;
                    if (totalHeight >= scrollHeight) {
                        clearInterval(timer);
                        window.scrollTo(0, 0);
                        resolve();
                    }
                }, 250);
            });
        }
        """
    )


def activate_post_trend_section(page) -> bool:
    """Open WEIQ's content-performance tab so its lazy trend request is issued."""
    selectors = (
        "xpath=//li[normalize-space(.)='内容表现']",
        "text=内容表现",
        "xpath=//*[normalize-space(text())='内容表现']",
        "xpath=//*[contains(normalize-space(.),'内容表现')]",
    )
    for selector in selectors:
        try:
            locator = page.locator(selector)
            locator.first.wait_for(state="visible", timeout=10000)
            for index in range(min(locator.count(), 8)):
                candidate = locator.nth(index)
                if not candidate.is_visible():
                    continue
                candidate.click(timeout=3000)
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    pass
                return True
        except Exception:
            continue
    # Some WEIQ builds render the tab through a wrapper that swallows the
    # locator click. Dispatch a real DOM click as a compatibility fallback.
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const nodes = [...document.querySelectorAll('*')].filter((node) =>
                    (node.textContent || '').trim() === '内容表现' && node.getClientRects().length
                  );
                  const target = nodes.find((node) => node.closest('button,[role="tab"],a')) || nodes[0];
                  if (!target) return false;
                  (target.closest('button,[role="tab"],a') || target).click();
                  return true;
                }
                """
            )
        )
    except Exception:
        return False


def _post_trend_row_from_item(
    item: dict[str, Any],
    *,
    uid: str,
    account_id: str,
    run_id: str,
    crawl_time: str,
    order: int,
) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    published_at = str(item.get("time") or "").strip()
    content = " ".join(str(item.get("text") or "").split()).strip()[:500]
    source_id = str(item.get("id") or item.get("mid") or item.get("mblogid") or "").strip()
    try:
        reads = int(float(str(item.get("value") or "0").replace(",", "")))
    except (TypeError, ValueError):
        reads = 0
    if not published_at or reads < 0:
        return None
    if not source_id:
        stable = f"{uid}|{published_at}|{content}|{reads}".encode("utf-8")
        source_id = f"fallback-{hashlib.sha256(stable).hexdigest()[:32]}"
    raw_type = item.get("post_type", item.get("weibo_type", item.get("content_type", item.get("type"))))
    if item.get("is_repost") is True or item.get("is_forward") is True or item.get("repost") is True or item.get("forward") is True:
        raw_type = "repost"
    if item.get("is_original") is True:
        raw_type = "direct"
    type_text = str(raw_type or "").strip().lower()
    if type_text in {"direct", "original", "原创", "直发", "原发", "1", "true"}:
        post_type = "direct"
    elif type_text in {"repost", "forward", "转发", "转发微博", "转载", "2"}:
        post_type = "repost"
    else:
        post_type = "unknown"
    post_url = None
    for field in ("url", "link", "weibo_url", "weiboLink", "share_url", "detail_url", "href", "name"):
        candidate = str(item.get(field) or "").strip()
        if candidate.startswith(("https://", "http://")):
            post_url = candidate
            break
    cover = ""
    for field in ("pic", "pic_url", "cover", "cover_url", "thumbnail", "image"):
        candidate = str(item.get(field) or "").strip()
        if candidate.startswith(("https://", "http://")):
            cover = candidate
            break
    return {
        "run_id": run_id,
        "crawl_time": crawl_time,
        "账号ID": account_id,
        "uid": uid,
        "source_post_id": source_id,
        "发布时间": published_at,
        "阅读量": reads,
        "正文摘要": content,
        "封面URL": cover or None,
        "微博链接": post_url,
        "博文类型": post_type,
        "source_order": order,
        "趋势状态": "success",
        "趋势错误": None,
    }


def extract_post_trend_rows(
    payloads: list[dict[str, Any]],
    *,
    uid: str,
    account_id: str,
    run_id: str,
    crawl_time: str,
) -> tuple[list[dict[str, Any]], str, str | None]:
    """Parse the confirmed WEIQ getUserBlogList contract; no generic JSON scanning."""
    for payload in payloads:
        data = payload.get("data") if isinstance(payload, dict) else None
        reads = data.get("reads") if isinstance(data, dict) else None
        items = reads.get("list") if isinstance(reads, dict) else None
        if not isinstance(items, list):
            continue
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            row = _post_trend_row_from_item(
                item,
                uid=uid,
                account_id=account_id,
                run_id=run_id,
                crawl_time=crawl_time,
                order=len(rows),
            )
            if row is None or row["source_post_id"] in seen:
                continue
            seen.add(row["source_post_id"])
            rows.append(row)
            if len(rows) >= 20:
                break
        if rows:
            return rows, "success", None
        return [], "empty", ERROR_MESSAGES_ZH[ErrorCode.POST_TREND_EMPTY]
    return [], "failed", ERROR_MESSAGES_ZH[ErrorCode.POST_TREND_PARSE]


def extract_post_trend_rows_from_echarts(
    page,
    *,
    uid: str,
    account_id: str,
    run_id: str,
    crawl_time: str,
) -> list[dict[str, Any]]:
    """Compatibility fallback for an already-rendered WEIQ chart, without guessing unrelated payloads."""
    try:
        points = page.evaluate(
            """
            () => {
              if (!window.echarts?.getInstanceByDom) return [];
              for (const node of document.querySelectorAll('[_echarts_instance_]')) {
                let container = node;
                let isPostTrend = false;
                for (let depth = 0; depth < 5 && container; depth += 1, container = container.parentElement) {
                  if ((container.innerText || '').includes('最近20篇博文趋势')) { isPostTrend = true; break; }
                }
                if (!isPostTrend) continue;
                const option = window.echarts.getInstanceByDom(node)?.getOption?.();
                const xAxis = Array.isArray(option?.xAxis) ? option.xAxis[0] : option?.xAxis;
                const labels = xAxis?.data;
                const series = Array.isArray(option?.series) ? option.series : [];
                const reads = series.find((item) => item?.type === 'bar')?.data;
                if (Array.isArray(labels) && labels.length && Array.isArray(reads) && labels.length === reads.length) {
                  return labels.map((publishedAt, index) => ({ time: publishedAt, value: reads[index] }));
                }
              }
              return [];
            }
            """
        )
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for item in points if isinstance(points, list) else []:
        row = _post_trend_row_from_item(
            item,
            uid=uid,
            account_id=account_id,
            run_id=run_id,
            crawl_time=crawl_time,
            order=len(rows),
        )
        if row is not None:
            rows.append(row)
    return rows[:20]


def process_account_url(
    playwright_obj,
    session: dict[str, Any],
    account_id: str,
    url: str,
    current_idx: int,
    total_accounts: int,
    config: CrawlConfig,
    hooks: CrawlHooks,
    uid: str | None = None,
) -> AccountProcessResult:
    progress = f"[{current_idx}/{total_accounts}]"
    print(f"\n{progress} ----------------------------------------------------")
    print(f"{progress} [ID: {account_id}] 正在访问页面...")

    attempt = 1
    auth_recovery_count = 0
    max_auth_recoveries = 3
    while attempt <= config.retry_times:
        ensure_browser_session(playwright_obj, session, config, preferred_state_file=session.get("state_file"))
        page = session["page"]
        context = session["context"]
        trend_payloads: list[dict[str, Any]] = []

        def capture_post_trend_response(response) -> None:
            if "getUserBlogList" not in response.url or response.request.method != "POST":
                return
            try:
                payload = response.json()
            except Exception:
                return
            if isinstance(payload, dict):
                trend_payloads.append(payload)

        listener_attached = hasattr(page, "on")
        listener_page = page
        if listener_attached:
            page.on("response", capture_post_trend_response)
        if should_stop(hooks):
            if listener_attached:
                try:
                    listener_page.remove_listener("response", capture_post_trend_response)
                except Exception:
                    pass
            return AccountProcessResult(
                metrics=_build_result_payload("取消"),
                account_status=AccountStatus.CANCELLED,
                error_code=ErrorCode.CANCELLED,
                error_message=ERROR_MESSAGES_ZH[ErrorCode.CANCELLED],
            )

        try:
            response = goto_with_recovery(
                playwright_obj,
                session,
                url,
                config,
                preferred_state_file=session.get("state_file"),
                wait_until="domcontentloaded",
            )
            page = session["page"]
            context = session["context"]
            if response is None or response.status >= 400:
                msg = f"状态码异常: {response.status if response else 'Null'}"
                print(f"{progress} ❌ {msg}")
                issue_code = infer_blocked_response_issue(page)
                if issue_code in {ErrorCode.AUTH_REQUIRED, ErrorCode.CAPTCHA_REQUIRED}:
                    if auth_recovery_count < max_auth_recoveries:
                        recovered, recovery_code = recover_account_session_after_auth(
                            playwright_obj,
                            session,
                            config,
                            hooks,
                            issue_code=issue_code,
                            page_url=page.url,
                            current_idx=current_idx,
                            total_accounts=total_accounts,
                            progress=progress,
                        )
                        if recovered:
                            auth_recovery_count += 1
                            continue
                        issue_code = recovery_code
                    return AccountProcessResult(
                        metrics=_build_result_payload("等待登录"),
                        account_status=AccountStatus.FAILED,
                        error_code=issue_code,
                        error_message=ERROR_MESSAGES_ZH[issue_code],
                    )
                return AccountProcessResult(
                    metrics=_build_result_payload("异常_阻断"),
                    account_status=AccountStatus.FAILED,
                    error_code=ErrorCode.HTTP_BLOCKED,
                    error_message=ERROR_MESSAGES_ZH[ErrorCode.HTTP_BLOCKED],
                )

            sys.stdout.write(f"\r{progress} 页面抵达，执行滚动加载...")
            sys.stdout.flush()
            trend_tab_opened = activate_post_trend_section(page)
            if trend_tab_opened:
                print(f"\n{progress} 已打开内容表现，等待微博趋势数据...")
                try:
                    page.wait_for_function(
                        "document.body && /最近20篇博文趋势[\\s\\S]*最低阅读量\\s*[¥￥]?[\\d,.]+/.test(document.body.innerText)",
                        timeout=10000,
                    )
                except Exception:
                    pass
            else:
                print(f"\n{progress} 未定位到内容表现页签，将尝试读取已加载趋势数据...")
            perform_lazy_scroll(page)
            print("")

            try:
                page.wait_for_load_state("networkidle", timeout=config.network_idle_timeout_ms)
            except Exception:
                pass

            wait_seconds = random.randint(config.wait_min_seconds, config.wait_max_seconds)
            for remaining in range(wait_seconds, 0, -1):
                if should_stop(hooks):
                    return AccountProcessResult(
                        metrics=_build_result_payload("取消"),
                        account_status=AccountStatus.CANCELLED,
                        error_code=ErrorCode.CANCELLED,
                        error_message=ERROR_MESSAGES_ZH[ErrorCode.CANCELLED],
                    )
                sys.stdout.write(f"\r{progress} 数据装配等待中: {remaining} 秒...")
                sys.stdout.flush()
                time.sleep(1)
            print("")
            needs_auth, reason_code = detect_auth_or_challenge(page)
            if needs_auth:
                if auth_recovery_count < max_auth_recoveries:
                    recovered, recovery_code = recover_account_session_after_auth(
                        playwright_obj,
                        session,
                        config,
                        hooks,
                        issue_code=reason_code,
                        page_url=page.url,
                        current_idx=current_idx,
                        total_accounts=total_accounts,
                        progress=progress,
                    )
                    if recovered:
                        auth_recovery_count += 1
                        continue
                    reason_code = recovery_code
                return AccountProcessResult(
                    metrics=_build_result_payload("等待登录"),
                    account_status=AccountStatus.FAILED,
                    error_code=reason_code,
                    error_message=ERROR_MESSAGES_ZH[reason_code],
                )

            extracted_data = extract_metrics(page)
            extracted_data["认证等级"] = extract_verification_level(page)
            trend_rows, trend_status, trend_error = extract_post_trend_rows(
                trend_payloads,
                uid=uid or "",
                account_id=account_id,
                run_id="",
                crawl_time=now_iso(),
            )
            if not trend_rows:
                trend_rows = extract_post_trend_rows_from_echarts(
                    page,
                    uid=uid or "",
                    account_id=account_id,
                    run_id="",
                    crawl_time=now_iso(),
                )
                if trend_rows:
                    trend_status, trend_error = "success", None
            if trend_status != "success":
                print(f"{progress} ⚠️ 微博趋势未采集：{trend_error or '未知原因'}")
            page_text = _safe_body_text(page)
            has_login_form = _page_has_visible_login_form(page)
            issue_code = infer_post_extraction_issue(
                page_url=page.url,
                page_text=page_text,
                has_login_form=has_login_form,
                extracted_data=extracted_data,
            )
            overall_valid_count = _count_effective_metrics(extracted_data, METRIC_KEYS)
            core_valid_count = _count_effective_metrics(extracted_data, CRITICAL_METRIC_KEYS)

            if issue_code in {ErrorCode.AUTH_REQUIRED, ErrorCode.CAPTCHA_REQUIRED}:
                if auth_recovery_count < max_auth_recoveries:
                    recovered, recovery_code = recover_account_session_after_auth(
                        playwright_obj,
                        session,
                        config,
                        hooks,
                        issue_code=issue_code,
                        page_url=page.url,
                        current_idx=current_idx,
                        total_accounts=total_accounts,
                        progress=progress,
                    )
                    if recovered:
                        auth_recovery_count += 1
                        continue
                    issue_code = recovery_code
                return AccountProcessResult(
                    metrics=_build_result_payload("等待登录"),
                    account_status=AccountStatus.FAILED,
                    error_code=issue_code,
                    error_message=ERROR_MESSAGES_ZH[issue_code],
                )

            if issue_code == ErrorCode.EMPTY_PAGE:
                print(f"{progress} ⚠️ 页面似乎无有效数据。")
                return AccountProcessResult(
                    metrics=extracted_data,
                    account_status=AccountStatus.FAILED,
                    error_code=ErrorCode.EMPTY_PAGE,
                    error_message=ERROR_MESSAGES_ZH[ErrorCode.EMPTY_PAGE],
                )

            print(
                f"{progress} ✅ 成功提取有效指标：核心 {core_valid_count}/{len(CRITICAL_METRIC_KEYS)}，"
                f"总计 {overall_valid_count}/{len(METRIC_KEYS)}。"
            )
            return AccountProcessResult(
                metrics=extracted_data,
                account_status=AccountStatus.SUCCESS,
                error_code=ErrorCode.NONE,
                error_message=ERROR_MESSAGES_ZH[ErrorCode.NONE],
                post_trend_rows=trend_rows,
                post_trend_status=trend_status,
                post_trend_error=trend_error,
            )

        except PlaywrightTimeoutError:
            print(f"{progress} ❌ 页面响应超时。")
            if attempt < config.retry_times:
                print(f"{progress} [重试] {config.retry_backoff_seconds}s 后进行第 {attempt + 1} 次尝试。")
                time.sleep(config.retry_backoff_seconds)
                attempt += 1
                continue
            return AccountProcessResult(
                metrics=_build_result_payload("超时"),
                account_status=AccountStatus.FAILED,
                error_code=ErrorCode.TIMEOUT,
                error_message=ERROR_MESSAGES_ZH[ErrorCode.TIMEOUT],
            )
        except Exception as exc:
            print(f"{progress} ❌ 读取报错: {exc}")
            if is_browser_session_closed_error(exc):
                print(f"{progress} [恢复] 浏览器会话失效，正在自动恢复...")
                recreate_browser_session(
                    playwright_obj,
                    session,
                    config,
                    preferred_state_file=session.get("state_file"),
                )
            if attempt < config.retry_times:
                print(f"{progress} [重试] {config.retry_backoff_seconds}s 后进行第 {attempt + 1} 次尝试。")
                time.sleep(config.retry_backoff_seconds)
                attempt += 1
                continue
            return AccountProcessResult(
                metrics=_build_result_payload("挂起"),
                account_status=AccountStatus.FAILED,
                error_code=ErrorCode.NAVIGATION_ERROR,
                error_message=ERROR_MESSAGES_ZH[ErrorCode.NAVIGATION_ERROR],
            )
        finally:
            if listener_attached:
                try:
                    listener_page.remove_listener("response", capture_post_trend_response)
                except Exception:
                    pass

    return AccountProcessResult(
        metrics=_build_result_payload("挂起"),
        account_status=AccountStatus.FAILED,
        error_code=ErrorCode.NAVIGATION_ERROR,
        error_message=ERROR_MESSAGES_ZH[ErrorCode.NAVIGATION_ERROR],
    )


def run_crawl(config: CrawlConfig, hooks: Optional[CrawlHooks] = None) -> CrawlRunResult:
    hooks = hooks or CrawlHooks()

    input_excel = str(Path(config.input_excel).resolve())
    output_excel = resolve_output_path(config.output_dir, config.output_excel)
    state_store = StateStore(config.progress_state)

    run_id = config.run_id
    if not run_id:
        run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]

    state_store.ensure_run(run_id)

    started_at = now_iso()
    emit_event(
        hooks,
        {
            "type": "task_status",
            "status": TaskStatus.RUNNING,
            "run_id": run_id,
            "started_at": started_at,
            "output_excel": output_excel,
        },
    )

    try:
        df = load_accounts(input_excel)
    except Exception as exc:
        finished_at = now_iso()
        return CrawlRunResult(
            run_id=run_id,
            status=TaskStatus.FAILED,
            total_accounts=0,
            processed_accounts=0,
            success_accounts=0,
            failed_accounts=0,
            skipped_accounts=0,
            started_at=started_at,
            finished_at=finished_at,
            output_excel=output_excel,
            error_code=f"INPUT_ERROR: {exc}",
        )

    total_accounts = len(df)
    processed_accounts = 0
    success_accounts = 0
    failed_accounts = 0
    skipped_accounts = 0
    post_trend_success_accounts = 0
    post_trend_failed_accounts = 0
    post_trend_post_count = 0
    terminal_failure_code = ErrorCode.NONE
    global_request_count = 0

    print(f"[系统] 本次 run_id={run_id}，任务总数 {total_accounts}。")

    original_display = os.environ.get("DISPLAY")
    if config.display:
        os.environ["DISPLAY"] = config.display

    try:
        with sync_playwright() as p:
            startup_state_file = select_startup_state_file(config.state_storage, config.state_json)
            session = init_browser(p, startup_state_file, config.headless, display=config.display)
            session, auth_ready, startup_error_code = ensure_authenticated_session(
                p,
                session,
                config,
                hooks,
                total_accounts=total_accounts,
            )
            if not auth_ready:
                close_browser_session(session)
                finished_at = now_iso()
                final_status = (
                    TaskStatus.BLOCKED_AUTH
                    if startup_error_code in {ErrorCode.AUTH_REQUIRED, ErrorCode.CAPTCHA_REQUIRED}
                    else TaskStatus.FAILED
                )
                emit_event(
                    hooks,
                    {
                        "type": "task_status",
                        "run_id": run_id,
                        "status": final_status,
                        "finished_at": finished_at,
                        "progress": 0.0,
                        "error_code": startup_error_code,
                    },
                )
                return CrawlRunResult(
                    run_id=run_id,
                    status=final_status,
                    total_accounts=total_accounts,
                    processed_accounts=0,
                    success_accounts=0,
                    failed_accounts=0,
                    skipped_accounts=0,
                    started_at=started_at,
                    finished_at=finished_at,
                    output_excel=output_excel,
                    error_code=startup_error_code,
                )

            for index, row in df.iterrows():
                current_idx = index + 1
                aid = str(row.get("账号ID", "未命名账号")).strip()
                uid_raw = row.get("uid")

                if should_stop(hooks):
                    print("[系统] 收到取消信号，准备停止任务。")
                    break

                if pd.isna(uid_raw) or str(uid_raw).strip() == "":
                    skipped_accounts += 1
                    processed_accounts += 1
                    result_row = {
                        "run_id": run_id,
                        "crawl_time": now_iso(),
                        "account_status": AccountStatus.SKIPPED,
                        "error_code": ErrorCode.INVALID_UID,
                        "error_message": ERROR_MESSAGES_ZH[ErrorCode.INVALID_UID],
                        "账号ID": aid,
                        "uid": "",
                        "主页链接": "",
                    }
                    result_row.update({k: "空" for k in METRIC_KEYS})
                    safe_append_row(output_excel, result_row)
                    emit_event(
                        hooks,
                        {
                            "type": "progress",
                            "run_id": run_id,
                            "status": TaskStatus.RUNNING,
                            "current_account": aid,
                            "progress": processed_accounts / total_accounts if total_accounts else 1.0,
                            "processed": processed_accounts,
                            "total": total_accounts,
                            "error_code": ErrorCode.INVALID_UID,
                        },
                    )
                    continue

                uid = str(uid_raw).strip()

                if config.resume and state_store.is_processed(run_id, uid):
                    skipped_accounts += 1
                    processed_accounts += 1
                    print(f"[{current_idx}/{total_accounts}] [ID: {aid}] uid={uid} 已处理，跳过。")
                    emit_event(
                        hooks,
                        {
                            "type": "progress",
                            "run_id": run_id,
                            "status": TaskStatus.RUNNING,
                            "current_account": aid,
                            "progress": processed_accounts / total_accounts if total_accounts else 1.0,
                            "processed": processed_accounts,
                            "total": total_accounts,
                            "error_code": ErrorCode.NONE,
                            "message": f"已跳过 {aid}（{processed_accounts}/{total_accounts}）",
                        },
                    )
                    continue

                global_request_count += 1
                if config.cooldown_every > 0 and global_request_count > 1 and global_request_count % config.cooldown_every == 0:
                    print(f"\n[{current_idx}/{total_accounts}] [机制] 触发冷却保护。")
                    for remaining in range(config.cooldown_seconds, 0, -1):
                        if should_stop(hooks):
                            break
                        sys.stdout.write(f"\r[{current_idx}/{total_accounts}] 冷却倒计时: {remaining:3d} 秒")
                        sys.stdout.flush()
                        time.sleep(1)
                    print("")

                current_account_label = f"{aid}（UID: {uid}）"
                emit_event(
                    hooks,
                    {
                        "type": "progress",
                        "run_id": run_id,
                        "status": TaskStatus.RUNNING,
                        "current_account": current_account_label,
                        "progress": processed_accounts / total_accounts if total_accounts else 1.0,
                        "processed": processed_accounts,
                        "total": total_accounts,
                        "error_code": ErrorCode.NONE,
                        "message": f"正在抓取 {aid}（第 {current_idx}/{total_accounts} 个）",
                    },
                )

                url = f"https://weiq.com/client/product/weibo/detail?account_uid={uid}"
                process_result = process_account_url(
                    playwright_obj=p,
                    session=session,
                    account_id=aid,
                    url=url,
                    current_idx=current_idx,
                    total_accounts=total_accounts,
                    config=config,
                    hooks=hooks,
                    uid=uid,
                )

                crawl_time = now_iso()
                result_row = {
                    "run_id": run_id,
                    "crawl_time": crawl_time,
                    "account_status": process_result.account_status,
                    "error_code": process_result.error_code,
                    "error_message": process_result.error_message,
                    "账号ID": aid,
                    "uid": uid,
                    "主页链接": url,
                }
                result_row.update(process_result.metrics)
                trend_export_rows = process_result.post_trend_rows or [{
                    "run_id": run_id,
                    "crawl_time": crawl_time,
                    "账号ID": aid,
                    "uid": uid,
                    "source_post_id": "",
                    "发布时间": "",
                    "阅读量": None,
                    "正文摘要": "",
                    "封面URL": None,
                    "微博链接": None,
                    "博文类型": "unknown",
                    "source_order": None,
                    "趋势状态": process_result.post_trend_status if process_result.post_trend_status != "not_collected" else "failed",
                    "趋势错误": process_result.post_trend_error or "本账号周指标采集未完成，未能读取微博趋势",
                }]
                for trend_row in trend_export_rows:
                    trend_row["run_id"] = run_id
                    trend_row["crawl_time"] = crawl_time
                if process_result.post_trend_status == "success":
                    post_trend_success_accounts += 1
                    post_trend_post_count += len(process_result.post_trend_rows)
                else:
                    post_trend_failed_accounts += 1

                try:
                    safe_append_row(output_excel, result_row, trend_export_rows)
                    state_store.mark_processed(
                        run_id=run_id,
                        uid=uid,
                        status=process_result.account_status,
                        error_code=process_result.error_code,
                    )
                except Exception as exc:
                    process_result.account_status = AccountStatus.FAILED
                    process_result.error_code = ErrorCode.WRITE_ERROR
                    process_result.error_message = f"{ERROR_MESSAGES_ZH[ErrorCode.WRITE_ERROR]}: {exc}"
                    failed_accounts += 1
                    terminal_failure_code = ErrorCode.WRITE_ERROR
                    processed_accounts += 1
                    emit_event(
                        hooks,
                        {
                            "type": "progress",
                            "run_id": run_id,
                            "status": TaskStatus.RUNNING,
                            "current_account": current_account_label,
                            "progress": processed_accounts / total_accounts if total_accounts else 1.0,
                            "processed": processed_accounts,
                            "total": total_accounts,
                            "error_code": ErrorCode.WRITE_ERROR,
                            "message": f"{aid} 写入结果失败（{processed_accounts}/{total_accounts}）",
                        },
                    )
                    continue

                processed_accounts += 1
                if process_result.account_status == AccountStatus.SUCCESS:
                    success_accounts += 1
                elif process_result.account_status == AccountStatus.CANCELLED:
                    failed_accounts += 1
                    terminal_failure_code = ErrorCode.CANCELLED
                else:
                    failed_accounts += 1
                    if process_result.error_code and process_result.error_code != ErrorCode.NONE:
                        terminal_failure_code = process_result.error_code

                emit_event(
                    hooks,
                    {
                        "type": "progress",
                        "run_id": run_id,
                        "status": TaskStatus.RUNNING,
                        "current_account": current_account_label,
                        "progress": processed_accounts / total_accounts if total_accounts else 1.0,
                        "processed": processed_accounts,
                        "total": total_accounts,
                        "error_code": process_result.error_code,
                        "message": f"已完成 {aid}（{processed_accounts}/{total_accounts}）",
                    },
                )

                if process_result.error_code in {ErrorCode.AUTH_REQUIRED, ErrorCode.CAPTCHA_REQUIRED}:
                    terminal_failure_code = process_result.error_code
                    break

            try:
                persist_login_state(session["context"], state_json=config.state_json, state_storage=config.state_storage)
            except Exception:
                pass
            close_browser_session(session)
    finally:
        if config.display:
            if original_display is None:
                os.environ.pop("DISPLAY", None)
            else:
                os.environ["DISPLAY"] = original_display

    output_path = Path(output_excel)
    output_file_valid = False
    output_validation_error = None
    try:
        with pd.ExcelFile(output_path) as workbook:
            required_sheets = {WEEKLY_SHEET_NAME, POST_TREND_SHEET_NAME}
            missing = required_sheets.difference(workbook.sheet_names)
            if missing:
                output_validation_error = f"结果文件缺少工作表：{', '.join(sorted(missing))}"
            else:
                output_file_valid = True
    except Exception as exc:
        output_validation_error = f"结果文件不可读：{exc}"
    if not output_file_valid:
        print(f"[系统] ❌ {output_validation_error or '结果文件未生成'}")
        if terminal_failure_code == ErrorCode.NONE:
            terminal_failure_code = ErrorCode.WRITE_ERROR

    if should_stop(hooks):
        task_status = TaskStatus.CANCELLED
        error_code = ErrorCode.CANCELLED
    elif terminal_failure_code in {ErrorCode.AUTH_REQUIRED, ErrorCode.CAPTCHA_REQUIRED}:
        task_status = TaskStatus.BLOCKED_AUTH
        error_code = terminal_failure_code
    elif success_accounts > 0 and output_file_valid:
        task_status = TaskStatus.SUCCESS
        error_code = ErrorCode.NONE
    else:
        task_status = TaskStatus.FAILED
        error_code = terminal_failure_code if terminal_failure_code != ErrorCode.NONE else ErrorCode.EMPTY_PAGE
    finished_at = now_iso()
    emit_event(
        hooks,
        {
            "type": "task_status",
            "run_id": run_id,
            "status": task_status,
            "finished_at": finished_at,
            "progress": 1.0 if total_accounts == 0 else processed_accounts / total_accounts,
            "error_code": error_code,
        },
    )

    print("\n============================================")
    print(f"[系统] 任务结束，状态={task_status}，结果文件: {output_excel}")
    print(
        f"[系统] 微博趋势导出：成功账号 {post_trend_success_accounts}，"
        f"未成功账号 {post_trend_failed_accounts}，明细 {post_trend_post_count} 条。"
    )
    print("============================================")

    return CrawlRunResult(
        run_id=run_id,
        status=task_status,
        total_accounts=total_accounts,
        processed_accounts=processed_accounts,
        success_accounts=success_accounts,
        failed_accounts=failed_accounts,
        skipped_accounts=skipped_accounts,
        started_at=started_at,
        finished_at=finished_at,
        output_excel=output_excel,
        error_code=error_code,
    )


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WEIQ 采集脚本")
    parser.add_argument("--input-excel", default=INPUT_EXCEL, help="输入账号表路径")
    parser.add_argument("--output-excel", default=OUTPUT_EXCEL, help="输出结果文件名或路径")
    parser.add_argument("--output-dir", default=".", help="输出目录")
    parser.add_argument("--state-json", default=STATE_JSON, help="长期登录态文件路径")
    parser.add_argument("--state-storage", default=STATE_STORE_JSON, help="本次任务临时登录态文件路径")
    parser.add_argument("--progress-state", default=PROGRESS_STATE_JSON, help="断点续跑进度文件路径")
    parser.add_argument("--headless", action="store_true", help="无头模式运行")
    parser.add_argument("--cooldown-every", type=int, default=50, help="每处理多少账号触发冷却")
    parser.add_argument("--cooldown-seconds", type=int, default=180, help="冷却秒数")
    parser.add_argument("--retry-times", type=int, default=1, help="单账号最大重试次数")
    parser.add_argument("--retry-backoff-seconds", type=int, default=3, help="重试间隔秒数")
    parser.add_argument("--no-resume", action="store_true", help="禁用断点续跑")
    parser.add_argument("--run-id", default=None, help="指定 run_id 进行恢复或重跑")
    parser.add_argument("--display", default=None, help="显式指定 DISPLAY")
    return parser


def parse_cli_config() -> CrawlConfig:
    args = build_cli_parser().parse_args()
    return CrawlConfig(
        input_excel=args.input_excel,
        output_excel=args.output_excel,
        state_json=args.state_json,
        progress_state=args.progress_state,
        output_dir=args.output_dir,
        headless=args.headless,
        cooldown_every=args.cooldown_every,
        cooldown_seconds=args.cooldown_seconds,
        retry_times=max(1, args.retry_times),
        retry_backoff_seconds=max(0, args.retry_backoff_seconds),
        resume=not args.no_resume,
        run_id=args.run_id,
        state_storage=args.state_storage,
        display=args.display,
    )


def main() -> None:
    config = parse_cli_config()
    result = run_crawl(config=config, hooks=CrawlHooks())
    if result.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
        sys.exit(1)


if __name__ == "__main__":
    main()
