# WEIQ 数据采集与分析（零基础可用版）

这份文档是给**没有编程基础**的同学写的。
你可以把它当作“照着做就能跑起来”的操作手册。

## 你可以用它做什么

这个项目可以帮助你把一批账号的数据自动采集下来，并自动生成可视化分析面板。

完整流程是：
- 准备账号表（Excel）
- 运行采集程序（自动打开浏览器）
- 得到采集结果（Excel）
- 打开分析看板（图表）

你不需要先学会编程，按本文步骤执行即可。

## 先看这 3 件事

1. 你至少需要有一个 `uid` 列的账号表（Excel）。
2. 第一次运行时会要求你在浏览器里登录 WEIQ，这是正常流程。
3. 如果中途断了，可以用“断点续跑”继续，不会从头全部重跑。

## 项目功能（通俗版）

### 1) 采集引擎（`scraper.py`）
- 自动打开网页并抓取账号指标。
- 支持失败重试、冷却防风控、登录失效后恢复。
- 每条数据会自动带上运行批次和错误信息，便于回溯。

### 2) 分析看板（`main.py`）
- 打开浏览器就能看图表，不需要你写 SQL。
- 支持按 `run_id`（运行批次）筛选。

### 3) API 服务（`cloud_api.py`，可选）
- 给网站或后端接入用。
- 可以创建任务、看进度、取消、继续、拉取结果。
- 如果你只是自己手工跑一次，不用先学 API。

## 目录结构

```text
.
├── scraper.py       # 采集程序（最常用）
├── main.py          # 数据分析看板
├── cloud_api.py     # API 服务（可选）
├── analytics.py     # 变化摘要与质量评分
├── tests/           # 基础测试
├── pyproject.toml
└── README.md
```

## 一、Windows 教程（给零基础）

> 建议使用 Windows Terminal / PowerShell。

### 第 1 步：安装 Python（只做一次）

1. 打开 Python 官网下载 Python 3.12+。
2. 安装时**勾选** `Add python.exe to PATH`。
3. 安装完成后打开 PowerShell，执行：

```powershell
python --version
```

看到版本号（例如 `Python 3.12.x`）说明成功。

### 第 2 步：进入项目目录

假设你把项目放在桌面 `weiq-scraper-stable-api` 文件夹：

```powershell
cd $HOME\Desktop\weiq-scraper-stable-api
```

### 第 3 步：安装项目依赖（只做一次）

```powershell
python -m pip install --upgrade pip
python -m pip install -e .
python -m playwright install chromium --no-shell
```

### 第 4 步：准备账号 Excel

在项目目录放一个 `accounts.xlsx` 文件，至少包含两列：

- `账号ID`：你给账号起的名字（例如 客户A）
- `uid`：账号唯一 ID（必填）

示例：

| 账号ID | uid |
| --- | --- |
| 客户A | 123456 |
| 客户B | 789012 |

### 第 5 步：开始采集

```powershell
python scraper.py
```

说明：
- 第一次会打开浏览器，提示你登录 WEIQ。
- 登录完成后，回到终端按回车继续。
- 采集完成后会生成 `weiq_results.xlsx`。

### 第 6 步：打开分析看板

```powershell
python -m streamlit run main.py
```

终端会显示一个本地地址（通常是 `http://localhost:8501`），浏览器打开即可看图表。

### 常用命令（Windows）

自定义输入和输出目录：

```powershell
python scraper.py --input-excel .\accounts.xlsx --output-dir .\exports
```

禁用断点续跑：

```powershell
python scraper.py --no-resume
```

## 二、Mac 教程（给零基础）

> 建议使用 macOS 自带 Terminal。

### 第 1 步：安装 Python（只做一次）

1. 安装 Python 3.12+（官网安装包或 Homebrew 均可）。
2. 终端执行：

```bash
python3 --version
```

如果你机器是 `python` 命令，也可用 `python --version`。

### 第 2 步：进入项目目录

假设项目在桌面：

```bash
cd ~/Desktop/weiq-scraper-stable-api
```

### 第 3 步：安装项目依赖（只做一次）

```bash
python3 -m pip install --upgrade pip
python3 -m pip install -e .
python3 -m playwright install chromium --no-shell
```

### 第 4 步：准备账号 Excel

在项目目录放置 `accounts.xlsx`，至少包含：

- `账号ID`
- `uid`

### 第 5 步：开始采集

```bash
python3 scraper.py
```

流程与 Windows 一样：
- 第一次会弹浏览器登录
- 登录后回终端按回车
- 完成后输出 `weiq_results.xlsx`

### 第 6 步：打开分析看板

```bash
python3 -m streamlit run main.py
```

打开终端给出的本地地址（通常 `http://localhost:8501`）。

### 常用命令（Mac）

自定义输入和输出目录：

```bash
python3 scraper.py --input-excel ./accounts.xlsx --output-dir ./exports
```

指定历史批次继续：

```bash
python3 scraper.py --run-id run_20260531_120000_ab12cd
```

## 三、最常用参数（看不懂可以先跳过）

```bash
python scraper.py \
  --input-excel ./accounts.xlsx \
  --output-dir ./exports \
  --output-excel weiq_results.xlsx \
  --state-json ./state.json \
  --state-storage ./crawl_state.json \
  --cooldown-every 50 \
  --cooldown-seconds 180 \
  --retry-times 2 \
  --retry-backoff-seconds 3
```

参数解释：
- `--input-excel`：输入账号表
- `--output-dir`：结果输出目录
- `--state-json`：登录态缓存文件
- `--state-storage`：断点续跑状态文件
- `--retry-times`：失败重试次数

## 四、API 使用（可选）

如果你要把采集接入网站/系统，可以启用 API。

### 启动 API

```bash
python -m uvicorn cloud_api:app --host 0.0.0.0 --port 8080
```

健康检查：

```bash
curl http://127.0.0.1:8080/health
```

### API 功能

- `POST /v1/tasks/crawl` 创建任务
- `GET /v1/tasks/{task_id}` 查询状态
- `POST /v1/tasks/{task_id}/cancel` 取消任务
- `POST /v1/tasks/{task_id}/resume` 登录后继续
- `GET /v1/tasks/{task_id}/latest` 获取任务结果
- `GET /v1/tasks/{task_id}/quality` 查看质量评分
- `GET /v1/accounts/{uid}/changes` 查看指标变化摘要

## 五、常见问题（小白版）

### 1) 运行后没反应 / 卡住

先看终端是不是在等你登录或验证码处理。
很多时候不是程序死了，而是等人工处理风控。

### 2) 结果文件在哪里

默认在当前目录：`weiq_results.xlsx`。
如果你用了 `--output-dir`，去那个目录找。

### 3) 中断后如何继续

再次运行同样命令即可，默认会根据 `crawl_state.json` 尝试续跑。

### 4) 看板提示没有数据

检查：
- 是否已经成功生成 `weiq_results.xlsx`
- 是否在正确目录运行 `streamlit`
- 是否设置了错误的 `WEIQ_DATA_FILE`

## 六、运行结果里关键字段是什么意思

每条账号记录里新增了下面几个字段：

- `run_id`：本次运行批次号
- `crawl_time`：采集时间
- `account_status`：该账号采集状态（SUCCESS / FAILED / SKIPPED）
- `error_code`：失败类型代码
- `error_message`：失败中文说明

常见错误码：
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

## 七、安全提醒（很重要）

以下文件不要上传到公开仓库：
- `state.json`
- `crawl_state.json`
- `*.db`
- `accounts.xlsx`
- `weiq_results*.xlsx`

## 八、给第一次使用者的建议

你可以先用 3~5 个账号做一轮小测试，确认流程没问题后，再跑全量任务。
这样最稳、最省时间。
