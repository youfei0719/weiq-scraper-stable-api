# 更新日志

## [v0.2.0] - 2026-05-31

### Added
- `scraper.py` 重构为可复用采集内核，支持 CLI 参数化配置。
- 新增结果元数据字段：`run_id`, `crawl_time`, `account_status`, `error_code`, `error_message`。
- 新增断点续跑状态存储（按 `run_id + uid`）。
- 新增 `cloud_api.py`（FastAPI + SQLite + 单 worker）任务壳。
- 新增 API 接口：
  - `POST /v1/tasks/crawl`
  - `GET /v1/tasks/{task_id}`
  - `POST /v1/tasks/{task_id}/cancel`
  - `POST /v1/tasks/{task_id}/resume`
  - `GET /v1/tasks/{task_id}/latest`
- 新增扩展分析能力（阶段 3 轻量版）：
  - `GET /v1/tasks/{task_id}/quality` 质量评分与预警
  - `GET /v1/accounts/{uid}/changes` 增量变化摘要
- 新增基础测试：状态存储与 API 最小生命周期。

### Changed
- 结果写盘改为临时文件原子替换，降低中断导致文件损坏风险。
- 反爬相关场景统一标准错误码输出。
- `main.py` 支持按 `run_id` 批次筛选分析。
- `README.md` 改为与当前实际代码一致。
- `pyproject.toml` 补齐看板和 API 依赖。

### Fixed
- 修复文档与仓库真实实现不一致问题。
- 修复依赖声明不足导致环境初始化后看板/API无法直接运行的问题。
