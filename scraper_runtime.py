import argparse
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
STATE_STORE_JSON = "crawl_state.json"

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
]
CRITICAL_METRIC_KEYS = ["粉丝数", "直发CPM", "阅读中位数", "发布博文数"]
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
LOGIN_FORM_SELECTORS = [
    "input[type='password']",
    "input[placeholder*='密码']",
    "input[placeholder*='验证码']",
    "input[placeholder*='手机号']",
    "input[placeholder*='账号']",
    "input[placeholder*='用户名']",
]

RESULT_META_KEYS = ["run_id", "crawl_time", "account_status", "error_code", "error_message"]


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
    ErrorCode.CANCELLED: "任务被取消",
}


@dataclass
class CrawlConfig:
    input_excel: str = INPUT_EXCEL
    output_excel: str = OUTPUT_EXCEL
    state_json: str = STATE_JSON
    output_dir: str = "."
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
        if _parse_metric_number(value) > 0:
            count += 1
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


def load_accounts(input_excel: str) -> pd.DataFrame:
    df = pd.read_excel(input_excel)
    if "uid" not in df.columns:
        raise ValueError("输入表缺少 uid 列")
    if "账号ID" not in df.columns:
        df["账号ID"] = "未命名账号"
    return df


def to_atomic_excel(path: str, df: pd.DataFrame) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(target.parent), suffix=".xlsx", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        df.to_excel(tmp_path, index=False)
        os.replace(tmp_path, target)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def safe_append_row(path: str, row_dict: dict[str, Any]) -> None:
    row_df = pd.DataFrame([row_dict])
    target = Path(path)

    if target.exists():
        old_df = pd.read_excel(target)
        all_df = pd.concat([old_df, row_df], ignore_index=True, sort=False)
    else:
        all_df = row_df

    to_atomic_excel(str(target), all_df)


def init_browser(playwright_obj, state_file: str, headless: bool):
    print("[初始化] 正在启动浏览器...")
    browser = playwright_obj.chromium.launch(headless=headless)

    if os.path.exists(state_file):
        print(f"[初始化] 检测到凭证文件 {state_file}，尝试恢复会话。")
        context = browser.new_context(storage_state=state_file)
    else:
        print("[警告] 未检测到凭证文件，将以未登录状态启动。")
        context = browser.new_context()

    page = context.new_page()
    return browser, context, page


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

        if (elements.length === 0) return "空_无标签";

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
            }
            parent = parent ? parent.parentElement : null;
        }

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


def process_account_url(
    page,
    context,
    account_id: str,
    url: str,
    current_idx: int,
    total_accounts: int,
    config: CrawlConfig,
    hooks: CrawlHooks,
) -> AccountProcessResult:
    progress = f"[{current_idx}/{total_accounts}]"
    print(f"\n{progress} ----------------------------------------------------")
    print(f"{progress} [ID: {account_id}] 正在访问页面...")

    for attempt in range(1, config.retry_times + 1):
        if should_stop(hooks):
            return AccountProcessResult(
                metrics={k: "取消" for k in METRIC_KEYS},
                account_status=AccountStatus.CANCELLED,
                error_code=ErrorCode.CANCELLED,
                error_message=ERROR_MESSAGES_ZH[ErrorCode.CANCELLED],
            )

        try:
            response = page.goto(url, timeout=config.goto_timeout_ms, wait_until="domcontentloaded")
            if response is None or response.status >= 400:
                msg = f"状态码异常: {response.status if response else 'Null'}"
                print(f"{progress} ❌ {msg}")
                return AccountProcessResult(
                    metrics={k: "异常_阻断" for k in METRIC_KEYS},
                    account_status=AccountStatus.FAILED,
                    error_code=ErrorCode.HTTP_BLOCKED,
                    error_message=ERROR_MESSAGES_ZH[ErrorCode.HTTP_BLOCKED],
                )

            sys.stdout.write(f"\r{progress} 页面抵达，执行滚动加载...")
            sys.stdout.flush()
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
                        metrics={k: "取消" for k in METRIC_KEYS},
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
                auth_handler = hooks.on_auth_required or default_auth_handler
                emit_event(
                    hooks,
                    {
                        "type": "auth_required",
                        "reason_code": reason_code,
                        "page_url": page.url,
                        "current_index": current_idx,
                        "total_accounts": total_accounts,
                    },
                )
                if auth_handler(reason_code, page.url, page, context, config.state_json):
                    if attempt < config.retry_times:
                        print(f"{progress} [恢复] 登录处理完成，准备重试当前账号...")
                        time.sleep(config.retry_backoff_seconds)
                        continue
                return AccountProcessResult(
                    metrics={k: "等待登录" for k in METRIC_KEYS},
                    account_status=AccountStatus.FAILED,
                    error_code=reason_code,
                    error_message=ERROR_MESSAGES_ZH[reason_code],
                )

            extracted_data = extract_metrics(page)
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
                auth_handler = hooks.on_auth_required or default_auth_handler
                emit_event(
                    hooks,
                    {
                        "type": "auth_required",
                        "reason_code": issue_code,
                        "page_url": page.url,
                        "current_index": current_idx,
                        "total_accounts": total_accounts,
                    },
                )
                if auth_handler(issue_code, page.url, page, context, config.state_json):
                    if attempt < config.retry_times:
                        print(f"{progress} [恢复] 登录处理完成，准备重试当前账号...")
                        time.sleep(config.retry_backoff_seconds)
                        continue
                return AccountProcessResult(
                    metrics={k: "等待登录" for k in METRIC_KEYS},
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
            )

        except PlaywrightTimeoutError:
            print(f"{progress} ❌ 页面响应超时。")
            if attempt < config.retry_times:
                print(f"{progress} [重试] {config.retry_backoff_seconds}s 后进行第 {attempt + 1} 次尝试。")
                time.sleep(config.retry_backoff_seconds)
                continue
            return AccountProcessResult(
                metrics={k: "超时" for k in METRIC_KEYS},
                account_status=AccountStatus.FAILED,
                error_code=ErrorCode.TIMEOUT,
                error_message=ERROR_MESSAGES_ZH[ErrorCode.TIMEOUT],
            )
        except Exception as exc:
            print(f"{progress} ❌ 读取报错: {exc}")
            if attempt < config.retry_times:
                print(f"{progress} [重试] {config.retry_backoff_seconds}s 后进行第 {attempt + 1} 次尝试。")
                time.sleep(config.retry_backoff_seconds)
                continue
            return AccountProcessResult(
                metrics={k: "挂起" for k in METRIC_KEYS},
                account_status=AccountStatus.FAILED,
                error_code=ErrorCode.NAVIGATION_ERROR,
                error_message=ERROR_MESSAGES_ZH[ErrorCode.NAVIGATION_ERROR],
            )

    return AccountProcessResult(
        metrics={k: "挂起" for k in METRIC_KEYS},
        account_status=AccountStatus.FAILED,
        error_code=ErrorCode.NAVIGATION_ERROR,
        error_message=ERROR_MESSAGES_ZH[ErrorCode.NAVIGATION_ERROR],
    )


def run_crawl(config: CrawlConfig, hooks: Optional[CrawlHooks] = None) -> CrawlRunResult:
    hooks = hooks or CrawlHooks()

    input_excel = str(Path(config.input_excel).resolve())
    output_excel = resolve_output_path(config.output_dir, config.output_excel)
    state_store = StateStore(config.state_storage or config.state_json)

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
    global_request_count = 0

    print(f"[系统] 本次 run_id={run_id}，任务总数 {total_accounts}。")

    with sync_playwright() as p:
        browser, context, page = init_browser(p, config.state_json, config.headless)

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
                page=page,
                context=context,
                account_id=aid,
                url=url,
                current_idx=current_idx,
                total_accounts=total_accounts,
                config=config,
                hooks=hooks,
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

            try:
                safe_append_row(output_excel, result_row)
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
            else:
                failed_accounts += 1

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

        context.storage_state(path=config.state_json)
        browser.close()

    task_status = TaskStatus.SUCCESS
    error_code = ErrorCode.NONE

    if should_stop(hooks):
        task_status = TaskStatus.CANCELLED
        error_code = ErrorCode.CANCELLED
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
    parser.add_argument("--state-json", default=STATE_JSON, help="登录态文件路径")
    parser.add_argument("--state-storage", default=STATE_STORE_JSON, help="断点恢复状态存储文件")
    parser.add_argument("--headless", action="store_true", help="无头模式运行")
    parser.add_argument("--cooldown-every", type=int, default=50, help="每处理多少账号触发冷却")
    parser.add_argument("--cooldown-seconds", type=int, default=180, help="冷却秒数")
    parser.add_argument("--retry-times", type=int, default=1, help="单账号最大重试次数")
    parser.add_argument("--retry-backoff-seconds", type=int, default=3, help="重试间隔秒数")
    parser.add_argument("--no-resume", action="store_true", help="禁用断点续跑")
    parser.add_argument("--run-id", default=None, help="指定 run_id 进行恢复或重跑")
    return parser


def parse_cli_config() -> CrawlConfig:
    args = build_cli_parser().parse_args()
    return CrawlConfig(
        input_excel=args.input_excel,
        output_excel=args.output_excel,
        state_json=args.state_json,
        output_dir=args.output_dir,
        headless=args.headless,
        cooldown_every=args.cooldown_every,
        cooldown_seconds=args.cooldown_seconds,
        retry_times=max(1, args.retry_times),
        retry_backoff_seconds=max(0, args.retry_backoff_seconds),
        resume=not args.no_resume,
        run_id=args.run_id,
        state_storage=args.state_storage,
    )


def main() -> None:
    config = parse_cli_config()
    result = run_crawl(config=config, hooks=CrawlHooks())
    if result.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
        sys.exit(1)


if __name__ == "__main__":
    main()
