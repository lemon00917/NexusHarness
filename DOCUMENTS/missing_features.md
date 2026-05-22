# NexusHarness 缺失功能分析

## 概述

NexusHarness 当前是最小可运行原型（约 500 行代码），具备基础 Agent Harness 功能。但离完整/生产级 Harness 还有差距。

---

## 功能现状

| 功能 | 状态 | 说明 |
|------|------|------|
| **评估框架** | ⚠️ 部分实现 | BenchmarkRunner CLI，支持 6 种评分规则，结果持久化到 JSON，需补充更多 Benchmark 任务 |
| **重试机制** | ✅ 已实现 | RetryToolExecutor 单例，支持指数退避+抖动，4种错误分类策略，可视化重试日志 |
| **流式输出** | ✅ 已实现 | `llm.astream()` + SSE token 事件推送，前端实时显示 |
| **Token 统计** | ✅ 已实现 | 调用次数、input/output tokens、累计成本，支持 CLI 和 Web UI |
| **多会话管理** | ✅ 已实现 | SessionManager 单例，会话持久化到 `conversations/*.json`，支持中断/恢复 |
| **Skill CLI** | ✅ 已实现 | `python harness.py skill [list|install|remove|show|update]`，支持 URL/本地路径安装 |
| **工具注册表** | ✅ 已实现 | 单例 ToolRegistry，支持动态注册/注销/启用/禁用，Web UI 面板 |
| **回放/调试** | ✅ 已实现 | ReplayLogger 单例，录制到 `replays/*.jsonl`，`/api/replay/{session_id}` 端点，`replay.html` 页面 |
| **沙箱隔离** | ⚠️ | 简单 `/tmp/sandbox`，无真正的进程/网络隔离 |
| **前端界面** | ✅ 已实现 | FastAPI + SSE + Web UI，`python harness.py web` 启动 |
| **操作审计** | ✅ 已实现 | approve/reject 持久化到 audit.log，/api/audit 端点 |
| **超时控制** | ⚠️ | 工具执行有 15-30s 超时，但无智能重试 |

---

## 已实现功能详情

### 1. 前端界面 + 流式输出 (✅ 2026-05-17)

**实现方式**:
- CLI: `python harness.py web` 启动 FastAPI 服务器 (port 8000)
- 后端: `web/app.py` — 使用 `llm.astream()` 替代阻塞的 `llm.invoke()`
- 流式: 每个 token 通过 SSE `token` 事件推送
- 前端: `web/static/js/app.js` 处理 `token` 事件，实时追加到显示区域
- UI: `web/templates/index.html` + `web/static/css/style.css`

**改动文件**:
- `microharness/harness.py` — +8行 `web` 子命令
- `web/app.py` — ~15行 astream 替换 invoke
- `web/static/js/app.js` — +15行 token 事件处理
- `web/static/css/style.css` — +5行 流式样式

---

### 2. Token 统计 (✅ 2026-05-17)

**实现方式**:
- 新建 `microharness/token_tracker.py` — `TokenStats` 单例，维护调用记录
- 各 Provider 定价表（USD/M tokens）内置，支持 cost 自动计算
- `harness.py` 的 `agent_node()` 调用后记录
- `web/app.py` 的 `run_harness_async()` 流式结束后记录
- `/api/token_stats` + `/api/token_history` 端点
- 前端 `loadSystemStatus()` 展示调用次数、token 总数、成本

**改动文件**:
- `microharness/token_tracker.py` — 新建
- `microharness/harness.py` — `agent_node()` 记录 usage + 打印统计
- `web/app.py` — `run_harness_async()` 记录 usage + 新增端点
- `web/static/js/app.js` — 展示 token 统计
- `web/static/css/style.css` — 样式

---

### 3. 操作审计日志 (✅ 2026-05-19)

**实现方式**:
- 新建 `microharness/audit.py` — `log_audit()` 写入 `audit.log`，`get_audit_records()` 读取
- `/api/approve` 和 `/api/reject` 调用 `log_audit()` 持久化
- 新增 `/api/audit` 端点返回最近记录
- 记录内容：timestamp、session_id、step、tool、args、approved、operator

**改动文件**:
- `microharness/audit.py` — 新建
- `web/app.py` — `/api/approve`、`/api/reject` 加日志，新增 `/api/audit`

### 4. 回放/调试 (Replay/Debug) (✅ 2026-05-21)

**实现方式**:
- 新建 `microharness/replay_log.py` — `ReplayLogger` 单例，管理录制缓冲，每 10 条自动刷盘
- 录制类型：`agent`（LLM 输入/输出）、`tool_call`（工具名+参数）、`tool_result`（执行结果）、`approval`（审批决定）、`interrupt`、`complete`
- 存储格式：`replays/{session_id}.jsonl`（JSON Lines，每行一条记录）
- `GET /api/replay/{session_id}` — 获取完整回放
- `GET /api/replay/{session_id}/step/{step}` — 获取指定步骤
- `web/templates/replay.html` — 回放 UI，双栏布局（时间线 + 详情），支持上一步/下一步导航
- 前端会话面板每个会话增加 🔍 回放按钮

**API 端点**:
- `GET /api/replay/{session_id}` — 返回 `{"session_id", "records": [...]}`
- `GET /api/replay/{session_id}/step/{step}` — 返回 `{"session_id", "step", "record"}`

**改动文件**:
- `microharness/replay_log.py` — 新建
- `web/app.py` — `run_harness_async()` 集成录制，新增 replay API 端点
- `web/templates/replay.html` — 新建
- `web/templates/index.html` — 增加回放按钮
- `web/static/js/app.js` — `openReplay()` 函数
- `web/static/css/style.css` — 回放按钮样式

### 5. 重试机制 (✅ 2026-05-22)

**实现方式**:
- 新建 `microharness/retry.py` — `RetryPolicy`、`classify_error()`、`RetryToolExecutor` 类
- 4种错误分类：timeout（超时）、network（网络错误）、rate_limit（限流）、default（默认）
- 指数退避 + 随机抖动：`delay = base * (2^attempt) * (0.5~1.0)`
- 写/删操作（ALWAYS_CONFIRM）重试次数更少（2次），延迟更短
- SSE 事件 `retry` 实时推送到前端，界面显示重试状态和错误信息

**API 端点**:
- 无新端点，重试逻辑在 `web/app.py` 的 `run_harness_async` 中内联

**改动文件**:
- `microharness/retry.py` — 新建
- `web/app.py` — 工具执行使用 `RetryToolExecutor`
- `web/static/js/app.js` — `showRetryEvent()` 处理 retry 事件
- `web/static/css/style.css` — 重试状态样式

### 6. 评估框架 (⚠️ 部分实现 2026-05-22)

**实现方式**:
- 新建 `microharness/evaluation.py` — `BenchmarkRunner`、`TaskResult`、`BenchmarkResult` 类
- 6种验证规则：contains、exact、regex、tool_calls、llm_judge、hybrid
- `BenchmarkRunner.run_benchmark()` — 加载 `benchmarks/*.json`，批量执行评分
- `BenchmarkRunner.compare_providers()` — 多 Provider 对比
- 结果持久化到 `benchmark_results/{run_id}_{provider}_{model}.json`

**CLI 用法**:
```bash
python harness.py benchmark                    # 运行所有 benchmark
python harness.py benchmark --category code   # 只运行 code 类
python harness.py benchmark --tasks fibonacci_001  # 指定任务
python harness.py benchmark --providers anthropic deepseek --model claude-sonnet-4-20250514  # 多 Provider 对比
```

**改动文件**:
- `microharness/evaluation.py` — 新建
- `microharness/harness.py` — 增加 `benchmark` 子命令
- `benchmarks/` — 新建目录，包含示例任务

### 7. Skill CLI (✅ 2026-05-20)

**实现方式**:
- 新建 `microharness/skill_cli.py` — CLI 主逻辑
- `harness skill list` — 扫描 `skills/` 目录，解析 SKILL.md 展示列表
- `harness skill install <source>` — 支持 raw URL / GitHub tree URL / 本地路径
- `harness skill remove <name>` — `shutil.rmtree` 删除，触发 reload
- `harness skill show <name>` — 解析 SKILL.md 展示详情
- `harness skill update <name> [source]` — 重新安装覆盖

**安装来源**:
- Raw SKILL.md URL → 直接下载
- GitHub tree URL → 构造 raw URL 后下载
- 本地路径 → 复制到 `skills/{slug}/`

**改动文件**:
- `microharness/skill_cli.py` — 新建
- `microharness/harness.py` — 添加 `skill` 子命令分支
- `microharness/skill_manager.py` — 新增 `remove_skill(name)` 函数

---

## 详细说明

### 1. 评估框架 (Evaluation Framework)

**现状**: 无。

**需要**:
- 标准测试用例集（task → expected outcome）
- 自动评分机制（成功率、响应质量）
- 多 Provider 对比能力

**实现方向**:
```python
# evaluation/benchmarks.py
class Benchmark:
    def run(self, tasks: list[Task]) -> BenchmarkResult
    def score(self, actual: str, expected: str) -> float
```

### 2. 重试机制 (Retry Mechanism)

**现状**: 工具失败直接报错，无重试。

**问题**:
- 网络抖动导致的失败会被当作最终错误
- Agent 无法从错误中恢复

**实现方向**:
```python
# 3-tier retry: fast (1s) → medium (5s) → slow (30s)
# Exponential backoff with jitter
```

### 3. 流式输出 (Streaming) ✅ 已实现

**现状**: 已实现。参见「已实现功能详情 - 前端界面 + 流式输出」。

### 4. Token 统计 (Token Tracking)

**现状**: 无。

**需要**:
- 每次调用记录 input/output tokens
- 计算成本（基于 provider pricing）
- 累计统计面板

### 5. 多会话管理 (Session Management)

**现状**: 单次会话，无法中断。

**需要**:
- Session ID 追踪
- 中断/恢复能力
- 状态持久化（messages, step_count）

### 6. Skill CLI

**现状**: skills 只在启动时从 `skills/` 目录自动发现。

**需要**:
- `microharness install <skill>` — 从 OpenClaw marketplace 安装
- `microharness list` — 列出已安装
- `microharness remove <skill>` — 卸载

### 7. 工具注册表 (Tool Registry)

**现状**: `TOOLS = [list_files, read_file, ...]` 是静态列表。

**需要**:
- 动态注册/注销工具
- 工具版本管理
- Schema 统一管理

### 8. 回放/调试 (Replay/Debug)

**现状**: 无。

**需要**:
- 保存每一步的完整状态快照
- 可视化决策树
- 断点调试能力

### 9. 沙箱隔离 (Sandboxing)

**现状**: 简单的 `/tmp/sandbox` 目录。

**问题**:
- `run_python` 使用 `subprocess.Popen(shell=True)` — 可逃逸
- 无网络限制
- 无资源配额（CPU/内存）

**实现方向**:
- Docker 容器化执行
- seccomp + AppArmor 限制
- 资源 quota

### 10. 前端界面 (Frontend) ✅ 已实现

**现状**: 已实现。参见「已实现功能详情 - 前端界面 + 流式输出」。

---

### 11. 工具注册表 (Tool Registry) ✅ 已实现

**实现方式**:
- 新建 `microharness/tool_registry.py` — `ToolRegistry` 单例类
- 内置工具 + Skill 工具统一管理
- 支持注册/注销/启用/禁用
- Web UI 右侧面板展示工具列表和开关

**API 端点**:
- `GET /api/tools` — 列出所有工具
- `GET /api/tools/{name}` — 获取工具详情
- `GET /api/tools/{name}/schema` — 获取 JSON Schema
- `DELETE /api/tools/{name}` — 注销工具（内置不可删）
- `POST /api/tools/{name}/enable` — 启用工具
- `POST /api/tools/{name}/disable` — 禁用工具

**改动文件**:
- `microharness/tool_registry.py` — 新建
- `microharness/tools.py` — 初始化注册表
- `web/app.py` — 新增工具 API 端点
- `web/templates/index.html` — 工具注册表面板
- `web/static/css/style.css` — 工具列表样式
- `web/static/js/app.js` — loadTools() 函数

---

## 优先级建议

| 优先级 | 功能 | 状态 | 理由 |
|--------|------|------|------|
| **P0** | 前端界面 + 流式输出 | ✅ 已实现 | 立即可用性，展示效果 |
| **P1** | 多会话管理 | ✅ 已实现 | 基础体验提升 |
| **P1** | 操作审计日志 | ✅ 已实现 | 安全合规 |
| **P1** | 回放/调试 | ✅ 已实现 | Agent 决策过程回放 |
| **P2** | 重试机制 | ✅ 已实现 | 稳定性提升 |
| **P2** | Token 统计 | ✅ 已实现 | 成本控制 |
| **P3** | 评估框架 | ⚠️ 部分实现 | BenchmarkRunner CLI，支持 6 种评分规则 |
| **P2** | 工具注册表 | ✅ 已实现 | 运行时工具管理 |

---

## 架构愿景

```
完整 Harness
├── Core
│   ├── Agent Executor      # 运行、暂停、恢复、重试
│   ├── Tool Registry       # 工具发现、描述、版本
│   └── LLM Gateway         # 多 Provider 抽象
├── Safety
│   ├── Guard               # 拦截、审计
│   └── Sandbox             # 进程隔离
├── Memory
│   ├── Short-term          # 会话内上下文
│   └── Long-term           # 跨会话持久化
├── Observability
│   ├── Streaming           # 实时输出
│   ├── Token Tracking      # 成本统计
│   └── Audit Log           # 操作记录
├── Evaluation
│   ├── Benchmarks          # 标准测试
│   └── Regression Suite    # 回归测试
├── Session Manager         # 多会话、中断恢复
└── Skill Marketplace       # 安装、发现、更新
```