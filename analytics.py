from __future__ import annotations

from typing import Any

import pandas as pd

DEFAULT_TRACK_METRICS = ["粉丝数", "直发CPM", "阅读中位数", "发布博文数"]


def _is_empty_value(value: Any) -> bool:
    if pd.isna(value):
        return True
    value_str = str(value).strip()
    return value_str in {"", "空", "空_无标签", "空_无数据", "账号失效/未收录", "挂起", "超时", "等待登录"}


def load_result_df(output_excel: str) -> pd.DataFrame:
    return pd.read_excel(output_excel)


def incremental_changes(
    df: pd.DataFrame,
    uid: str,
    metrics: list[str] | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    metrics = metrics or DEFAULT_TRACK_METRICS
    if "uid" not in df.columns:
        return {"uid": uid, "changes": [], "count": 0}

    uid_df = df[df["uid"].astype(str) == str(uid)].copy()
    if uid_df.empty:
        return {"uid": uid, "changes": [], "count": 0}

    if "crawl_time" in uid_df.columns:
        uid_df["crawl_time_parsed"] = pd.to_datetime(uid_df["crawl_time"], errors="coerce")
        uid_df = uid_df.sort_values(by="crawl_time_parsed")

    changes: list[dict[str, Any]] = []
    for metric in metrics:
        if metric not in uid_df.columns:
            continue
        prev = None
        for _, row in uid_df.iterrows():
            current = row.get(metric)
            if prev is None:
                prev = current
                continue
            if str(current) != str(prev):
                changes.append(
                    {
                        "uid": uid,
                        "metric": metric,
                        "from": prev,
                        "to": current,
                        "crawl_time": row.get("crawl_time"),
                        "run_id": row.get("run_id"),
                    }
                )
            prev = current

    return {
        "uid": uid,
        "count": len(changes),
        "changes": changes[-max(1, min(limit, 500)) :],
    }


def quality_report(df: pd.DataFrame, run_id: str | None = None) -> dict[str, Any]:
    if run_id and "run_id" in df.columns:
        df = df[df["run_id"].astype(str) == str(run_id)]

    total = len(df)
    if total == 0:
        return {
            "run_id": run_id,
            "total": 0,
            "success_rate": 0.0,
            "missing_rate": 1.0,
            "score": 0,
            "alerts": ["无可用数据"],
        }

    success_rate = 0.0
    if "account_status" in df.columns:
        success_rate = float((df["account_status"] == "SUCCESS").sum()) / total

    missing_counter = 0
    metric_counter = 0
    for metric in DEFAULT_TRACK_METRICS:
        if metric not in df.columns:
            continue
        metric_counter += len(df)
        missing_counter += sum(_is_empty_value(v) for v in df[metric].tolist())

    missing_rate = (missing_counter / metric_counter) if metric_counter else 1.0

    score = int(max(0, min(100, round(success_rate * 70 + (1 - missing_rate) * 30, 0))))

    alerts: list[str] = []
    if success_rate < 0.8:
        alerts.append("成功率低于 80%，建议检查登录态和风控频率。")
    if missing_rate > 0.3:
        alerts.append("关键指标缺失率较高，建议排查页面结构变化。")

    return {
        "run_id": run_id,
        "total": total,
        "success_rate": round(success_rate, 4),
        "missing_rate": round(missing_rate, 4),
        "score": score,
        "alerts": alerts,
    }
