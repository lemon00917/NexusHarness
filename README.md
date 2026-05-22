# NexusHarness

[中文版](README_ZH.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Minimal Agent Harness built on LangGraph. Multi-provider, safety-guarded, memory-enabled, with replay debugging and evaluation framework.

## Features

- **Multi-provider support** (9 providers: Anthropic, OpenAI, DeepSeek, Kimi, Qwen, GLM, MiniMax, Xiaomi, Custom)
- **3-tier tool safety guard** (auto-approve / always-confirm / keyword-check)
- **Cross-session long-term memory** (persisted to `memory.json`)
- **Multi-session management** with interrupt/resume capability
- **Execution replay & debugging** — full step-by-step trace recording
- **Retry mechanism** — exponential backoff for transient tool failures
- **Evaluation framework** — benchmark tasks with pass/fail scoring
- **Token cost tracking** — per-call and cumulative cost analysis
- **Web UI** with SSE streaming, real-time progress display
- **Skill marketplace** — install/update/remove tools dynamically

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure API key in .env
#    Edit PROVIDER, API_KEY, MAIN_MODEL

# 3. Run CLI
python harness.py

# 4. Or run Web UI
python harness.py web
# Then open http://localhost:8000
```

## Usage Modes

### CLI Mode

```bash
# Interactive mode
python harness.py
Task: Write a Fibonacci script

# Run benchmark
python harness.py benchmark --category code

# Manage skills
python harness.py skill list
python harness.py skill install https://raw.githubusercontent.com/...
```

### Web UI Mode

```bash
python harness.py web
# Opens http://localhost:8000
```

Features in Web UI:
- Real-time SSE streaming output
- Multi-session management (create/resume/interrupt/delete)
- Execution replay — click 🔍 on any session to see step-by-step trace
- Token statistics panel
- Audit log viewer
- Tool registry with enable/disable

## Architecture

```
NexusHarness
├── Config Layer      config.py — Provider/Model configuration
├── Prompt Layer     prompts.py — System prompt + memory injection
├── Tool Layer       tools.py — 6 built-in tools + skill tools
├── Guard Layer      guard.py — Safety classification + approval
├── Agent Layer      harness.py / web/app.py — LangGraph execution
├── Memory Layer     memory.py — Long-term memory extraction
├── Session Layer    session_manager.py — Multi-session lifecycle
├── Replay Layer     replay_log.py — Execution trace recording
├── Retry Layer      retry.py — Exponential backoff retry
├── Evaluation Layer evaluation.py — Benchmark scoring
└── Token Layer      token_tracker.py — Cost tracking
```

## Tool Safety Levels

| Tool | Level | Behavior |
|------|-------|----------|
| `list_files` | AUTO_APPROVE | Automatically allowed |
| `read_file` | AUTO_APPROVE | Automatically allowed |
| `get_file_info` | AUTO_APPROVE | Automatically allowed |
| `write_file` | ALWAYS_CONFIRM | Always requires human approval |
| `delete_file` | ALWAYS_CONFIRM | Always requires human approval |
| `run_python` | KEYWORD_CHECK | Blocked if dangerous keywords detected |

## Provider Configuration

Edit `.env` to configure your provider:

```bash
# Anthropic (default)
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

# Custom (any OpenAI-compatible API)
PROVIDER=custom
OPENAI_COMPATIBLE_API_KEY=your_key
OPENAI_COMPATIBLE_BASE_URL=https://your-api-endpoint/v1
MAIN_MODEL=your-model
```

## Key Files

| File | Purpose |
|------|---------|
| `harness.py` | CLI entry point |
| `web/app.py` | FastAPI web server |
| `microharness/config.py` | Provider/Model configuration |
| `microharness/tools.py` | Built-in tools |
| `microharness/guard.py` | Safety classification |
| `microharness/memory.py` | Long-term memory |
| `microharness/session_manager.py` | Multi-session management |
| `microharness/replay_log.py` | Replay recording |
| `microharness/retry.py` | Retry mechanism |
| `microharness/evaluation.py` | Benchmark runner |
| `microharness/token_tracker.py` | Token cost tracking |
| `microharness/audit.py` | Audit logging |
| `microharness/tool_registry.py` | Tool registry |
| `microharness/skill_cli.py` | Skill CLI |

## License

MIT License — see [LICENSE](LICENSE)