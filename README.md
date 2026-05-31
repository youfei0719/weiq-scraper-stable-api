# WEIQ 数据采集与分析（稳定版）

本项目当前提供两条可用主链路：
- 采集链路：`Excel 账号输入 -> Playwright 采集 -> Excel 结果输出`
- 分析链路：`Streamlit 看板读取结果 Excel 并可视化分析`

同时提供轻量 API 任务壳（FastAPI + SQLite + 单 worker）用于网站或后端轮询接入。

## 当前能力

### 1) 可恢复采集（`scraper.py`）
- CLI 参数化：输入表、输出目录、状态文件、限速、重试、无头模式等。
- 断点续跑：按 `run_id + uid` 记录处理状态，可恢复中断任务。
- 标准元数据字段（写入结果表）：
  - `run_id`
  - `crawl_time`
  - `account_status`
  - `error_code`
  - `error_message`
- 原子写入：结果写盘采用“临时文件 + 原子替换”策略，降低中断损坏风险。
- 反爬恢复：登录失效、验证码、超时、空页面等场景统一错误码。

### 2) 轻量任务 API（`cloud_api.py`）
- 任务状态持久化：SQLite（`weiq_local.db`）。
- 单 worker 串行执行：适合单人、周频、100-500 账号规模。
- 支持接口：
  - `POST /v1/tasks/crawl` 创建任务
  - `GET /v1/tasks/{task_id}` 查询任务状态
  - `POST /v1/tasks/{task_id}/cancel` 取消任务
  - `POST /v1/tasks/{task_id}/resume` 登录阻塞后继续
  - `GET /v1/tasks/{task_id}/latest` 查询该任务最新结果
  - `GET /v1/tasks/{task_id}/quality` 查询该任务质量评分
  - `GET /v1/accounts/{uid}/changes` 查询账号指标变化摘要

### 3) 分析看板（`main.py`）
- 读取 `weiq_results.xlsx`（或环境变量 `WEIQ_DATA_FILE` 指定文件）。
- 若结果表包含 `run_id`，支持按批次筛选分析。

## 目录结构

```text
.
├── scraper.py       # 采集内核 + CLI
├── cloud_api.py     # 轻量 API 任务壳
├── main.py          # Streamlit 分析看板
├── tests/           # 基础测试
├── pyproject.toml
└── README.md
```

## 安装

```bash
python -m pip install --upgrade pip
python -m pip install -e .
python -m playwright install chromium --no-shell
```

## 采集 CLI 用法

### 最简运行

```bash
python scraper.py
```

默认读取：`accounts.xlsx`，默认输出：当前目录 `weiq_results.xlsx`。

### 常用参数

```bash
python scraper.py \
  --input-excel ./accounts.xlsx \
  --output-dir ./exports \
  --output-excel weiq_results.xlsx \
  --state-json ./state.json \
  --state-storage ./state_store.json \
  --cooldown-every 50 \
  --cooldown-seconds 180 \
  --retry-times 2 \
  --retry-backoff-seconds 3
```

可选参数：
- `--headless`：无头模式
- `--run-id <id>`：指定 run_id 恢复或重跑
- `--no-resume`：禁用断点续跑

## 启动 API

```bash
python -m uvicorn cloud_api:app --host 0.0.0.0 --port 8080
```

健康检查：

```bash
curl http://127.0.0.1:8080/health
```

### 创建任务

```bash
curl -X POST "http://127.0.0.1:8080/v1/tasks/crawl" \
  -H "Content-Type: application/json" \
  -d '{
    "input_excel": "accounts.xlsx",
    "output_excel": "weiq_results.xlsx",
    "output_dir": ".",
    "retry_times": 2,
    "resume": true
  }'
```

### 轮询任务状态

```bash
curl "http://127.0.0.1:8080/v1/tasks/<task_id>"
```

状态枚举：
- `PENDING`
- `RUNNING`
- `BLOCKED_AUTH`
- `SUCCESS`
- `FAILED`
- `CANCELLED`

### 登录阻塞后继续

```bash
curl -X POST "http://127.0.0.1:8080/v1/tasks/<task_id>/resume"
```

### 取消任务

```bash
curl -X POST "http://127.0.0.1:8080/v1/tasks/<task_id>/cancel"
```

### 查询任务最新结果

```bash
curl "http://127.0.0.1:8080/v1/tasks/<task_id>/latest?limit=20"
```

### 查询任务质量评分

```bash
curl "http://127.0.0.1:8080/v1/tasks/<task_id>/quality"
```

### 查询账号变化摘要

```bash
curl "http://127.0.0.1:8080/v1/accounts/<uid>/changes?output_excel=weiq_results.xlsx&limit=30"
```

## 启动看板

```bash
python -m streamlit run main.py
```

如果采集输出不在默认路径，可先设置：

```bash
export WEIQ_DATA_FILE=/绝对路径/weiq_results.xlsx
python -m streamlit run main.py
```

## 测试

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

## 错误码（采集）

- `NONE`
- `INVALID_UID`
- `HTTP_BLOCKED`
- `TIMEOUT`
- `AUTH_REQUIRED`
- `CAPTCHA_REQUIRED`
- `EMPTY_PAGE`
- `NAVIGATION_ERROR`
- `WRITE_ERROR`
- `CANCELLED`

## 安全建议

请勿提交以下文件到仓库：
- `state.json`
- `*.db`
- `accounts.xlsx`
- `weiq_results*.xlsx`
