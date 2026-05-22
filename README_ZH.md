# NexusHarness

[English](README.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

基于 LangGraph 的轻量级 Agent Harness，支持多模型 Provider、安全守卫、长期记忆、回放调试和评估框架。

## 功能特性

- **多 Provider 支持**（9个：Anthropic、OpenAI、DeepSeek、Kimi、Qwen、GLM、MiniMax、Xiaomi、自定义）
- **三级工具安全守卫**（自动放行 / 强制确认 / 关键词检测）
- **跨会话长期记忆**（持久化到 `memory.json`）
- **多会话管理** — 支持中断/恢复
- **执行回放与调试** — 完整步骤追溯
- **重试机制** — 指数退避应对临时工具故障
- **评估框架** — Benchmark 任务评分
- **Token 成本追踪** — 每次调用和累计成本分析
- **Web UI** — SSE 流式输出，实时进度显示
- **Skill 市场** — 动态安装/更新/移除工具

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 .env 中的 API Key
#    填写 PROVIDER、API_KEY、MAIN_MODEL

# 3. 运行 CLI
python harness.py

# 4. 或运行 Web UI
python harness.py web
# 然后打开 http://localhost:8000
```

## 使用模式

### CLI 模式

```bash
# 交互模式
python harness.py
Task: 写一个斐波那契数列脚本

# 运行评估
python harness.py benchmark --category code

# 管理 Skill
python harness.py skill list
python harness.py skill install https://raw.githubusercontent.com/...
```

### Web UI 模式

```bash
python harness.py web
# 打开 http://localhost:8000
```

Web UI 功能：
- 实时 SSE 流式输出
- 多会话管理（创建/恢复/中断/删除）
- 执行回放 — 点击任意会话的 🔍 按钮查看步骤追溯
- Token 统计面板
- 审计日志查看
- 工具注册表（启用/禁用）

## 架构

```
NexusHarness
├── 配置层      config.py — Provider/Model 配置
├── 提示层      prompts.py — 系统提示 + 记忆注入
├── 工具层      tools.py — 6 个内置工具 + skill 工具
├── 守卫层      guard.py — 安全分类 + 审批
├── Agent 层    harness.py / web/app.py — LangGraph 执行
├── 记忆层      memory.py — 长期记忆提取
├── 会话层      session_manager.py — 多会话生命周期
├── 回放层      replay_log.py — 执行轨迹录制
├── 重试层      retry.py — 指数退避重试
├── 评估层      evaluation.py — Benchmark 评分
└── Token 层    token_tracker.py — 成本追踪
```

## 工具安全分级

| 工具 | 级别 | 行为 |
|------|------|------|
| `list_files` | AUTO_APPROVE | 自动放行 |
| `read_file` | AUTO_APPROVE | 自动放行 |
| `get_file_info` | AUTO_APPROVE | 自动放行 |
| `write_file` | ALWAYS_CONFIRM | 强制人工确认 |
| `delete_file` | ALWAYS_CONFIRM | 强制人工确认 |
| `run_python` | KEYWORD_CHECK | 含危险关键词时拦截 |

## Provider 配置

编辑 `.env` 配置 Provider：

```bash
# Anthropic（默认）
PROVIDER=anthropic
ANTHROPIC_API_KEY=your_key
MAIN_MODEL=claude-sonnet-4-20250514
MEMORY_MODEL=claude-haiku-4-5-20250501

# OpenAI
PROVIDER=openai
OPENAI_COMPATIBLE_API_KEY=your_key
MAIN_MODEL=gpt-4o

# DeepSeek
PROVIDER=deepseek
OPENAI_COMPATIBLE_API_KEY=your_key
MAIN_MODEL=deepseek-chat

# MiniMax
PROVIDER=minimax
OPENAI_COMPATIBLE_API_KEY=your_key
MAIN_MODEL=abab6.5s-chat

# 自定义（任何 OpenAI 兼容接口）
PROVIDER=custom
OPENAI_COMPATIBLE_API_KEY=your_key
OPENAI_COMPATIBLE_BASE_URL=https://your-api-endpoint/v1
MAIN_MODEL=your-model
```

## 核心文件

| 文件 | 说明 |
|------|------|
| `harness.py` | CLI 入口 |
| `web/app.py` | FastAPI Web 服务器 |
| `microharness/config.py` | Provider/Model 配置 |
| `microharness/tools.py` | 内置工具 |
| `microharness/guard.py` | 安全分类 |
| `microharness/memory.py` | 长期记忆 |
| `microharness/session_manager.py` | 多会话管理 |
| `microharness/replay_log.py` | 回放录制 |
| `microharness/retry.py` | 重试机制 |
| `microharness/evaluation.py` | Benchmark 评分 |
| `microharness/token_tracker.py` | Token 成本追踪 |
| `microharness/audit.py` | 审计日志 |
| `microharness/tool_registry.py` | 工具注册表 |
| `microharness/skill_cli.py` | Skill CLI |

## License

MIT License — 参见 [LICENSE](LICENSE)