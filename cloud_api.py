import queue
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from analytics import incremental_changes, load_result_df, quality_report
from scraper import (
    CrawlConfig,
    CrawlHooks,
    CrawlRunResult,
    ErrorCode,
    TaskStatus,
    run_crawl,
)

DB_PATH = Path("weiq_local.db").resolve()
TASK_QUEUE: "queue.Queue[str]" = queue.Queue()
DB_LOCK = threading.Lock()

app = FastAPI(title="WEIQ Scraper API", version="0.1.0")


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


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


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
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                )
                """
            )
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


def is_cancel_requested(task_id: str) -> bool:
    row = fetch_one("SELECT cancel_requested FROM tasks WHERE task_id = ?", (task_id,))
    return bool(row and row["cancel_requested"])


def wait_for_resume_or_cancel(task_id: str) -> bool:
    while True:
        row = fetch_one(
            "SELECT cancel_requested, resume_requested FROM tasks WHERE task_id = ?",
            (task_id,),
        )
        if not row:
            return False
        if row["cancel_requested"]:
            return False
        if row["resume_requested"]:
            upsert_task_event(
                task_id,
                {
                    "resume_requested": 0,
                    "status": TaskStatus.RUNNING,
                    "blocked_reason": None,
                    "message": "已收到继续请求，恢复运行",
                },
            )
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
                    "message": "等待手动登录或验证码处理",
                },
            )

    def should_stop() -> bool:
        return is_cancel_requested(task_id)

    def on_auth_required(reason_code: str, page_url: str) -> bool:
        upsert_task_event(
            task_id,
            {
                "status": TaskStatus.BLOCKED_AUTH,
                "blocked_reason": reason_code,
                "message": f"等待处理登录风控: {page_url}",
            },
        )
        return wait_for_resume_or_cancel(task_id)

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
            created_at
        ) VALUES (?, ?, 0, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    return row


@app.post("/v1/tasks/{task_id}/cancel", response_model=TaskControlResponse)
def cancel_task(task_id: str) -> TaskControlResponse:
    row = fetch_one("SELECT status FROM tasks WHERE task_id = ?", (task_id,))
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")

    if row["status"] in {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}:
        return TaskControlResponse(task_id=task_id, status=row["status"], message="任务已是终态")

    upsert_task_event(task_id, {"cancel_requested": 1, "message": "已请求取消任务"})
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
