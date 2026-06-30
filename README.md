# WEIQ 数据采集与变化追踪平台

最近更新时间：**2026-06-30 19:30:00 CST (+0800, Asia/Shanghai)**

## 1. 项目简介

WEIQ 项目用于采集账号数据、记录字段变化，并对网站提供标准 API 接口。项目分为两条使用路径：
- 本地启动器（给运营/非技术同学）：双击启动，自动拉起浏览器、校验登录态、引导登录后继续采集。
- API 服务（给网站/后端）：任务提交、任务状态查询、账号最新数据与变化记录读取。

核心目标：
- 把“手工采集 + 手工拷表”改成“任务化采集 + 可追溯存储 + 稳定接口输出”。
- 处理登录失效、任务阻塞、导出管理等高频问题。

## 2. 当前版本能力总览

- 任务级临时登录态：每个抓取任务单独创建一份远端临时 `storage_state.json`，登录成功后只供当前任务使用。
- 任务可观测：状态、进度、当前账号、结构化日志、错误码与中文提示可实时查看。
- 旧库兼容：支持旧 SQLite 结构兼容运行，避免升级后直接报错卡死。
- 多账号表运行：可选择 `inputs/` 下不同 Excel 作为任务输入源。
- 导出增强：
  - 运行前可指定导出目录；
  - 运行后可把导出文件复制到任意目录；
  - 历史文件不覆盖。
- 认证等级识别（结构化优先）：
  - 新增结果字段 `认证等级`；
  - 主判定改为读取昵称右侧认证 `svg` 的 `path fill` 组合；
  - 当前已验真映射：`#FFF/#F6CA45/#FFF -> 黄V`、`#FFF/#FF6C00/#FFF -> 橙V`、`#FEFF78/#CD3620/#FEFF78 -> 金V`；
  - 明确区分 `无认证` 与 `unknown`，不以截图取色作为主逻辑。
- 网站接入友好：提供任务与账号数据查询 API，支持轮询集成。
- 启动统一治理：
  - `scraper.py` 只保留兼容入口，实际统一走 `scraper_runtime.py`；
  - 启动后总是先打开 `https://www.weiq.com/` 验证登录态，不再停留 `about:blank`；
  - 长期 `state.json` 失效时自动进入重登流程，并刷新本次任务的 `storage_state.json`。

## 3. 目录结构说明

```text
.
├── scraper.py                # 兼容入口（python scraper.py）
├── scraper_runtime.py        # 唯一采集运行时（浏览器/登录态/任务执行）
├── cloud_api.py              # API 服务（FastAPI）
├── main.py                   # 结果分析看板（Streamlit）
├── start_desktop.command     # macOS 双击启动器
├── start_desktop.bat         # Windows 双击启动器
├── tests/                    # 单元测试
├── runtime/                  # API/任务运行时目录（不提交仓库）
├── weiq_local.db             # 本地 SQLite（本地使用，不提交仓库）
└── CHANGELOG.md              # 更新日志
```

## 4. 快速开始（非技术用户）

### 第 1 步：安装依赖

```bash
python -m pip install --upgrade pip
python -m pip install playwright "psycopg[binary]" apscheduler streamlit plotly openpyxl pandas fastapi uvicorn
python -m playwright install chromium --no-shell
```

说明：
- 如果你使用 `zsh`，`psycopg[binary]` 必须加引号。
- `--no-shell` 可减少浏览器内核下载失败概率。

### 第 2 步：启动采集器

- macOS：双击 `start_desktop.command`
- Windows：双击 `start_desktop.bat`
- 或命令行：

```bash
python scraper.py
```

启动后程序会自动执行：
1. 启动浏览器。
2. 打开 `https://www.weiq.com/`。
3. 校验长期 `state.json` 是否仍然有效。
4. 如失效，提示你在浏览器中重新登录。
5. 登录通过后自动刷新本次任务的 `storage_state.json`，再开始采集。

### 第 3 步：准备账号输入表

把账号表放在项目目录下，默认文件名为 `accounts.xlsx`。

建议字段：
- `账号ID`（业务名称）
- `uid`（唯一标识，必填）

### 第 4 步：运行时如何看状态

终端会直接输出中文状态日志，例如：
- `正在校验长期登录态`
- `长期登录态失效，已打开 WEIQ，请完成登录`
- `登录验证通过，开始采集`
- `浏览器会话失效，正在自动恢复`
- `任务结束，状态=SUCCESS`

关键状态说明：
- `运行中`：正常采集。
- `等待登录`：登录失效或风控，需要你在浏览器完成登录。
- `已暂停`：人为暂停。
- `失败`：任务已终止，查看错误码和日志。
- `成功`：任务完成，可导出或供 API 读取。

### 第 5 步：登录失效处理（重点）

当程序提示登录失效时：
1. 浏览器窗口会自动保持在 WEIQ 页面，不会停在 `about:blank`。
2. 你直接在浏览器里完成登录或安全验证。
3. 回到终端按回车继续。
4. 系统会再次校验登录态，通过后自动刷新长期 `state.json` 和本次任务 `storage_state.json`。

### 第 6 步：导出与归档

任务完成后：
- 页面显示本次导出文件路径。
- 可输入任意目标目录，点击“复制导出文件到目标目录”。
- 原始导出保留，不覆盖历史。

## 5. 技术用户 CLI 教程

### 5.1 直接运行

```bash
python scraper.py
```

或使用统一运行时入口：

```bash
python -m scraper_runtime
```

### 5.2 指定输入表与导出目录

```bash
python scraper.py \
  --input-excel /绝对路径/accounts.xlsx \
  --output-dir /绝对路径/exports
```

### 5.3 显式指定登录态与断点文件

```bash
python scraper.py \
  --state-json /绝对路径/state.json \
  --state-storage /绝对路径/storage_state.json \
  --progress-state /绝对路径/crawl_progress.json
```

说明：
- `state.json`：长期登录态，程序启动时会先验真它。
- `storage_state.json`：本次任务临时登录态快照。
- `crawl_progress.json`：断点续跑进度文件。

### 5.4 无头模式（仅在已验证登录态时建议使用）

```bash
python scraper.py --headless
```

### 5.5 为什么 `state.json` 会失效

`state.json` 不是永久票据。WEIQ 侧可能因为登录超时、风控、验证码、安全验证等原因让旧登录态失效。

当前版本的处理策略是：
- 不再因为文件存在就默认视为已登录；
- 每次启动先真实打开 WEIQ 首页做验真；
- 失效时自动引导你重登；
- 重登成功后重新保存长期 `state.json`，再派生本次任务的 `storage_state.json`。

## 6. API 接入网站详细教程

本节给网站开发同学，按“提交任务 -> 轮询 -> 控制 -> 拉取数据”落地。

### 6.1 启动 API 服务

```bash
python -m uvicorn cloud_api:app --host 0.0.0.0 --port 8080 --workers 1

说明：
- 当前云端 API 依赖进程内任务队列，只支持单 worker 进程模式。
- 默认兼容旧流程的认证模式为 `WEIQ_AUTH_MODE=per_task`。
- 新的网站一键触发流程推荐改成 `WEIQ_BROWSER_AUTH_MODE=browser_worker`，并配套：
  - `WEIQ_LEGACY_STATE_JSON=/opt/weiq-scraper-stable-api/state.json`
  - `WEIQ_BROWSER_USER_DATA_DIR=/opt/weiq-scraper-stable-api/browser_profile`
  - `WEIQ_BROWSER_HEADLESS=false`
- 可选环境变量：
  - `WEIQ_AUTH_SESSION_TTL_SECONDS=600`
  - `WEIQ_AUTH_STATE_DIR=/opt/weiq-scraper-stable-api/runtime/auth_sessions`
  - `WEIQ_KEEP_AUTH_STATE_FOR_DEBUG=false`
- 如果后续需要多 worker/多实例，请改为 Redis、Celery、RQ 等外部队列方案。
```

健康检查：

```bash
curl http://127.0.0.1:8080/health
```

### 6.2 提交采集任务

接口：`POST /v1/tasks/crawl`

如果当前为 `browser_worker` 模式且长期登录态无效，接口会直接返回：

```json
{
  "task_id": null,
  "status": "AUTH_REQUIRED",
  "message": "请在 Browser Worker 登录窗口中完成 WEIQ 登录"
}
```

请求示例：

```bash
curl -X POST "http://127.0.0.1:8080/v1/tasks/crawl" \
  -H "Content-Type: application/json" \
  -d '{
    "platform": "weiq",
    "priority": 5,
    "accounts": [
      {"account_id": "客户A", "uid": "123456"},
      {"account_id": "客户B", "uid": "789012"}
    ]
  }'
```

响应示例：

```json
{
  "task_id": "e7be...",
  "status": "PENDING",
  "status_zh": "排队中",
  "progress": 0.0,
  "message": "任务已创建"
}
```

### 6.3 轮询任务状态

接口：`GET /v1/tasks/{task_id}`

### 6.2.1 Browser Worker 登录态接口

- `GET /v1/auth/browser/status`
- `POST /v1/auth/browser/open`
- `POST /v1/auth/browser/check`

这组接口用于网站在不接收 WEIQ 账号密码的前提下，查询远端浏览器登录态、打开远端可视浏览器登录窗口，并在用户手动登录后检查长期 `state.json` 是否已经可复用。

建议轮询间隔：2~3 秒。

关键字段：
- `status`：程序判断字段（英文）。
- `status_zh`：页面展示字段（中文）。
- `progress`：0~1。
- `current_account`：当前账号。
- `blocked_reason`：阻塞原因（如 `AUTH_REQUIRED`）。
- `error_code`：错误码（英文）。
- `error_message_zh`：错误中文解释。
- `auth_waiting`：是否等待登录。
- `resume_requested`：是否收到继续请求。
- `auth_check_passed`：继续前登录校验是否通过。
- `output_dir`：导出目录。
- `export_file`：导出文件路径。

终态判断：
- 成功终态：`SUCCESS`
- 失败终态：`FAILED`
- 取消终态：`CANCELLED`

### 6.4 控制任务（暂停/继续/取消）

- `POST /v1/tasks/{task_id}/pause`
- `POST /v1/tasks/{task_id}/resume`
- `POST /v1/tasks/{task_id}/cancel`

继续接口建议流程：
1. 用户在浏览器完成登录。
2. 前端点击“继续”。
3. 后端调用 `/resume`。
4. 再轮询 `/tasks/{task_id}`，确认从 `BLOCKED_AUTH` 回到 `RUNNING`。

### 6.5 拉取采集结果数据

- `GET /v1/accounts/{uid}/latest`：获取最新快照。
- `GET /v1/accounts/{uid}/changes`：获取变化记录。

典型页面流程：
1. 先查 `latest` 渲染当前值。
2. 再查 `changes` 渲染“最近变化历史”。

### 6.6 网站前端接入示例（JavaScript）

```js
async function startAndTrack(payload) {
  const createRes = await fetch('/api/proxy/v1/tasks/crawl', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  const createData = await createRes.json();
  const taskId = createData.task_id;

  const timer = setInterval(async () => {
    const res = await fetch(`/api/proxy/v1/tasks/${taskId}`);
    const task = await res.json();

    renderStatus(task.status_zh || task.status);
    renderProgress(task.progress || 0);
    renderCurrent(task.current_account || '-');

    if (task.auth_waiting || task.status === 'BLOCKED_AUTH') {
      showAuthNotice('请在浏览器完成登录后点击继续');
    }

    if (['SUCCESS', 'FAILED', 'CANCELLED'].includes(task.status)) {
      clearInterval(timer);
      onTaskFinished(task);
    }
  }, 2500);
}
```

### 6.7 后端代理示例（Node.js）

```js
import express from 'express';
import fetch from 'node-fetch';

const app = express();
app.use(express.json());

const WEIQ_API = 'http://127.0.0.1:8080';

app.post('/api/proxy/v1/tasks/crawl', async (req, res) => {
  const r = await fetch(`${WEIQ_API}/v1/tasks/crawl`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(req.body)
  });
  res.status(r.status).json(await r.json());
});

app.get('/api/proxy/v1/tasks/:taskId', async (req, res) => {
  const r = await fetch(`${WEIQ_API}/v1/tasks/${req.params.taskId}`);
  res.status(r.status).json(await r.json());
});

app.listen(3000);
```

## 7. 常见问题与排查

### 问题 1：点击开始后进度一直 0%

排查顺序：
1. 看任务日志是否出现 `DB_SCHEMA_MISMATCH` 或 `RUNTIME_CRASH`。
2. 看子进程是否仍存活。
3. 看 `heartbeat_at` 是否持续更新。
4. 用“诊断”查看最后错误摘要。

### 问题 2：登录页闪烁、反复跳转

处理策略：
- 等待登录态时任务进入 `BLOCKED_AUTH`；
- 通过 `POST /v1/auth/session/{session_id}/submit` 和 `POST /v1/auth/session/{session_id}/check` 完成当前任务的远端临时登录；
- 登录成功后任务重新入队继续；
- 任务成功、失败、取消后默认删除 `runtime/auth_sessions/{session_id}/`。

### 问题 3：导出文件不在预期目录

检查：
- 任务配置里的 `output_dir`。
- 任务结果里的 `export_file`。
- 如需归档，用“复制到目标目录”。

## 8. 安全与敏感信息

严禁提交到仓库的内容：
- `runtime/auth_sessions/*/storage_state.json`
- `runtime/auth_sessions/*/preview.png`
- `state.json`
- `*.db`
- `accounts.xlsx`, `inputs/*.xlsx`
- `数据导出_*.xlsx`, `latest.xlsx`
- 任何 DSN、token、密码、cookie、私钥

提交前建议执行：

```bash
git status --short
git diff --cached --name-only
git grep -n "WEIQ_DB_DSN\|postgresql://\|password\|token\|state.json" || true
```

## 9. 开发与测试

```bash
python -m py_compile scraper.py scraper_runtime.py cloud_api.py main.py
python -m unittest discover -s tests -p 'test_*.py'
```

## 10. 更新记录

详细更新请查看：[CHANGELOG.md](./CHANGELOG.md)
