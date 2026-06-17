# WEIQ Scraper Stable API

`weiq-scraper-stable-api` 是唯一抓取执行器。

- `weibo` 负责业务编排、登录引导、预览和导入
- 本仓库负责登录会话落盘、Playwright 抓取执行、Excel 导出、任务状态查询
- `weibo` 只通过 HTTP 调用本服务
- `weibo` 不读取本仓库 SQLite，也不依赖共享目录

## 当前结构

- [scraper.py](/Users/youfei/Desktop/weiq-scraper-stable-api/scraper.py)  
  CLI 包装层，终端模式入口
- [scraper_runtime.py](/Users/youfei/Desktop/weiq-scraper-stable-api/scraper_runtime.py)  
  共用抓取 runtime，CLI 和 API 都调用 `run_crawl()`
- [cloud_api.py](/Users/youfei/Desktop/weiq-scraper-stable-api/cloud_api.py)  
  FastAPI 服务，负责 auth session、task、worker、export
- [main.py](/Users/youfei/Desktop/weiq-scraper-stable-api/main.py)  
  本地结果看板，不参与 API worker 执行

## 安装

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e .
./.venv/bin/python -m playwright install chromium
```

## CLI 用法

终端模式仍可继续使用，并且现在也走同一个 `scraper_runtime.run_crawl()`。

```bash
./.venv/bin/python scraper.py \
  --input-excel ./accounts.xlsx \
  --output-dir ./output \
  --state-storage ./state.json
```

可选参数：

- `--output-excel` 指定完整输出文件路径
- `--headless` 使用无头浏览器
- `--login-url` 指定缺少登录态时打开的登录页
- `--probe-verify` 运行认证等级探针
- `--probe-uids` 指定探针 uid 列表

首次没有 `storage_state` 时，CLI 会打开浏览器等待人工登录，登录成功后保存到 `state.json`。

## API 用法

启动服务：

```bash
./.venv/bin/python -m uvicorn cloud_api:app --host 127.0.0.1 --port 8080 --workers 1
```

健康检查：

```bash
curl http://127.0.0.1:8080/health
```

### 登录会话接口

- `POST /v1/auth/session`
  - 轻量创建 session，只建目录和 DB 记录，不启动浏览器
- `POST /v1/auth/session/{session_id}/submit`
  - 真正执行服务器端登录，并在成功后生成 `storage_state.json`
- `POST /v1/auth/session/{session_id}/check`
  - 只检查该 session 的 `storage_state`

### 抓取任务接口

- `POST /v1/tasks/crawl`
  - 必须带 `login_session_id`
  - 对应 session 必须已经 `authenticated`
- `GET /v1/tasks/{task_id}`
  - 查询任务状态和进度
- `GET /v1/tasks/{task_id}/export`
  - 下载本次抓取导出的 Excel
- `POST /v1/tasks/{task_id}/cancel`
  - 取消任务

### 诊断接口

- `GET /v1/worker/health`
- `GET /v1/debug/env`
- `GET /v1/debug/weiq-access`

`/v1/debug/env` 只返回安全诊断信息，不返回 cookie、密码、token、`storage_state` 内容。
`/v1/debug/weiq-access` 会分别用 `httpx` 和 Playwright Chromium 测试 WEIQ 可达性，并返回安全化后的出口信息。

## 运行时目录与环境变量

推荐线上目录：

```text
/opt/weiq-scraper-stable-api
├── .venv
├── cloud_api.py
├── scraper.py
├── scraper_runtime.py
├── weiq_local.db
└── runtime
    ├── auth_sessions
    └── tasks
```

环境变量：

```bash
WEIQ_DB_PATH=/opt/weiq-scraper-stable-api/weiq_local.db
WEIQ_API_RUNTIME_DIR=/opt/weiq-scraper-stable-api/runtime
WEIQ_AUTH_STATE_DIR=/opt/weiq-scraper-stable-api/runtime/auth_sessions
WEIQ_AUTH_SESSION_TTL_SECONDS=600
WEIQ_KEEP_AUTH_STATE_FOR_DEBUG=false
WEIQ_PROXY_SERVER=
WEIQ_PROXY_USERNAME=
WEIQ_PROXY_PASSWORD=
WEIQ_PROXY_BYPASS=
```

说明：

- `WEIQ_DB_PATH` 必须是绝对路径
- 不要依赖当前工作目录生成 SQLite
- worker 目前只支持单进程内存队列，必须 `--workers 1`
- 如果配置 `WEIQ_PROXY_SERVER`，登录提交、抓取执行、Playwright 诊断、`httpx` 诊断都会走同一套代理出口

## systemd 示例

```ini
[Unit]
Description=WEIQ Scraper Stable API
After=network.target

[Service]
WorkingDirectory=/opt/weiq-scraper-stable-api
Environment=WEIQ_DB_PATH=/opt/weiq-scraper-stable-api/weiq_local.db
Environment=WEIQ_API_RUNTIME_DIR=/opt/weiq-scraper-stable-api/runtime
Environment=WEIQ_AUTH_STATE_DIR=/opt/weiq-scraper-stable-api/runtime/auth_sessions
Environment=WEIQ_AUTH_MODE=per_task
Environment=WEIQ_KEEP_AUTH_STATE_FOR_DEBUG=false
ExecStart=/usr/bin/xvfb-run -a /opt/weiq-scraper-stable-api/.venv/bin/python -m uvicorn cloud_api:app --host 127.0.0.1 --port 8080 --workers 1
Restart=always

[Install]
WantedBy=multi-user.target
```

## 与 weibo 的边界

- `weibo` 只需要配置 `WEIQ_CLOUD_API_BASE_URL=http://127.0.0.1:8080`
- `weibo` 不需要 `WEIQ_CLOUD_SHARED_DIR`
- `weibo` 不读取本仓库 DB
- `weibo` 只通过 HTTP 调用本服务

## Excel 输出兼容性

导出 Excel 继续沿用原抓取字段，并兼容 `weibo` 的 importer：

- `账号ID`
- `uid`
- `主页链接`
- `认证等级`
- `粉丝数`
- `直发CPM`
- `阅读中位数`
- `直发阅读中位数`
- `转发阅读中位数`
- `互动中位数`
- `直发互动中位数`
- `转发互动中位数`
- `发布博文数`
- `转发中位数`
- `评论中位数`
- `点赞中位数`
- `最低阅读量`
- `最高阅读量`
- `阅读量均值`

`weibo` 侧会把 `认证等级` 识别为 `认证层级` 别名，因此不需要第二套导入逻辑。

## 服务器出口被拦截

如果 `/v1/debug/weiq-access` 返回 `blocked_detected=true`，说明代码链路基本正常，但当前 stable-api 运行环境访问 WEIQ 时被目标站点或中间网络拦截。

典型表现：

- `requests.blocked_detected=true`
- `playwright.blocked_detected=true`
- 页面标题出现 `The URL you requested has been blocked`

这时不要继续重写抓取流程，优先处理网络出口：

1. 更换能正常访问 WEIQ 的服务器。
2. 把 stable-api 部署到本机或内网机器，再通过安全隧道让 `weibo` 调用。
3. 配置稳定且合规的代理出口。
4. 联系 WEIQ 放行 stable-api 所在服务器的出口 IP。

## 验证

```bash
./.venv/bin/python -m py_compile scraper.py scraper_runtime.py cloud_api.py
./.venv/bin/python -m pytest tests/test_cloud_api.py tests/test_cli_compile.py
```
