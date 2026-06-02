# 更新日志

本项目遵循语义化版本（SemVer），并参考 Keep a Changelog 维护。

## [Unreleased]

### Changed（变更）
- 微博认证等级识别主逻辑改为读取昵称右侧认证 `svg` 的 `path fill` 组合，不再依赖截图取色或模糊样式猜测。
- 认证等级判定顺序收敛为：`昵称右侧认证 svg 是否存在` -> `svg 内部 path fill 精确映射` -> `unknown`，不再以视觉颜色估计作为主判断链路。

### Fixed（修复）
- 修复认证账号频繁被识别为 `unknown` 的问题。
- 修复橙V、黄V、金V因颜色近似而互相误判的问题。
- 修复部分账号被误判为 `无认证` 的问题，改为先判断昵称行认证 `svg` 是否存在。
- 修复 `#FFF` 标准化为 `#FFFFFF` 后与映射表不一致，导致真实认证 `svg` 无法命中等级映射的问题。
- 修复最终已验真映射：
  - `#FFFFFF / #F6CA45 / #FFFFFF` -> `黄V`
  - `#FFFFFF / #FF6C00 / #FFFFFF` -> `橙V`
  - `#FEFF78 / #CD3620 / #FEFF78` -> `金V`

## [v1.2.0] - 2026-04-12

发布时间：**2026-04-12 20:58:41 CST (+0800, Asia/Shanghai)**

### Added（新增）
- 新增桌面端“多账号表运行”能力：支持从 `inputs/*.xlsx` 选择不同来源表执行任务。
- 新增导出目录配置能力：任务启动前可指定 `output_dir`，并支持自动创建目录。
- 新增导出后文件管理：支持将本次导出文件复制到任意目标目录，不影响原始文件。
- 新增任务可观测字段（桌面端与 API 同步）：
  - `status_zh`
  - `error_message_zh`
  - `output_dir`
  - `export_file`
  - `auth_waiting`
  - `resume_requested`
  - `auth_check_passed`
- 新增登录等待阶段中文引导文案，降低非技术用户操作门槛。
- 新增按键问题排查入口（关键字扫描），用于定位 `key/keyboard/快捷键` 相关异常。

### Changed（变更）
- 登录恢复机制从“自动恢复”改为“手动点击继续后校验恢复”：
  - 登录失效后浏览器窗口保持打开。
  - 用户完成登录后必须点击“继续”。
  - 系统校验通过才恢复采集。
- 登录等待阶段采集导航行为收敛为“静默等待”，避免登录页面闪烁或被任务抢占。
- 导出命名策略升级：按来源表 + 时间戳命名，避免同日重复任务覆盖历史文件。
- 任务状态中文化增强：前端可见文案尽可能中文，错误码保留英文用于定位。
- 项目文档结构重写：README 增加面向非技术用户的分步教程及 API 网站接入教程。

### Fixed（修复）
- 修复“进度条长期 0% 且界面显示运行中”的假运行问题：
  - 启动阶段异常时可回写失败状态，避免状态滞留 `RUNNING`。
  - 旧库缺字段导致插入失败时，给出可读错误并支持兼容策略。
- 修复登录阻塞时浏览器被过早关闭问题，保证用户可在窗口内完成重新登录。
- 修复登录恢复阶段误触发采集跳转导致页面闪烁的问题。
- 修复导航上下文类异常（如 `Execution context was destroyed`）直接误判登录失败的问题，改为优先可恢复重试。
- 修复桌面端部分过时参数告警（`use_container_width` -> `width="stretch"`）。

### API 兼容与影响
- 任务状态查询接口新增字段（向后兼容）：
  - `status_zh`
  - `error_message_zh`
  - `output_dir`
  - `auth_waiting`
  - `resume_requested`
  - `auth_check_passed`
- 保持错误码英文语义不变（如 `AUTH_REQUIRED`、`DB_SCHEMA_MISMATCH`），前端建议“中文解释 + 英文错误码”展示。
- 控制接口语义保持不变：
  - `POST /v1/tasks/{task_id}/pause`
  - `POST /v1/tasks/{task_id}/resume`
  - `POST /v1/tasks/{task_id}/cancel`

### Security（安全）
- 加强 `.gitignore` 敏感文件规则，避免提交登录态、业务数据和凭证。
- 明确禁止提交：
  - `state.json`
  - `*.db`
  - `accounts.xlsx`、`inputs/*.xlsx`
  - `数据导出_*.xlsx`、`latest.xlsx`
  - `.runtime/`、`.runtime_api/`
  - `*.env*`、`*.pem`、`*.key`
- 文档补充发布前敏感信息自检命令与检查清单。

### 文档更新
- 重写 `README.md`：
  - 项目定位与架构说明
  - 桌面端详细使用步骤
  - CLI 使用教程
  - API 接入网站教程（提交/轮询/控制/取数 + 前后端示例）
  - 常见问题排查
  - 安全规范
- 增强发布说明可执行性，降低交接和二次部署成本。

### 发布前验收建议
- [ ] 登录失效后，浏览器保持打开，手动继续可恢复任务。
- [ ] 任务运行期间进度、日志、当前账号持续刷新。
- [ ] 自定义导出目录写入成功，复制导出文件成功。
- [ ] 同日多次运行历史导出不被覆盖。
- [ ] API 轮询字段完整返回，网站前端可正常展示。
- [ ] `python -m unittest discover -s tests -p 'test_*.py'` 通过。
- [ ] Git 暂存区不含敏感文件与凭证.
