# WEIQ 数据采集与变化追踪平台

最近更新时间：**2026-04-12 20:58:41 CST (+0800, Asia/Shanghai)**

## 1. 项目简介

WEIQ 项目用于采集账号数据、记录字段变化，并对网站提供标准 API 接口。项目分为两条使用路径：
- 桌面端（给运营/非技术同学）：图形化操作，支持账号密码或手机号验证码登录，任务可视化，导出可管理。
- API 服务（给网站/后端）：任务提交、任务状态查询、账号最新数据与变化记录读取。

核心目标：
- 把“手工采集 + 手工拷表”改成“任务化采集 + 可追溯存储 + 稳定接口输出”。
- 处理登录失效、任务阻塞、导出管理等高频问题。

## 2. 当前版本能力总览

- 登录阻塞可恢复：登录失效时浏览器保持打开，用户登录后点击“继续”再恢复任务。
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

## 3. 目录结构说明

```text
.
├── desktop_app.py            # 桌面端 UI（Streamlit）
├── scraper.py                # 采集主引擎（CLI + 调度）
├── cloud_api.py              # API 服务（FastAPI）
├── src/weiq_core/            # 核心契约与通用模块
├── tests/                    # 单元测试
├── inputs/                   # 多账号输入表目录（本地使用，不提交仓库）
├── data/                     # 中间数据目录
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

### 第 2 步：启动桌面端

- macOS：双击 `start_desktop.command`
- Windows：双击 `start_desktop.bat`
- 或命令行：

```bash
python -m streamlit run desktop_app.py
```

### 第 3 步：准备账号输入表

在页面中进入“账号表管理”：
- 没有模板时先生成模板；
- 填写账号后上传到 `inputs/` 或直接在页面导入。

建议字段：
- `账号ID`（业务名称）
- `uid`（唯一标识，必填）

### 第 4 步：配置并启动任务

页面“开始采集”区域按顺序设置：
1. 选择输入表。
2. 选择数据库模式（旧库兼容 / 新结构）。
3. 设置导出目录（可自定义）。
4. 点击“开始采集任务”。

### 第 5 步：任务运行中如何看状态

你会看到：
- 状态（中文）
- 进度条
- 当前处理账号
- 最近日志
- 异常诊断

关键状态说明：
- `运行中`：正常采集。
- `等待登录`：登录失效或风控，需要你在浏览器完成登录。
- `已暂停`：人为暂停。
- `失败`：任务已终止，查看错误码和日志。
- `成功`：任务完成，可导出或供 API 读取。

### 第 6 步：登录失效处理（重点）

当页面提示登录失效时：
1. 浏览器窗口会保持打开（不会自动关闭）。
2. 你在浏览器里完成登录。
3. 回到桌面端点击“登录后继续（手动）”。
4. 系统先校验登录态，通过后恢复任务。

### 第 7 步：导出与归档

任务完成后：
- 页面显示本次导出文件路径。
- 可输入任意目标目录，点击“复制导出文件到目标目录”。
- 原始导出保留，不覆盖历史。

## 5. 技术用户 CLI 教程

### 5.1 直接运行

```bash
python scraper.py
```

### 5.2 指定输入表与导出目录

```bash
python scraper.py \
  --input-excel /绝对路径/accounts.xlsx \
  --output-dir /绝对路径/exports
```

### 5.3 数据库模式

```bash
# 旧库兼容模式
python scraper.py --schema-mode legacy

# 完整新结构模式
python scraper.py --schema-mode full
```

### 5.4 周任务调度（示例）

```bash
python scraper.py --weekly --day-of-week mon --hour 3 --minute 0 --run-immediately
```

### 5.5 认证等级探针（4样本验收）

```bash
python scraper.py --probe-verify
```

说明：
- 默认探针样本：`2115314532,6557986019,5099051423,7331622139`
- 可自定义：

```bash
python scraper.py --probe-verify --probe-uids 2115314532,6557986019
```

探针会输出每个 uid 的：
- `认证等级`（`黄V` / `橙V` / `金V` / `无认证` / `unknown`）
- `证据来源`（例如 `dom_svg_exact` / `dom_svg_absent` / `dom_svg_unknown` / `api`）
- `线索预览`（`svg` 片段、`fill` 组合或接口字段片段）

## 6. API 接入网站详细教程

本节给网站开发同学，按“提交任务 -> 轮询 -> 控制 -> 拉取数据”落地。

### 6.1 启动 API 服务

```bash
python -m uvicorn cloud_api:app --host 0.0.0.0 --port 8080 --workers 1

说明：
- 当前云端 API 依赖进程内任务队列，只支持单 worker 进程模式。
- 如果后续需要多 worker/多实例，请改为 Redis、Celery、RQ 等外部队列方案。
```

健康检查：

```bash
curl http://127.0.0.1:8080/health
```

### 6.2 提交采集任务

接口：`POST /v1/tasks/crawl`

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
- 等待登录态时采集流程停止导航；
- 手动点击继续后才恢复；
- 若仍失败，检查网络与风控并重试。

### 问题 3：导出文件不在预期目录

检查：
- 任务配置里的 `output_dir`。
- 任务结果里的 `export_file`。
- 如需归档，用“复制到目标目录”。

## 8. 安全与敏感信息

严禁提交到仓库的内容：
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
python -m py_compile scraper.py desktop_app.py cloud_api.py src/weiq_core/contracts.py
python -m unittest discover -s tests -p 'test_*.py'
```

## 10. 更新记录

详细更新请查看：[CHANGELOG.md](./CHANGELOG.md)
