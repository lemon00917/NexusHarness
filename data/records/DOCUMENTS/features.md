# NexusHarness 功能文档

## 概述

NexusHarness 是一个基于 LangGraph 的轻量级 Agent Harness，支持多模型 Provider、安全守卫、长期记忆等完整功能。

---

## 功能概览

| 功能 | 状态 | 说明 |
|------|------|------|
| 多 Provider 支持 | ✅ 已实现 | Anthropic、OpenAI、DeepSeek、Kimi、Qwen、GLM、MiniMax、Xiaomi、自定义 |
| 工具安全守卫 | ✅ 已实现 | 三级分类：自动放行/强制确认/关键词检测 |
| 跨会话长期记忆 | ✅ 已实现 | 持久化到 `memory.json`，自动注入系统提示 |
| 多会话管理 | ✅ 已实现 | SessionManager，支持中断/恢复 |
| 回放/调试 | ✅ 已实现 | 完整执行步骤追溯 |
| 重试机制 | ✅ 已实现 | 指数退避+抖动，4种错误分类 |
| 评估框架 | ✅ 已实现 | Benchmark 任务评分，8个预设任务全部通过 |
| Token 成本追踪 | ✅ 已实现 | 每次调用和累计成本分析 |
| Web UI | ✅ 已实现 | FastAPI + SSE 流式输出 |
| Skill 市场 | ✅ 已实现 | 动态安装/更新/移除工具 |
| 工具注册表 | ✅ 已实现 | 运行时注册/注销/启用/禁用 |
| 操作审计 | ✅ 已实现 | approve/reject 持久化 |
| 沙箱隔离 | ⚠️ 待改进 | 简单 `/tmp/sandbox`，无进程/网络隔离 |

---

## 已实现功能详情

### 1. Web UI + 流式输出

**技术实现**:
- CLI: `python harness.py web` 启动 FastAPI 服务器 (port 8000)
- 使用 `llm.astream()` 实现流式输出
- 每个 token 通过 SSE 事件推送，前端实时显示

**文件**:
- `web/app.py` — FastAPI 后端
- `web/static/js/app.js` — 前端事件处理
- `web/templates/index.html` — 主页面

### 2. 多会话管理

**技术实现**:
- `SessionManager` 单例管理会话生命周期
- 会话持久化到 `conversations/*.json`
- 支持中断 (`set_interrupted`) 和恢复 (`resumeSession`)

**文件**:
- `microharness/session_manager.py` — 会话管理

### 3. 回放/调试

**技术实现**:
- `ReplayLogger` 单例录制执行步骤到 `replays/*.jsonl`
- 录制类型：agent、tool_call、tool_result、approval、interrupt、complete
- Web UI 双栏布局：时间线 + 详情

**API 端点**:
- `GET /api/replay/{session_id}` — 获取完整回放
- `GET /api/replay/{session_id}/step/{step}` — 获取指定步骤

**文件**:
- `microharness/replay_log.py` — 录制逻辑
- `web/templates/replay.html` — 回放页面

### 4. 重试机制

**技术实现**:
- 4种错误分类：timeout、network、rate_limit、default
- 指数退避：`delay = base * (2^attempt) * (0.5~1.0)`
- 写/删操作重试次数更少（2次）

**文件**:
- `microharness/retry.py` — RetryToolExecutor

### 5. 评估框架

**技术实现**:
- 6种验证规则：contains、exact、regex、tool_calls、llm_judge、hybrid
- `BenchmarkRunner` 批量执行和评分
- 结果持久化到 `benchmark_results/`

**预设 Benchmark 任务** (8个，全部通过):

| ID | 类别 | 任务描述 |
|---|---|---|
| `fibonacci_001` | code | 斐波那契数列到100 |
| `hello_world_001` | code | 打印 Hello World |
| `math_add_001` | reasoning | 计算 123+456+789 |
| `prime_check_001` | reasoning | 判断质数 |
| `code_quality_001` | reasoning | 阶乘递归函数 |
| `file_content_001` | regression | JSON文件创建读取 |
| `multi_file_ops_001` | regression | 多文件操作 |
| `file_ops_001` | tool_use | 写→读→删文件 |

**CLI 用法**:
```bash
python harness.py benchmark                    # 运行所有
python harness.py benchmark --category code  # 按分类
python harness.py benchmark --tasks fibonacci_001  # 指定任务
```

**文件**:
- `microharness/evaluation.py` — 评估引擎
- `benchmarks/` — 任务定义

### 6. Token 成本追踪

**技术实现**:
- `TokenStats` 单例记录每次调用
- 内置各 Provider 定价表
- CLI 和 Web UI 同时展示

**文件**:
- `microharness/token_tracker.py`

### 7. Skill 市场

**技术实现**:
- `harness skill list` — 列出已安装
- `harness skill install <source>` — 安装
- `harness skill remove <name>` — 卸载
- 支持 URL/GitHub/本地路径

**文件**:
- `microharness/skill_cli.py` — CLI
- `microharness/skill_manager.py` — 管理器

### 8. 工具注册表

**技术实现**:
- `ToolRegistry` 单例统一管理内置+Skill工具
- 动态启用/禁用
- Web UI 工具面板

**文件**:
- `microharness/tool_registry.py`
- `microharness/tools.py`

### 9. 操作审计

**技术实现**:
- 所有 approve/reject 持久化到 `audit.log`
- Web UI `/api/audit` 查看历史

**文件**:
- `microharness/audit.py`

---

## 待改进功能

### 沙箱隔离

**现状**: 简单的 `/tmp/sandbox` 目录，`run_python` 使用 `subprocess.Popen(shell=True)`

**问题**:
- 可逃逸到宿主系统
- 无网络限制
- 无资源配额（CPU/内存）

**改进方向**:
- Docker 容器化执行
- seccomp/AppArmor 限制
- 资源 quota

---

## 架构

```
NexusHarness
├── Core
│   ├── Agent Executor      # LangGraph agent 运行
│   ├── Tool Registry       # 工具发现、描述
│   └── LLM Gateway         # 多 Provider 抽象
├── Safety
│   ├── Guard               # 拦截、审计
│   └── Sandbox             # 进程隔离
├── Memory
│   ├── Short-term          # 会话内上下文
│   └── Long-term           # 跨会话持久化
├── Observability
│   ├── Streaming           # SSE 流式输出
│   ├── Token Tracking      # 成本统计
│   └── Audit Log           # 操作记录
├── Evaluation
│   ├── Benchmarks          # 标准测试
│   └── Regression Suite    # 回归测试
├── Session Manager         # 多会话、中断恢复
└── Skill Marketplace       # 工具安装、更新
```