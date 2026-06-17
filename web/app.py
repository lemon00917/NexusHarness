"""
NexusHarness Web API
====================
FastAPI backend for the NexusHarness web interface.

Provides:
- POST /api/run — Execute agent task, returns SSE stream
- GET /api/memory — Get memory records
- GET /api/skills — Get installed skills
- POST /api/approve — Approve pending operation
- POST /api/reject — Reject pending operation
"""

import asyncio
import json
import re
import sys
import os
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, Request, UploadFile, Form
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from microharness import config as config_module
from microharness.config.config import get_config, save_config, validate
from microharness.config.config import PROVIDER, MAIN_MODEL, MEMORY_MODEL, MAX_STEPS, get_llm
from microharness.agent.retry import get_retry_executor
from microharness.agent.harness import HarnessState, build_harness
from microharness.agent.guard import should_confirm, SKILL_SAFETY_LEVELS, register_skill_safety_levels
from microharness.agent.tools import BUILTIN_SAFETY
from microharness.memory.memory import load_memories, extract_and_save_memory
from microharness.memory.replay_log import get_replay_logger, load_replay_from_disk
from microharness.memory.session_manager import get_session_manager, SessionState
from microharness.skills.skill_manager import load_skills, get_skill_safety_map, get_skills
from microharness.observability.token_tracker import token_stats, get_cost
from microharness.observability.audit import log_audit, get_audit_records
from microharness.observability.evaluation import BenchmarkRunner, print_benchmark_result
from microharness.config.prompts import get_system_prompt
from microharness.config.prompt_config import (
    load_prompt_config,
    save_prompt_config,
    validate_prompt_config,
    get_intent_templates,
    delete_intent,
)
from microharness.rag.rag import rag
from microharness.rag.rag_config import load_config, save_config, RAGConfig
from microharness.rag.document_parser import parse_document
from microharness.rag.template_binding import TwoStageBinder
from microharness.observability.logger import rag_logger
from microharness.rag.template_binding_v2 import ThreeStageBinder
from microharness.agent.tools import TOOLS as TOOLS

# Ensure utf-8 output
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

app = FastAPI(title="NexusHarness", version="0.1.0")

# Project root for resolving relative paths
PROJECT_ROOT = Path(__file__).parent.parent

# Load RAG index at startup (graceful fallback if chromadb unavailable)
try:
    rag.load_index()
except Exception as e:
    print(f"[Startup] RAG index load skipped: {e}")
    print("[Startup] RAG features (knowledge base, record filter) will be unavailable")

# Mount static files
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Templates
templates_dir = Path(__file__).parent / "templates"


# Per-session approval tracking (approval_id = f"{session_id}_{step}")
pending_approvals: dict[str, dict] = {}
approval_results: dict[str, bool] = {}

disabled_skills: set[str] = set()  # Track disabled skill names


def get_active_tools():
    """Return tools from registry filtered by disabled_skills."""
    from microharness.agent.tool_registry import get_registry
    registry = get_registry()
    return [t for t in registry.list(include_disabled=False) if t.name not in disabled_skills]


# ──────────────────────────────────────────────────
# Utility functions
# ──────────────────────────────────────────────────

def sse_event(event_type: str, data: dict) -> bytes:
    """Create an SSE event as bytes with proper JSON encoding."""
    import json
    from sse_starlette.event import ServerSentEvent
    json_str = json.dumps(data, ensure_ascii=False)
    event = ServerSentEvent(data=json_str, event=event_type, sep="\r\n")
    return event.encode()


async def event_stream(session_id: str, task: str):
    """Run the harness and yield SSE events."""
    global pending_approvals, approval_results

    session_manager = get_session_manager()

    # Get or create session
    session = session_manager.get_session(session_id)
    if not session:
        session = session_manager.create_session(task)

    # Check if resuming an interrupted session
    resume_state = None
    if session.get("harness_state", {}).get("step_count", 0) > 0 and session.get("status") == "interrupted":
        resume_state = session["harness_state"]
        # Clear interrupted flag before resuming
        session_manager.clear_interrupted(session_id)
        session_manager.set_status(session_id, "active")

    yield sse_event("start", {"session_id": session_id, "task": task, "resuming": resume_state is not None})

    # Initialize harness
    try:
        validate()
    except Exception as e:
        yield sse_event("error", {"message": f"Config error: {e}"})
        return

    # Load skills
    try:
        load_skills()
        register_skill_safety_levels(get_skill_safety_map())
    except Exception as e:
        yield sse_event("error", {"message": f"Skill loading error: {e}"})
        return

    # Use resume_state if available, otherwise create init state with existing messages or fresh
    if resume_state:
        init_state = resume_state
    else:
        # Get existing messages from session history, or start fresh
        existing_messages = session.get("harness_state", {}).get("messages", [])
        if existing_messages:
            # Append new task to existing conversation history
            init_state = {
                "messages": existing_messages + [{"type": "human", "content": task}],
                "step_count": 0,
                "approved": True,
            }
        else:
            # No history, start fresh
            init_state = {
                "messages": [{"type": "human", "content": task}],
                "step_count": 0,
                "approved": True,
            }

    # Run harness using our simplified run_harness_async
    try:
        async for chunk in run_harness_async(None, init_state, session_id, task):
            yield chunk
    except Exception as e:
        yield sse_event("error", {"message": str(e)})


async def run_harness_async(harness, init_state: HarnessState, session_id: str, task: str):
    """Run harness with event streaming."""
    global pending_approvals, approval_results

    session_manager = get_session_manager()
    replay_logger = get_replay_logger()
    loop = asyncio.get_event_loop()
    state = init_state.copy()

    # Start replay recording
    replay_logger.start_session(session_id)

    for step in range(1, MAX_STEPS + 1):
        step_start_ms = int(time.time() * 1000)

        # Check interrupt signal
        if session_manager.is_interrupted(session_id):
            session_manager.clear_interrupted(session_id)
            session_manager.update_session(session_id, state)
            session_manager.set_status(session_id, "interrupted")
            replay_logger.log_interrupt(session_id, step)
            replay_logger.flush(session_id)
            yield sse_event("interrupted", {"session_id": session_id})
            return

        yield sse_event("step", {"step": step, "type": "thinking", "resuming": state["step_count"] > 0})

        # Agent thinking
        try:
            from langchain_core.messages import HumanMessage, SystemMessage

            system = SystemMessage(content=get_system_prompt(task))
            messages = [system] + state["messages"]

            llm = get_llm(MAIN_MODEL).bind_tools(get_active_tools())

            agent_start_ms = int(time.time() * 1000)

            if PROVIDER == "anthropic":
                full_response_content = ""
                last_chunk = None
                all_tool_calls = []
                async for chunk in llm.astream(messages):
                    last_chunk = chunk
                    if hasattr(chunk, "content") and chunk.content:
                        full_response_content += chunk.content
                        yield sse_event("token", {"content": chunk.content})
                    if hasattr(chunk, "additional_kwargs") and chunk.additional_kwargs:
                        tc = chunk.additional_kwargs.get("tool_calls", [])
                        if tc:
                            all_tool_calls.extend(tc)

                from langchain_core.messages import AIMessage
                response = AIMessage(content=full_response_content)
                if all_tool_calls:
                    # Ensure each tool_call has an id (Anthropic streaming may not include it)
                    for i, tc in enumerate(all_tool_calls):
                        if "id" not in tc:
                            tc["id"] = f"tool_{step}_{i}"
                    response.tool_calls = all_tool_calls

                elapsed_ms = int(time.time() * 1000) - agent_start_ms

                if last_chunk and hasattr(last_chunk, "usage_metadata") and last_chunk.usage_metadata:
                    usage = last_chunk.usage_metadata or {}
                    input_tokens = usage.get("input_tokens") or 0
                    output_tokens = usage.get("output_tokens") or 0
                    cost = get_cost(PROVIDER, MAIN_MODEL, input_tokens, output_tokens)
                    token_stats.record(PROVIDER, MAIN_MODEL, input_tokens, output_tokens, cost)
                    yield sse_event("token_stats", {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cost": cost,
                    })
            else:
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(None, llm.invoke, messages)

                elapsed_ms = int(time.time() * 1000) - agent_start_ms

                if hasattr(response, "content") and response.content:
                    for char in response.content:
                        yield sse_event("token", {"content": char})
                        await asyncio.sleep(0.01)

                if hasattr(response, "usage_metadata") and response.usage_metadata:
                    usage = response.usage_metadata or {}
                    input_tokens = usage.get("input_tokens") or 0
                    output_tokens = usage.get("output_tokens") or 0
                    cost = get_cost(PROVIDER, MAIN_MODEL, input_tokens, output_tokens)
                    token_stats.record(PROVIDER, MAIN_MODEL, input_tokens, output_tokens, cost)
                    yield sse_event("token_stats", {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cost": cost,
                    })

            # Serialize LLM input/output for replay
            llm_input_serialized = []
            for m in messages:
                if isinstance(m, dict):
                    llm_input_serialized.append({"type": m.get("type", "unknown"), "content": str(m.get("content", ""))})
                else:
                    llm_input_serialized.append({"type": m.type, "content": str(m.content) if hasattr(m, 'content') else ''})
            llm_output_serialized = {
                "content": str(response.content) if hasattr(response, 'content') else '',
                "tool_calls": getattr(response, 'tool_calls', None) or []
            }

            state["messages"] = state["messages"] + [response]

            # Log agent step to replay
            replay_logger.log_agent(session_id, step, llm_input_serialized, llm_output_serialized, elapsed_ms)

            if not (hasattr(response, "tool_calls") and response.tool_calls):
                yield sse_event("complete", {"message": "No more tool calls, task done"})
                try:
                    session_manager.set_status(session_id, "completed")
                    summary = extract_and_save_memory(state["messages"], task)
                    yield sse_event("memory_saved", {"summary": summary})
                except Exception as e:
                    print(f"Memory save error: {e}")
                replay_logger.log_complete(session_id, step)
                replay_logger.flush(session_id)
                return

            last_call = response.tool_calls[-1]
            tool_name = last_call["name"]
            tool_args = last_call["args"]

            yield sse_event("tool_call", {
                "step": step,
                "tool": tool_name,
                "args": tool_args,
            })

            # Log tool call to replay
            replay_logger.log_tool_call(session_id, step, tool_name, tool_args, 0)

            # Guard check
            approval_decision = "approved"
            if should_confirm(tool_name, tool_args):
                if tool_name in SKILL_SAFETY_LEVELS:
                    safety = SKILL_SAFETY_LEVELS[tool_name]
                    if safety == "AUTO_APPROVE":
                        yield sse_event("tool_approved", {"tool": tool_name, "reason": "auto_approve"})
                        approval_decision = "auto_approve"
                        replay_logger.log_approval(session_id, step, tool_name, "auto_approve")
                    else:
                        approval_id = f"{session_id}_{step}"
                        pending_approvals[approval_id] = {
                            "session_id": session_id, "step": step,
                            "tool": tool_name, "args": tool_args,
                        }
                        yield sse_event("approval_required", {
                            "approval_id": approval_id, "tool": tool_name, "args": tool_args,
                        })
                        while approval_id in pending_approvals:
                            await asyncio.sleep(0.2)
                        result = approval_results.pop(approval_id, None)
                        if result == False:
                            yield sse_event("tool_rejected", {"tool": tool_name})
                            approval_decision = "rejected"
                            replay_logger.log_approval(session_id, step, tool_name, "rejected")
                            replay_logger.flush(session_id)
                            return
                        approval_decision = "approved"
                        replay_logger.log_approval(session_id, step, tool_name, "approved")
                        yield sse_event("tool_approved", {"tool": tool_name})
                else:
                    approval_id = f"{session_id}_{step}"
                    pending_approvals[approval_id] = {
                        "session_id": session_id, "step": step,
                        "tool": tool_name, "args": tool_args,
                    }
                    yield sse_event("approval_required", {
                        "approval_id": approval_id, "tool": tool_name, "args": tool_args,
                    })
                    while approval_id in pending_approvals:
                        await asyncio.sleep(0.2)
                    result = approval_results.pop(approval_id, None)
                    if result == False:
                        yield sse_event("tool_rejected", {"tool": tool_name})
                        approval_decision = "rejected"
                        replay_logger.log_approval(session_id, step, tool_name, "rejected")
                        replay_logger.flush(session_id)
                        return
                    approval_decision = "approved"
                    replay_logger.log_approval(session_id, step, tool_name, "approved")
                    yield sse_event("tool_approved", {"tool": tool_name})
            else:
                yield sse_event("tool_approved", {"tool": tool_name, "reason": "auto_approve"})
                approval_decision = "auto_approve"
                replay_logger.log_approval(session_id, step, tool_name, "auto_approve")

            from langchain_core.messages import ToolMessage
            tool = next((t for t in get_active_tools() if t.name == tool_name), None)
            tool_res = ""
            tool_result_start = int(time.time() * 1000)
            if tool:
                try:
                    retry_executor = get_retry_executor()
                    retry_events = []

                    def on_retry(attempt, max_retries, delay, error, done):
                        retry_events.append({
                            "tool": tool_name,
                            "attempt": attempt,
                            "max_retries": max_retries,
                            "delay": delay,
                            "error": error,
                            "done": done,
                        })

                    result = retry_executor._call_tool_with_retry(
                        tool, tool_args, retry_executor._get_policy(tool_name), on_retry=on_retry
                    )

                    # Send collected retry events
                    for evt in retry_events:
                        yield sse_event("retry", evt)
                    tool_res = result
                    yield sse_event("tool_result", {
                        "step": step,
                        "tool": tool_name,
                        "result": str(tool_res)[:500],
                        "type": "tool_result",
                    })
                except Exception as e:
                    tool_res = f"Error: {e}"
                    yield sse_event("tool_result", {
                        "step": step,
                        "tool": tool_name,
                        "result": str(tool_res)[:500],
                        "type": "tool_result",
                    })
            tool_elapsed_ms = int(time.time() * 1000) - tool_result_start

            replay_logger.log_tool_result(session_id, step, tool_name, str(tool_res), tool_elapsed_ms)

            tool_msg = ToolMessage(
                content=str(tool_res),
                tool_call_id=last_call.get("id", ""),
                name=tool_name
            )
            state["messages"] = state["messages"] + [tool_msg]

            session_manager.update_session(session_id, state)

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            yield sse_event("error", {"message": f"Step {step}: {str(e)}, trace: {tb[:500]}"})
            replay_logger.flush(session_id)
            return

    yield sse_event("complete", {"message": "Max steps reached"})
    try:
        session_manager.set_status(session_id, "completed")
        summary = extract_and_save_memory(state["messages"], task)
        yield sse_event("memory_saved", {"summary": summary})
    except Exception as e:
        print(f"Memory save error: {e}")
    replay_logger.log_complete(session_id, MAX_STEPS)
    replay_logger.flush(session_id)


# ──────────────────────────────────────────────────
# API Routes
# ──────────────────────────────────────────────────

@app.get("/")
async def root():
    """Serve the main HTML page."""
    html_path = templates_dir / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return {"message": "NexusHarness API. Go to /docs for API documentation."}

@app.get("/templates/replay.html")
async def serve_replay_page(session_id: str = ""):
    """Serve the replay page."""
    html_path = templates_dir / "replay.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return {"error": "replay.html not found"}

@app.get("/templates/benchmark.html")
async def serve_benchmark_page():
    """Serve the benchmark page."""
    html_path = templates_dir / "benchmark.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return {"error": "benchmark.html not found"}


@app.get("/templates/rag.html")
async def serve_rag_page():
    """Serve the RAG knowledge base page."""
    html_path = templates_dir / "rag.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return {"error": "rag.html not found"}


@app.get("/templates/rag_config.html")
async def serve_rag_config_page():
    """Serve the RAG configuration page."""
    html_path = templates_dir / "rag_config.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return {"error": "rag_config.html not found"}


@app.get("/rag_config")
async def redirect_to_rag_config():
    """Redirect /rag_config to the template."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/templates/rag_config.html")


@app.get("/templates/prompt_config.html")
async def serve_prompt_config_page():
    """Serve the prompt configuration page."""
    html_path = templates_dir / "prompt_config.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return {"error": "prompt_config.html not found"}


@app.get("/templates/medical_filter.html")
async def serve_medical_filter_page():
    """Serve the medical record filter page."""
    html_path = templates_dir / "medical_filter.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return {"error": "medical_filter.html not found"}


@app.get("/templates/medical_config.html")
async def serve_medical_config_page():
    """Serve the medical catalog config page."""
    html_path = templates_dir / "medical_config.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return {"error": "medical_config.html not found"}


@app.get("/medical-filter")
async def redirect_to_medical_filter():
    """Convenience redirect to the medical filter page."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/templates/medical_filter.html")


@app.get("/templates/binding.html")
async def serve_binding_page():
    """Serve the binding page."""
    html_path = templates_dir / "binding.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return {"error": "binding.html not found"}


@app.get("/api/run")
async def run_task_get(session_id: str = "default", task: str = ""):
    """
    Run an agent task with SSE streaming.
    Query params: session_id, task
    """
    if not task:
        return {"error": "task is required"}

    return EventSourceResponse(event_stream(session_id, task))


@app.get("/api/memory")
async def get_memory():
    """Get all memory records."""
    memories = load_memories()
    return {"memories": memories[-20:]}


@app.delete("/api/memory")
async def clear_memory():
    """Clear all long-term memory records."""
    from microharness.memory.memory import clear_memories
    cleared = clear_memories()
    return {"status": "cleared" if cleared else "failed"}


@app.delete("/api/audit")
async def clear_audit():
    """Clear all audit log records."""
    from pathlib import Path
    audit_file = Path(__file__).parent.parent / "audit.log"
    if audit_file.exists():
        audit_file.write_text("", encoding="utf-8")
    return {"status": "cleared"}


@app.delete("/api/conversations")
async def clear_conversations():
    """Delete all conversation session files."""
    conv_dir = Path(__file__).parent.parent / "conversations"
    if conv_dir.exists():
        for f in conv_dir.glob("session_*.json"):
            f.unlink()
    return {"status": "cleared"}


@app.get("/api/skills")
async def get_skills_list():
    """Get all installed skills."""
    from microharness.skills.skill_manager import get_skill_safety_map
    skills = get_skills()
    safety_map = get_skill_safety_map()

    return {
        "skills": [
            {
                "name": s.name,
                "description": s.description[:100] if s.description else "",
                "safety": safety_map.get(s.name, "KEYWORD_CHECK"),
                "enabled": s.name not in disabled_skills,
            }
            for s in skills
        ]
    }


@app.patch("/api/skills/{skill_name}")
async def toggle_skill(skill_name: str, request: Request):
    """Enable or disable a skill by name."""
    data = await request.json()
    enabled = data.get("enabled", True)

    skills = get_skills()
    skill_names = [s.name for s in skills]

    if skill_name not in skill_names:
        return {"error": f"Skill '{skill_name}' not found"}

    if enabled:
        disabled_skills.discard(skill_name)
    else:
        disabled_skills.add(skill_name)

    return {"name": skill_name, "enabled": enabled}


@app.post("/api/approve")
async def approve_operation(request: Request):
    """Approve a pending operation."""
    data = await request.json()
    approval_id = data.get("approval_id", "")

    if approval_id in pending_approvals:
        entry = pending_approvals[approval_id]
        log_audit(
            session_id=entry["session_id"],
            step=entry["step"],
            tool=entry["tool"],
            args=entry["args"],
            approved=True,
        )
        approval_results[approval_id] = True
        del pending_approvals[approval_id]
        return {"status": "approved"}

    return {"status": "unknown", "message": f"Approval {approval_id} not found"}


@app.post("/api/reject")
async def reject_operation(request: Request):
    """Reject a pending operation."""
    data = await request.json()
    approval_id = data.get("approval_id", "")

    if approval_id in pending_approvals:
        entry = pending_approvals[approval_id]
        log_audit(
            session_id=entry["session_id"],
            step=entry["step"],
            tool=entry["tool"],
            args=entry["args"],
            approved=False,
        )
        approval_results[approval_id] = False
        del pending_approvals[approval_id]
        return {"status": "rejected"}

    return {"status": "unknown", "message": f"Approval {approval_id} not found"}


@app.get("/api/audit")
async def get_audit_log(limit: int = 50):
    """Get recent audit log records."""
    records = get_audit_records(limit=limit)
    return {"records": records}


@app.get("/api/status")
async def get_status():
    """Get system status."""
    stats = token_stats.get_summary()
    return {
        "provider": PROVIDER,
        "main_model": MAIN_MODEL,
        "memory_model": MEMORY_MODEL,
        "max_steps": MAX_STEPS,
        "pending_approvals": len(pending_approvals),
        "total_calls": stats["total_calls"],
        "total_input_tokens": stats["total_input_tokens"],
        "total_output_tokens": stats["total_output_tokens"],
        "total_cost_usd": stats["total_cost_usd"],
    }


@app.get("/api/token_stats")
async def get_token_stats():
    """Get token usage statistics."""
    return token_stats.get_summary()


@app.get("/api/token_history")
async def get_token_history():
    """Get per-call token history."""
    return {"history": token_stats.get_history()}


@app.get("/api/config")
async def get_system_config():
    """Get current system configuration."""
    return get_config()


@app.post("/api/config")
async def update_system_config(request: Request):
    """Update system configuration (persisted to config.json)."""
    data = await request.json()

    # Validate max_steps
    max_steps = data.get("max_steps", MAX_STEPS)
    if not isinstance(max_steps, int) or max_steps < 1 or max_steps > 50:
        return {"error": "max_steps must be between 1 and 50"}

    # Validate provider
    provider = data.get("provider", PROVIDER).lower()
    valid_providers = ["anthropic", "openai", "deepseek", "kimi", "minimax", "qwen", "glm", "xiaomi"]
    if provider not in valid_providers:
        return {"error": f"provider must be one of: {valid_providers}"}

    # Save to config.json
    save_config({
        "provider": provider,
        "main_model": data.get("main_model", MAIN_MODEL),
        "memory_model": data.get("memory_model", MEMORY_MODEL),
        "max_steps": max_steps,
    })

    return {"status": "saved", "config": get_config()}


@app.post("/api/model/switch")
async def switch_model_endpoint(request: Request):
    """Switch model at runtime, auto-match memory model."""
    data = await request.json()
    provider = data.get("provider", PROVIDER)
    model = data.get("model", MAIN_MODEL)
    from microharness.config.config import switch_model, set_runtime_memory_model
    result = switch_model(provider, model)
    if provider == "ollama":
        set_runtime_memory_model(model)
    return result


# ──────────────────────────────────────────────────
# Conversation Persistence
# ──────────────────────────────────────────────────

@app.get("/api/conversations")
async def list_conversations():
    """List all saved conversations (summary only)."""
    conv_dir = Path(__file__).parent.parent / "conversations"
    conv_dir.mkdir(exist_ok=True)

    conversations = []
    for f in sorted(conv_dir.glob("*.json"), key=lambda x: -x.stat().st_mtime):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            conversations.append({
                "session_id": data.get("session_id", f.stem),
                "title": data.get("title", "未命名会话"),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
                "message_count": len(data.get("messages", [])),
            })
        except Exception:
            continue

    return {"conversations": conversations[:50]}


@app.get("/api/conversations/{session_id}")
async def get_conversation(session_id: str):
    """Get a specific conversation by session_id."""
    conv_dir = Path(__file__).parent.parent / "conversations"
    conv_file = conv_dir / f"{session_id}.json"

    if not conv_file.exists():
        return {"error": "Conversation not found"}, 404

    try:
        data = json.loads(conv_file.read_text(encoding="utf-8"))
        return {"conversation": data}
    except Exception:
        return {"error": "Failed to read conversation"}, 500


@app.post("/api/conversations")
async def create_conversation(request: Request):
    """Create or update a conversation."""
    data = await request.json()
    session_id = data.get("session_id", f"session_{int(time.time() * 1000)}")

    conv_dir = Path(__file__).parent.parent / "conversations"
    conv_dir.mkdir(exist_ok=True)
    conv_file = conv_dir / f"{session_id}.json"

    conv_data = {
        "session_id": session_id,
        "created_at": data.get("created_at", ""),
        "updated_at": data.get("updated_at", ""),
        "title": data.get("title", "新会话"),
        "messages": data.get("messages", []),
        "meta": data.get("meta", {}),
    }

    conv_file.write_text(json.dumps(conv_data, ensure_ascii=False), encoding="utf-8")
    return {"status": "saved", "session_id": session_id}


@app.delete("/api/conversations/{session_id}")
async def delete_conversation(session_id: str):
    """Delete a conversation."""
    conv_dir = Path(__file__).parent.parent / "conversations"
    conv_file = conv_dir / f"{session_id}.json"

    if conv_file.exists():
        conv_file.unlink()
        return {"status": "deleted"}
    return {"status": "not_found"}


# ──────────────────────────────────────────────────
# Session Management API
# ──────────────────────────────────────────────────

@app.get("/api/sessions")
async def list_sessions():
    """List all sessions."""
    sm = get_session_manager()
    sessions = sm.list_sessions()
    return {
        "sessions": [
            {
                "session_id": s["session_id"],
                "task": s.get("task", "")[:80],
                "status": s.get("status", "active"),
                "created_at": s.get("created_at", ""),
                "updated_at": s.get("updated_at", ""),
                "step_count": s.get("harness_state", {}).get("step_count", 0),
                "interrupted": s.get("interrupted", False),
            }
            for s in sessions
        ]
    }


@app.post("/api/sessions")
async def create_session(request: Request):
    """Create a new session."""
    data = await request.json()
    task = data.get("task", "New session")

    sm = get_session_manager()
    session = sm.create_session(task)

    return {
        "session_id": session["session_id"],
        "status": session["status"],
        "created_at": session["created_at"],
    }


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    """Get a specific session's full state."""
    sm = get_session_manager()
    session = sm.get_session(session_id)
    if not session:
        return {"error": "Session not found"}, 404
    return {"session": session}


@app.post("/api/sessions/{session_id}/run")
async def run_session(session_id: str, request: Request):
    """
    Start or resume a session. Returns SSE stream.
    If session exists with status=interrupted, resumes from saved state.
    """
    data = await request.json()
    task = data.get("task", "")

    sm = get_session_manager()
    session = sm.get_session(session_id)

    if not session:
        # Create new session if doesn't exist
        if not task:
            return {"error": "task required for new session"}
        session = sm.create_session(task)

    # Use task from request if provided, otherwise use stored task
    run_task = task or session.get("task", "")

    return EventSourceResponse(event_stream(session_id, run_task))


@app.post("/api/sessions/{session_id}/interrupt")
async def interrupt_session(session_id: str):
    """Signal interrupt for a running session."""
    sm = get_session_manager()
    session = sm.get_session(session_id)
    if not session:
        return {"error": "Session not found"}, 404

    sm.set_interrupted(session_id)
    return {"status": "interrupted", "session_id": session_id}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session."""
    sm = get_session_manager()
    if sm.delete_session(session_id):
        return {"status": "deleted", "session_id": session_id}
    return {"error": "Session not found"}, 404


# ──────────────────────────────────────────────────
# Replay API
# ──────────────────────────────────────────────────

@app.get("/api/replay/{session_id}")
async def get_replay(session_id: str):
    """Get full replay trace for a session."""
    logger = get_replay_logger()
    records = logger.get_replay(session_id)
    if records is None:
        records = load_replay_from_disk(session_id)
    if records is None:
        return {"error": "Replay not found"}, 404
    return {"session_id": session_id, "records": records}


@app.get("/api/replay/{session_id}/step/{step}")
async def get_replay_step(session_id: str, step: int):
    """Get a specific step from replay."""
    logger = get_replay_logger()
    record = logger.get_replay_step(session_id, step)
    if record is None:
        records = load_replay_from_disk(session_id)
        if records:
            for r in records:
                if r.get("step") == step:
                    record = r
                    break
    if record is None:
        return {"error": "Step not found"}, 404
    return {"session_id": session_id, "step": step, "record": record}


# ──────────────────────────────────────────────────
# Tool Registry API
# ──────────────────────────────────────────────────

@app.get("/api/tools")
async def list_tools():
    """List all registered tools."""
    from microharness.agent.tool_registry import get_registry
    registry = get_registry()
    tools = []
    for t in registry.list(include_disabled=True):
        tools.append({
            "name": t.name,
            "description": (t.description or "")[:100],
            "enabled": registry.is_enabled(t.name),
            "safety": registry.get_safety(t.name) or "KEYWORD_CHECK",
        })
    return {"tools": tools}


@app.get("/api/tools/{name}")
async def get_tool(name: str):
    """Get tool details."""
    from microharness.agent.tool_registry import get_registry
    registry = get_registry()
    tool = registry.get(name)
    if not tool:
        return {"error": f"Tool '{name}' not found"}, 404

    return {
        "name": tool.name,
        "description": tool.description or "",
        "enabled": registry.is_enabled(name),
        "safety": registry.get_safety(name) or "KEYWORD_CHECK",
        "schema": registry.get_schema(name),
    }


@app.get("/api/tools/{name}/schema")
async def get_tool_schema(name: str):
    """Get tool JSON schema."""
    from microharness.agent.tool_registry import get_registry
    registry = get_registry()
    schema = registry.get_schema(name)
    if not schema:
        return {"error": f"Tool '{name}' not found or has no schema"}, 404
    return {"schema": schema}


@app.get("/api/benchmark")
def run_benchmark(
    category: str = None,
    tasks: str = None,
    provider: str = None,
    model: str = None,
):
    """Run benchmark tasks via web API."""
    from microharness.observability.evaluation import BenchmarkRunner, print_benchmark_result

    task_ids = tasks.split(",") if tasks else None

    runner = BenchmarkRunner()
    result = runner.run_benchmark(
        category=category,
        provider=provider or PROVIDER,
        model=model or MAIN_MODEL,
        benchmark_ids=task_ids,
    )

    return asdict(result)


# ──────────────────────────────────────────────────
# Prompt Config API
# ──────────────────────────────────────────────────

@app.get("/api/prompt-config")
async def get_prompt_config():
    """Get prompt configuration."""
    config = load_prompt_config()
    intents = get_intent_templates(config)
    return {
        "config": config,
        "intents": intents,
    }


@app.post("/api/prompt-config")
async def update_prompt_config(request: Request):
    """Update prompt configuration (full replace)."""
    data = await request.json()
    is_valid, error = validate_prompt_config(data)
    if not is_valid:
        return {"error": error}, 400
    save_prompt_config(data)
    return {"status": "saved", "config": load_prompt_config()}


@app.post("/api/prompt-config/intents")
async def create_or_update_intent(request: Request):
    """Create or update an intent template."""
    data = await request.json()
    intent_name = data.get("intent_name")
    intent_config = data.get("intent_config")
    new_name = data.get("new_name")  # for rename support

    if not intent_name or not intent_config:
        return {"error": "intent_name and intent_config are required"}, 400

    if "template" not in intent_config:
        return {"error": "intent_config must contain 'template'"}, 400

    config = load_prompt_config()

    # Handle rename: delete old key if new_name differs
    if new_name and new_name != intent_name:
        if intent_name in config["intents"]:
            del config["intents"][intent_name]
        intent_name = new_name

    config["intents"][intent_name] = intent_config
    save_prompt_config(config)
    return {"status": "saved", "intent_name": intent_name}


@app.delete("/api/prompt-config/intents/{intent_name}")
async def remove_intent(intent_name: str):
    """Delete an intent template."""
    config = delete_intent(intent_name)
    return {"status": "deleted", "intent_name": intent_name}


# ──────────────────────────────────────────────────
# ──────────────────────────────────────────────────
# Ollama API
# ──────────────────────────────────────────────────

@app.get("/api/ollama/models")
async def get_ollama_models():
    """Get available Ollama models."""
    from microharness.ollama import get_client
    client = get_client()
    try:
        models = client.list_models()
        return {"models": models}
    except Exception as e:
        return {"models": [], "error": str(e)}


# ──────────────────────────────────────────────────
# RAG API
# ──────────────────────────────────────────────────
# ──────────────────────────────────────────────────

@app.post("/api/rag/upload")
async def upload_document(file: UploadFile, visit_id: str = Form(...), description: str = "", model: str = Form(""), mode: str = Form("")):
    """Upload a document to the knowledge base with optional LLM processing.

    Args:
        mode: Chunking mode - "llm" for heading-based, "field_llm" for field extraction.
              If empty, uses system's default chunking configuration.
    """
    from microharness.rag.rag import rag

    # Read raw file content
    raw_content = await file.read()

    # Try different encodings
    content = None
    for enc in ['utf-8', 'gbk', 'latin-1']:
        try:
            content = raw_content.decode(enc)
            break
        except Exception:
            continue
    if content is None:
        content = raw_content.decode('utf-8', errors='replace')

    # Check if content looks garbled
    if content.count('�') > len(content) * 0.05:
        return {"error": f"文件编码无法正确解码，请确保文件是UTF-8编码。当前内容包含 {content.count('ufffd')} 个乱码字符。"}, 400

    metadata = {"description": description, "original_filename": file.filename}
    metadata['visit_id'] = visit_id

    # Use default behavior with system's chunking configuration
    # Detect HTML and convert to Markdown before chunking
    is_html = bool('<html' in content.lower() or '<body' in content.lower() or '<div' in content.lower()[:500])
    doc_id = rag.add_document(content, file.filename, metadata, is_html=is_html)

    rag.save_index()

    return {"doc_id": doc_id, "filename": file.filename, "visit_id": visit_id, "status": "success", "mode": mode or "default"}




@app.get("/api/rag/documents")
async def list_rag_documents():
    """List all documents in the knowledge base."""
    from microharness.rag.rag import rag
    return {"documents": rag.list_documents()}


@app.delete("/api/rag/documents/{doc_id}")
async def delete_rag_document(doc_id: str):
    """Delete a document from the knowledge base."""
    from microharness.rag.rag import rag
    success = rag.delete_document(doc_id)
    if success:
        rag.save_index()
        return {"status": "success"}
    return {"status": "error", "message": "Document not found"}, 404


@app.get("/api/rag/search")
async def search_rag(q: str, top_k: int = 3, vector_weight: float = None, bm25_weight: float = None):
    """Search the knowledge base."""
    from microharness.rag.rag import rag
    from microharness.rag.rag_config import load_config

    config = load_config()
    # Use query params if provided, otherwise use config values
    vw = vector_weight if vector_weight is not None else (config.vector_weight if config.search_mode == "hybrid" else 1.0)
    bw = bm25_weight if bm25_weight is not None else (config.bm25_weight if config.search_mode == "hybrid" else 0.0)

    results = rag.search(q, top_k, vw, bw)
    return {
        "results": [
            {
                "doc_id": r.document.doc_id,
                "filename": r.document.filename,
                "content": r.document.content,
                "created_at": r.document.created_at,
                "score": r.score,
            }
            for r in results
        ]
    }


@app.get("/api/rag/config")
async def get_rag_config():
    """Get RAG configuration."""
    from microharness.rag.rag_config import load_config
    config = load_config()
    return config.to_dict()


@app.post("/api/rag/config")
async def update_rag_config(request: Request):
    """Update RAG configuration."""
    from microharness.rag.rag_config import load_config, save_config, RAGConfig

    data = await request.json()
    config = load_config()

    # Update fields
    for key in ["chunk_mode", "chunk_size", "chunk_overlap", "search_mode", "vector_weight", "bm25_weight", "enhance_query_mode"]:
        if key in data and hasattr(config, key):
            setattr(config, key, data[key])

    save_config(config)
    return {"status": "success", "config": config.to_dict()}


@app.post("/api/rag/filter")
async def filter_records(request: Request):
    """Filter medical records using natural language conditions."""
    from microharness.rag.record_filter import RecordFilter

    data = await request.json()
    condition = data.get("condition", "")
    visit_id = data.get("visit_id")
    model = data.get("model", "qwen2.5:7b")
    top_k = data.get("top_k", 20)
    only_matched = data.get("only_matched", False)
    enhance_mode = data.get("enhance_mode", "simple")
    enhance_model = data.get("enhance_model")

    if not condition:
        return {"error": "condition is required"}, 400

    try:
        record_filter = RecordFilter(
            model=model,
            retrieval_top_k=top_k,
            enhance_query_mode=enhance_mode,
            enhance_model=enhance_model
        )
        filter_result = record_filter.filter(condition, visit_id=visit_id, only_matched=only_matched)

        results = filter_result["results"]
        enhanced_query = filter_result.get("enhanced_query", "")
        enhance_query_mode = filter_result.get("enhance_query_mode", "simple")

        return {
            "condition": condition,
            "visit_id": visit_id,
            "model": model,
            "total_candidates": top_k,
            "matched_count": len(results),
            "enhanced_query": enhanced_query,
            "enhance_query_mode": enhance_query_mode,
            "step_timings": filter_result.get("step_timings", {}),
            "results": [
                {
                    "doc_id": r.doc_id,
                    "filename": r.filename,
                    "score": min(1.0, max(0.0, r.score)),  # 向量相似度 0-1
                    "matched": r.matched,
                    "reason": r.reason,  # LLM判断理由
                    "matched_keywords": r.matched_keywords or [],  # 匹配上的关键词
                    "retrieved_chunk": r.retrieved_chunk or "",  # RAG检索到的chunk内容
                    "content_preview": (r.retrieved_chunk or r.content)[:500] + "..." if len(r.retrieved_chunk or r.content) > 500 else (r.retrieved_chunk or r.content),  # 检索内容预览
                }
                for r in results
            ]
        }
    except Exception as e:
        rag_logger.error(f"Filter error: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}, 500


@app.post("/api/rag/filter_batch")
async def filter_records_batch(request: Request):
    """Batch filter: retrieve chunks then judge all together.

    combined mode: multi-route search per sub-condition → round-robin merge → LLM judge.
    per_doc mode: single-query search → judge each doc independently.
    """
    from microharness.rag.record_filter import RecordFilter

    data = await request.json()
    condition = data.get("condition", "")
    visit_id = data.get("visit_id")
    model = data.get("model", "qwen2.5:7b")
    top_k = data.get("top_k", 20)
    sub_top_k = data.get("sub_top_k", 3)  # combined: chunks per sub-condition
    score_threshold = float(data.get("score_threshold", 0.0))
    enhance_mode = data.get("enhance_mode", "simple")
    enhance_model = data.get("enhance_model")
    merge_mode = data.get("merge_mode", "combined")

    if not condition:
        return {"error": "condition is required"}, 400

    try:
        record_filter = RecordFilter(
            model=model,
            enhance_query_mode=enhance_mode,
            enhance_model=enhance_model
        )
        result = record_filter.filter_batch(
            condition, visit_id=visit_id, top_k=top_k,
            score_threshold=score_threshold, merge_mode=merge_mode,
            sub_top_k=sub_top_k,
        )

        return {
            "condition": condition,
            "visit_id": visit_id,
            "model": model,
            "top_k": top_k,
            "sub_top_k": sub_top_k,
            "score_threshold": score_threshold,
            "enhanced_query": result.get("enhanced_query", ""),
            "enhance_query_mode": result.get("enhance_query_mode", "simple"),
            "step_timings": result.get("step_timings", {}),
            "matched": result.get("matched", False),
            "matched_docs": result.get("matched_docs", []),
            "all_chunks": result.get("all_chunks", []),
            "summary": result.get("summary", ""),
            "merge_mode": merge_mode
        }
    except Exception as e:
        rag_logger.error(f"Filter batch error: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}, 500


@app.get("/api/rag/documents/{doc_id}/chunks")
async def get_document_chunks(doc_id: str):
    """List all chunks for a specific document."""
    from microharness.rag.rag import rag

    doc = rag.get_document(doc_id)
    if not doc:
        return {"error": "Document not found"}, 404

    # Get all chunk IDs for this document
    chunk_ids = rag._get_document_chunks(doc_id)

    # Fetch chunk contents from ChromaDB
    collection = rag._get_chroma_collection()
    chunks = []

    if chunk_ids and chunk_ids[0] == doc_id:
        # Single chunk (doc_id stored as chunk_id)
        try:
            chunk_data = collection.get(ids=[doc_id], include=["documents", "metadatas"])
            if chunk_data["documents"]:
                chunks.append({
                    "chunk_id": doc_id,
                    "chunk_index": 0,
                    "total_chunks": 1,
                    "content": chunk_data["documents"][0],
                    "metadata": chunk_data["metadatas"][0] if chunk_data["metadatas"] else {}
                })
        except Exception:
            pass
    else:
        # Multiple chunks (doc_id_chunk_N format)
        try:
            chunk_data = collection.get(ids=chunk_ids, include=["documents", "metadatas"])
            for i, (cid, content, meta) in enumerate(zip(
                chunk_data["ids"],
                chunk_data["documents"],
                chunk_data["metadatas"]
            )):
                chunks.append({
                    "chunk_id": cid,
                    "chunk_index": meta.get("chunk_index", i),
                    "total_chunks": meta.get("total_chunks", len(chunk_ids)),
                    "content": content,
                    "metadata": meta
                })
        except Exception:
            pass

    return {
        "doc_id": doc_id,
        "filename": doc.filename,
        "created_at": doc.created_at,
        "chunk_count": len(chunks),
        "chunks": chunks
    }


@app.get("/api/rag/chunks/{chunk_id}")
async def get_chunk(chunk_id: str):
    """Get specific chunk content by ID."""
    from microharness.rag.rag import rag

    collection = rag._get_chroma_collection()
    try:
        chunk_data = collection.get(ids=[chunk_id], include=["documents", "metadatas"])
        if not chunk_data["documents"]:
            return {"error": "Chunk not found"}, 404

        return {
            "chunk_id": chunk_id,
            "content": chunk_data["documents"][0],
            "metadata": chunk_data["metadatas"][0] if chunk_data["metadatas"] else {}
        }
    except Exception:
        return {"error": "Chunk not found"}, 404


@app.post("/api/rag/clear")
async def clear_index():
    """
    Clear all RAG index data (ChromaDB + in-memory documents).
    Use this when documents are corrupted and need to be re-imported.
    """
    from microharness.rag.rag import rag as rag_instance

    doc_count = len(rag_instance._documents)
    doc_ids = list(rag_instance._documents.keys())

    # Clear ChromaDB
    try:
        collection = rag_instance._get_chroma_collection()
        all_chunks = collection.get(include=["ids"])
        if all_chunks["ids"]:
            collection.delete(ids=all_chunks["ids"])
    except Exception:
        pass

    # Clear in-memory documents
    rag_instance._documents.clear()
    rag_instance._chunk_to_parent.clear()
    rag_instance._bm25 = None

    # Delete index.json
    index_file = rag_instance.index_dir / "index.json"
    if index_file.exists():
        index_file.unlink()

    return {
        "status": "cleared",
        "documents_removed": doc_count,
        "doc_ids": doc_ids
    }


@app.post("/api/rag/rebuild")
async def rebuild_index():
    """
    Rebuild the entire RAG index with current chunking configuration.
    Re-adds all documents from memory using the latest chunk_size/overlap settings.
    Returns progress information.
    """
    from microharness.rag.rag import rag as rag_instance

    if not rag_instance._documents:
        return {"status": "no_documents", "message": "No documents to rebuild"}

    total = len(rag_instance._documents)
    results = []

    for doc_id, doc in list(rag_instance._documents.items()):
        try:
            # Delete existing chunks from ChromaDB
            chunk_ids = rag_instance._get_document_chunks(doc_id)
            if chunk_ids:
                rag_instance._get_chroma_collection().delete(ids=chunk_ids)

            # Re-chunk and re-index
            chunks = rag_instance._chunk_content(doc.content)
            if len(chunks) == 1:
                rag_instance._add_single_chunk(
                    rag_instance._get_chroma_collection(),
                    doc_id, chunks[0], doc.filename
                )
            else:
                rag_instance._add_multiple_chunks(
                    rag_instance._get_chroma_collection(),
                    doc_id, doc.filename, chunks
                )

            results.append({
                "doc_id": doc_id,
                "filename": doc.filename,
                "status": "success",
                "chunk_count": len(chunks)
            })
        except Exception as e:
            results.append({
                "doc_id": doc_id,
                "filename": doc.filename,
                "status": "error",
                "error": str(e)
            })

    # Rebuild BM25
    rag_instance._rebuild_bm25_index()

    # Save updated index
    rag_instance.save_index()

    success_count = sum(1 for r in results if r["status"] == "success")
    return {
        "status": "done",
        "total": total,
        "success_count": success_count,
        "error_count": total - success_count,
        "results": results
    }


@app.api_route("/api/rag/preview_chunk", methods=["GET", "POST"])
async def preview_chunk(request: Request):
    """Preview how text would be chunked with current config. Supports GET (URL params) and POST (JSON body)."""
    from microharness.rag.rag import rag
    from microharness.rag.chunker import html_to_markdown

    if request.method == "GET":
        text = request.query_params.get("text", "")
    else:
        body = await request.json()
        text = body.get("text", "")

    if not text:
        return {"chunks": [], "is_html": False, "markdown": ""}

    # For large HTML, truncate before conversion to avoid timeout
    # We only need a representative sample for preview, not the full document
    MAX_MARKDOWNIFY_SIZE = 50 * 1024  # 50KB
    if len(text) > MAX_MARKDOWNIFY_SIZE:
        text = text[:MAX_MARKDOWNIFY_SIZE]

    # Detect HTML and convert to Markdown before preview
    is_html = bool('<html' in text.lower() or '<body' in text.lower() or '<div' in text.lower()[:500])
    markdown_content = html_to_markdown(text) if is_html else text
    chunks = rag.preview_chunking(text, is_html=is_html)
    return {"chunks": chunks, "is_html": is_html, "markdown": markdown_content}


# ──────────────────────────────────────────────────
# Binding API (Two-Stage HTML-XML Binder)
# ──────────────────────────────────────────────────

import tempfile
import shutil
from pathlib import Path

# In-memory binding results storage
_binding_results = {}

@app.post("/api/binding/single")
async def binding_single(
    xml_dir: str = "data/临床文档模板",
    stage1_model: str = "qwen2.5:7b",
    stage2_model: str = "qwen2.5:7b",
    stage3_model: str = "qwen2.5:7b",
    stage1_timeout: int = 120,
    stage2_timeout: int = 120,
    stage3_timeout: int = 300,
    file: UploadFile = None
):
    """Single HTML file binding - three stages."""
    if not file:
        return {"error": "No file uploaded"}, 400

    # Resolve xml_dir relative to project root
    xml_dir = str(PROJECT_ROOT / xml_dir)

    # Save uploaded file to temp
    with tempfile.NamedTemporaryFile(delete=False, suffix=".html", mode='wb') as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        binder = TwoStageBinder(
            stage1_model=stage1_model,
            stage2_model=stage2_model,
            stage3_model=stage3_model,
            xml_dir=xml_dir,
            stage1_timeout=stage1_timeout,
            stage2_timeout=stage2_timeout,
            stage3_timeout=stage3_timeout
        )
        result = binder.bind_file(tmp_path)
        if result:
            return {
                "html_file": result.html_file,
                "xml_template": result.xml_template,
                "match_confidence": result.match_confidence,
                "field_count": len(result.field_bindings),
                "stage1_output": result.stage1_output if result.stage1_output else "",
                "bindings": [asdict(b) for b in result.field_bindings]
            }
        return {"error": "Binding failed"}, 500
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@app.post("/api/binding/new-flow")
async def binding_new_flow(
    xml_dir: str = "data/临床文档模板",
    stage1_model: str = "qwen2.5:7b",
    stage2_model: str = "qwen2.5:7b",
    stage3_model: str = "qwen2.5:7b",
    stage4_model: str = "qwen2.5:7b",
    file: UploadFile = None
):
    """New 4-stage binding flow:
    1. LLM matches HTML to XML template
    2. Parse XML template to get required fields
    3. Convert HTML to structured text matching XML fields
    4. Bind structured text to XML nodes
    """
    if not file:
        return {"error": "No file uploaded"}, 400

    xml_dir = str(PROJECT_ROOT / xml_dir)

    # Read HTML content
    html_content = await file.read()
    try:
        html_text = html_content.decode('utf-8', errors='replace')
    except:
        html_text = html_content.decode('gbk', errors='replace')

    # Extract doc type hint from filename
    import re
    doc_type_hint = file.filename or ""

    # Stage 1: Template matching (LLM)
    stage1_client = OllamaClient(model=stage1_model, timeout=120)
    stage2_client = OllamaClient(model=stage2_model, timeout=120)

    templates = load_xml_templates(xml_dir)

    # Stage1: Match template
    from microharness.rag.template_binding import stage1_extract, parse_stage1_output, clean_html
    fields = parse_stage1_output(stage1_extract(html_text, stage1_model, stage1_client))

    templates_info = "\n".join([f"- {t['filename']}" for t in templates])
    user_prompt = f"""【可用模板列表】
{templates_info}

【文档类型提示】
{doc_type_hint}

【HTML内容摘要】
{clean_html(html_text)[:500]}

请选择最匹配的模板文件名，只输出文件名：
"""

    try:
        response = stage1_client.chat([
            {"role": "user", "content": user_prompt}
        ], temperature=0.0)
        matched_name = response.strip()
    except Exception as e:
        rag_logger.error(f"[NewFlow] Stage1 failed: {e}")
        matched_name = ""

    # Find matched template
    matched_template = None
    for t in templates:
        if matched_name in t["filename"] or t["filename"] in matched_name:
            matched_template = t
            break
    if not matched_template:
        matched_template = templates[0]

    # Stage2: Get XML fields (already parsed in template)
    xml_fields = list(matched_template["nodes"].keys())

    # Stage3: Convert HTML to structured text matching XML fields (LLM)
    stage3_client = OllamaClient(model=stage3_model, timeout=120)
    xml_fields_text = "\n".join([f"- {p}: {v.get('sample', '')}" for p, v in matched_template["nodes"].items()])

    user_prompt3 = f"""【XML模板需要的字段】
{xml_fields_text}

【HTML病历内容】
{clean_html(html_text)[:6000]}

请根据XML模板需要的字段，从HTML中提取对应的值，输出JSON数组：
[{{"field":"字段路径","value":"字段值"}},...]

只输出JSON，不要其他内容：
"""

    try:
        response3 = stage3_client.chat([
            {"role": "user", "content": user_prompt3}
        ], temperature=0.0)
        html_fields = json.loads(response3.strip())
    except Exception as e:
        rag_logger.error(f"[NewFlow] Stage3 failed: {e}")
        html_fields = []

    # Stage4: Bind HTML fields to XML nodes (LLM)
    stage4_client = OllamaClient(model=stage4_model, timeout=300)

    user_prompt4 = f"""【XML模板节点】
{xml_fields_text}

【从HTML提取的字段及其值】
{json.dumps(html_fields, ensure_ascii=False)}

任务：将每个字段的field名保持不变，将其value绑定到对应的XML节点路径。

输出格式（每个元素包含html_field和xml_path）：
[{{"html_field":"clinicaldocument/docheader/version","value":"V1.0","xml_path":"clinicaldocument/docheader/version"}},...]

只输出JSON数组，不要其他内容：
"""

    bindings = []
    try:
        response4 = stage4_client.chat([
            {"role": "user", "content": user_prompt4}
        ], temperature=0.0)
        bindings_data = json.loads(response4.strip())
        bindings = bindings_data
    except Exception as e:
        rag_logger.error(f"[NewFlow] Stage4 failed: {e}")

    return {
        "template": matched_template["filename"],
        "confidence": 0.9,
        "xml_fields": xml_fields,
        "html_fields": html_fields,
        "bindings": bindings
    }


@app.post("/api/binding/v2")
async def binding_v2(
    xml_dir: str = "data/临床文档模板",
    stage1_model: str = "qwen2.5:7b",
    stage3_model: str = "qwen2.5:7b",
    stage4_model: str = "qwen2.5:7b",
    file: UploadFile = None
):
    """New 4-stage binding: 1) LLM匹配模板 2) 解析XML字段 3) LLM提取字段 4) LLM绑定字段"""
    if not file:
        return {"error": "No file uploaded"}, 400

    xml_dir = str(PROJECT_ROOT / xml_dir)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".html", mode='wb') as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        binder = ThreeStageBinder(
            stage1_model=stage1_model,
            stage3_model=stage3_model,
            xml_dir=xml_dir
        )
        result = binder.bind_file(tmp_path)
        if result:
            return result
        return {"error": "Binding failed"}, 500
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@app.post("/api/binding/directory")
async def binding_directory(
    html_dir: str,
    xml_dir: str = "data/临床文档模板",
    stage1_model: str = "qwen2.5:7b",
    stage2_model: str = "qwen2.5:7b",
    stage1_timeout: int = 120,
    stage2_timeout: int = 300
):
    """Batch directory binding."""
    # Resolve paths relative to project root
    xml_dir = str(PROJECT_ROOT / xml_dir)
    html_dir = str(PROJECT_ROOT / html_dir)

    binder = TwoStageBinder(
        stage1_model=stage1_model,
        stage2_model=stage2_model,
        xml_dir=xml_dir,
        stage1_timeout=stage1_timeout,
        stage2_timeout=stage2_timeout
    )
    result = binder.bind_directory(html_dir)
    if result:
        # Store in memory
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        key = f"batch_{ts}"
        _binding_results[key] = result
        return {
            "total": result.statistics["total"],
            "matched": result.statistics["matched"],
            "unmatched": result.statistics["unmatched"],
            "match_rate": result.statistics["match_rate"],
            "result_key": key
        }
    return {"error": "Binding failed"}, 500


@app.post("/api/binding/compare-models")
async def binding_compare_models(
    html_dir: str,
    xml_dir: str = "data/临床文档模板",
    stage1_models: str = "",
    stage2_models: str = "",
    stage1_timeout: int = 120,
    stage2_timeout: int = 300
):
    """Compare binding results across different model configurations."""
    # Resolve paths relative to project root
    xml_dir = str(PROJECT_ROOT / xml_dir)
    html_dir = str(PROJECT_ROOT / html_dir)

    s1_list = [m.strip() for m in stage1_models.split(",") if m.strip()]
    s2_list = [m.strip() for m in stage2_models.split(",") if m.strip()]

    results = []
    for s1 in s1_list:
        for s2 in s2_list:
            binder = TwoStageBinder(stage1_model=s1, stage2_model=s2, xml_dir=xml_dir, stage1_timeout=stage1_timeout, stage2_timeout=stage2_timeout)
            result = binder.bind_directory(html_dir)
            if result:
                results.append({
                    "stage1_model": s1,
                    "stage2_model": s2,
                    "statistics": result.statistics
                })

    return {"comparisons": results}


@app.get("/api/binding/results")
async def get_binding_results():
    """Get all stored binding results."""
    return {"results": list(_binding_results.keys())}


@app.get("/api/binding/results/{result_key}")
async def get_binding_result(result_key: str):
    """Get a specific binding result."""
    if result_key not in _binding_results:
        return {"error": "Result not found"}, 404
    result = _binding_results[result_key]
    return {
        "stage1_model": result.stage1_model,
        "stage2_model": result.stage2_model,
        "statistics": result.statistics,
        "bindings": [{
            "html_file": b.html_file,
            "xml_template": b.xml_template,
            "match_confidence": b.match_confidence,
            "field_count": len(b.field_bindings)
        } for b in result.bindings]
    }


@app.get("/api/benchmark/history")
async def get_benchmark_history():
    """Get list of past benchmark results."""
    results_dir = Path("benchmark_results")
    if not results_dir.exists():
        return {"results": []}

    results = []
    for f in results_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            results.append({
                "run_id": data.get("run_id"),
                "timestamp": data.get("timestamp"),
                "benchmark_id": data.get("benchmark_id"),
                "provider": data.get("provider"),
                "model": data.get("model"),
                "tasks_total": data.get("tasks_total"),
                "tasks_passed": data.get("tasks_passed"),
                "tasks_failed": data.get("tasks_failed"),
                "pass_rate": data.get("pass_rate"),
                "avg_score": data.get("avg_score"),
                "total_cost_usd": data.get("total_cost_usd"),
                "total_duration_ms": data.get("total_duration_ms"),
                "task_results": data.get("task_results", []),
            })
        except Exception:
            pass

    results.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return {"results": results}


@app.delete("/api/benchmark/history")
async def clear_benchmark_history():
    """Delete all benchmark history results."""
    import shutil
    results_dir = Path("benchmark_results")
    if results_dir.exists():
        for f in results_dir.glob("*.json"):
            f.unlink()
    return {"status": "deleted"}


@app.delete("/api/benchmark/history/{run_id}")
async def delete_benchmark_result(run_id: str):
    """Delete a specific benchmark result."""
    results_dir = Path("benchmark_results")
    # Find file matching run_id prefix
    for f in results_dir.glob(f"{run_id}*.json"):
        f.unlink()
        return {"status": "deleted", "run_id": run_id}
    return {"error": "Not found"}, 404


@app.delete("/api/tools/{name}")
async def unregister_tool(name: str):
    """Unregister a tool (built-in tools cannot be unregistered)."""
    from microharness.agent.tool_registry import get_registry
    from microharness.agent.tools import BUILTIN_SAFETY
    if name in BUILTIN_SAFETY:
        return {"error": "Cannot unregister built-in tool"}, 400

    registry = get_registry()
    if registry.unregister(name):
        # Also remove from disabled_skills
        disabled_skills.discard(name)
        return {"status": "unregistered", "name": name}
    return {"error": f"Tool '{name}' not found"}, 404


@app.post("/api/tools/{name}/enable")
async def enable_tool(name: str):
    """Enable a tool."""
    from microharness.agent.tool_registry import get_registry
    registry = get_registry()
    if registry.enable(name):
        disabled_skills.discard(name)
        return {"status": "enabled", "name": name}
    return {"error": f"Tool '{name}' not found"}, 404


@app.post("/api/tools/{name}/disable")
async def disable_tool(name: str):
    """Disable a tool."""
    from microharness.agent.tool_registry import get_registry
    registry = get_registry()
    if registry.disable(name):
        disabled_skills.add(name)
        return {"status": "disabled", "name": name}
    return {"error": f"Tool '{name}' not found"}, 404


# ═══════════════════════════════════════════════════════════
# Medical Record Filter API
# ═══════════════════════════════════════════════════════════

_PATIENTS_DIR = PROJECT_ROOT / "data" / "patients"
_VALID_HTML_FILES = ["入院记录.html", "出院记录.html", "门急诊病历.html",
                     "首次病程记录.html", "日常病程记录.html", "手术记录.html"]

# Accept any HTML/HTM file for upload (template matching happens during binding)
def _is_html_file(filename: str) -> bool:
    return filename.lower().endswith(('.html', '.htm'))


def _log_error_to_db(register_no: str, visit_no: str, doc_id: str, error_type: str, msg: str):
    """Log upload/bind/insert errors to database."""
    try:
        from microharness.database.db_client import get_db as _gdb
        _db = _gdb()
        if _db.test():
            safe_msg = msg[:200].replace("'", "''")
            _db.client.execute(
                f"INSERT INTO hdc_userv2.emr_error_log (doc_id, register_no, visit_no, error_type, error_msg) "
                f"VALUES ('{doc_id}', '{register_no}', '{visit_no}', '{error_type}', '{safe_msg}')")
    except Exception:
        pass


@app.get("/api/medical/patients")
async def list_patients():
    """List all patients from database records."""
    from microharness.database.field_mapper import TABLE_MAP
    from microharness.database.db_client import get_db as get_database

    patients_map = {}  # register_no → {name, visits: {visit_no → {files}}}

    try:
        db = get_database()
        if db.test():
            for doc_title, info in TABLE_MAP.items():
                table = info["table"]
                try:
                    rows = db.client.execute(
                        f"SELECT DISTINCT registerno, visitnumber, papat_relpatientid, paadm_relvisitnumber, doc_id "
                        f"FROM {table} WHERE registerno IS NOT NULL"
                    )
                    for r in rows:
                        rn = r.get("registerno","").strip()
                        vn = r.get("visitnumber","").strip()
                        if not rn: continue
                        if rn not in patients_map:
                            patients_map[rn] = {"name": rn, "visits": {}}
                        if vn not in patients_map[rn]["visits"]:
                            patients_map[rn]["visits"][vn] = {"files": {}}
                        doc_id = r.get("doc_id","")
                        if doc_id:
                            patients_map[rn]["visits"][vn]["files"][f"{doc_title}({doc_id})"] = {"uploaded": True, "bound": True}
                except Exception:
                    continue
    except Exception:
        pass

    # Fallback to file-based if DB empty
    if not patients_map and _PATIENTS_DIR.exists():
        for reg_dir in sorted(_PATIENTS_DIR.iterdir()):
            if not reg_dir.is_dir(): continue
            rn = reg_dir.name
            if rn not in patients_map: patients_map[rn] = {"name": rn, "visits": {}}
            for visit_dir in sorted(reg_dir.iterdir()):
                if not visit_dir.is_dir() or visit_dir.name.startswith("_"): continue
                vn = visit_dir.name
                if vn not in patients_map[rn]["visits"]: patients_map[rn]["visits"][vn] = {"files": {}}
                for fpath in sorted(list(visit_dir.glob("*.html")) + list(visit_dir.glob("*.htm"))):
                    fn = fpath.name
                    bp = visit_dir / "_bindings" / fn.replace(".html","_binding.json").replace(".htm","_binding.json")
                    patients_map[rn]["visits"][vn]["files"][fn] = {"uploaded": True, "bound": bp.exists()}

    patients = []
    for rn, pdata in sorted(patients_map.items()):
        visits = []
        for vn, vdata in sorted(pdata["visits"].items()):
            files = vdata["files"]
            visits.append({
                "visit_no": vn,
                "files": files,
                "total_files": len(files),
                "uploaded_count": len(files),
                "bound_count": sum(1 for s in files.values() if s["bound"]),
            })
        all_total = sum(v["total_files"] for v in visits)
        all_bound = sum(v["bound_count"] for v in visits)
        patients.append({
            "register_no": rn,
            "name": pdata["name"],
            "visits": visits,
            "visit_count": len(visits),
            "total_files": all_total,
            "bound_count": all_bound,
        })
    import sys as _sys
    _pcount = len(patients)
    _vcount = sum(p["visit_count"] for p in patients)
    _fcount = sum(p["total_files"] for p in patients)
    print(f"[PATIENTS] {_pcount}患者, {_vcount}就诊, {_fcount}文件", flush=True)
    return {"patients": patients}


@app.post("/api/medical/upload")
async def medical_upload(
    register_no: str = Form(...),
    visit_no: str = Form(""),
    global_patient_id: str = Form(""),
    global_visit_id: str = Form(""),
    patient_name: str = Form(""),
    files: list[UploadFile] = None
):
    """
    Upload HTML medical records.
    - register_no: 登记号 (required)
    - global_patient_id: 全局患者ID (optional)
    - visit_no: 就诊号 (optional)
    - global_visit_id: 全局就诊号 (optional)
    - patient_name: optional display name
    """
    if not files:
        return {"error": "No files provided"}, 400
    if not register_no or not register_no.strip():
        return {"error": "登记号(register_no)不能为空"}, 400

    register_no = register_no.strip()
    visit_no = visit_no.strip() or "default"
    gpid = global_patient_id.strip()
    gvid = global_visit_id.strip()
    name = patient_name.strip() or gpid or register_no

    # Path: data/patients/{register_no}/{visit_no}/
    patient_dir = _PATIENTS_DIR / register_no
    visit_dir = patient_dir / visit_no
    visit_dir.mkdir(parents=True, exist_ok=True)
    bindings_dir = visit_dir / "_bindings"
    bindings_dir.mkdir(exist_ok=True)

    saved = []
    for file in files:
        filename = file.filename or ""
        doc_id = filename.split("(")[0] if "(" in filename else filename.rsplit(".",1)[0]
        if not _is_html_file(filename):
            saved.append({"filename": filename, "status": "skipped", "reason": "非HTML文件"})
            continue

        content = await file.read()
        if len(content) > 50 * 1024 * 1024:
            saved.append({"filename": filename, "status": "error", "reason": "文件超过50MB"})
            continue

        text = None
        for enc in ['utf-8', 'gbk', 'gb2312', 'gb18030', 'latin-1']:
            try: text = content.decode(enc); break
            except Exception: continue
        if text is None:
            text = content.decode('utf-8', errors='replace')
            _log_error_to_db(register_no, visit_no, doc_id, "ENCODING", "文件编码异常")

        filepath = visit_dir / filename
        filepath.write_text(text, encoding="utf-8")
        saved.append({"filename": filename, "status": "saved"})

    # Meta at patient level
    meta = {
        "name": name,
        "register_no": register_no,
        "global_patient_id": gpid,
        "created_at": datetime.now().isoformat(),
    }
    (patient_dir / "_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    # Meta at visit level
    visit_meta = {
        "visit_no": visit_no,
        "global_visit_id": gvid,
        "file_count": sum(1 for s in saved if s["status"] == "saved"),
        "created_at": datetime.now().isoformat(),
    }
    (visit_dir / "_visit.json").write_text(json.dumps(visit_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "register_no": register_no,
        "visit_no": visit_no,
        "global_patient_id": gpid,
        "global_visit_id": gvid,
        "patient_name": name,
        "files": saved,
    }


@app.delete("/api/medical/patients/{register_no}")
async def delete_patient(register_no: str, visit_no: str = ""):
    """Delete a patient (or specific visit)."""
    import shutil
    if visit_no:
        visit_dir = _PATIENTS_DIR / register_no / visit_no
        if not visit_dir.exists(): return {"error": "Visit not found"}, 404
        shutil.rmtree(str(visit_dir))
    else:
        patient_dir = _PATIENTS_DIR / register_no
        if not patient_dir.exists(): return {"error": "Patient not found"}, 404
        shutil.rmtree(str(patient_dir))
    return {"status": "deleted"}


@app.post("/api/medical/bind/{register_no}")
async def bind_patient(register_no: str, request: Request = None, visit_no: str = ""):
    """Run 4-stage binding on a patient's HTML files.

    Optional JSON body: {"stage1_model": "qwen2.5:7b", ...}
    Query param: visit_no (optional, if empty binds all visits)
    """
    patient_dir = _PATIENTS_DIR / register_no
    if not patient_dir.exists():
        return {"error": "Patient not found"}, 404

    # Determine which visit directories to bind
    if visit_no:
        visit_dirs = [patient_dir / visit_no]
    else:
        visit_dirs = [d for d in patient_dir.iterdir() if d.is_dir() and not d.name.startswith("_")]

    if not visit_dirs or not any(d.exists() for d in visit_dirs):
        return {"error": "No visits found"}, 404

    # Parse model params from request body
    stage1_model = "qwen2.5:7b"
    stage3_model = "qwen2.5:7b"
    stage4_model = "qwen2.5:7b"
    if request:
        try:
            body = await request.json()
            stage1_model = body.get("stage1_model", stage1_model)
            stage3_model = body.get("stage3_model", stage3_model)
            stage4_model = body.get("stage4_model", stage4_model)
        except Exception:
            pass

    from microharness.medical.field_catalog import get_template_filename
    from microharness.rag.template_binding_v2 import ThreeStageBinder, clean_html

    xml_dir = str(PROJECT_ROOT / "data" / "临床文档模板")
    all_results = []

    for visit_dir in visit_dirs:
        if not visit_dir.exists(): continue
        bindings_dir = visit_dir / "_bindings"
        bindings_dir.mkdir(exist_ok=True)

        html_files = list(visit_dir.glob("*.html")) + list(visit_dir.glob("*.htm"))
        for fpath in html_files:
            fname = fpath.name

            template_filename = get_template_filename(fname)
            # If no exact filename match, Stage1 LLM will handle template matching
            if not template_filename:
                rag_logger.info(f"[Bind] No exact template match for {fname}, will use LLM Stage1 matching")

            binding_file = bindings_dir / fname.replace(".html", "_binding.json")

            try:
                binder = ThreeStageBinder(
                    stage1_model=stage1_model,
                    stage3_model=stage3_model,
                    xml_dir=xml_dir
                )
                result = binder.bind_file(str(fpath))
                if result:
                    binding_data = {
                        "html_file": fname,
                        "template": template_filename,
                        "bound_at": datetime.now().isoformat(),
                        "result": result,
                    }
                    binding_file.write_text(json.dumps(binding_data, ensure_ascii=False, indent=2), encoding="utf-8")
                    all_results.append({"filename": fname, "visit_no": visit_dir.name, "status": "bound"})

                    # ── Auto-insert to database ─────────────────
                    try:
                        from microharness.database.field_mapper import map_bindings_to_row, get_table_for_doc
                        from microharness.database.db_client import get_db

                        # doc_id from filename prefix: "3x3052531x1(外科...).html" → "3x3052531x1"
                        import re as _re
                        doc_id = fname.split("(")[0] if "(" in fname else fname.rsplit(".",1)[0]

                        # Determine document title from template (may be None for unknown filenames)
                        doc_title = ""
                        if template_filename:
                            xml_name = template_filename.replace(".xml","").replace("基本数据集","")
                            doc_title = xml_name.split(".",1)[-1].strip() if "." in xml_name else xml_name
                        else:
                            # Fallback: use the matched template from the binding result
                            matched = result.get("template", "")
                            if matched:
                                xml_name = matched.replace(".xml","").replace("基本数据集","")
                                doc_title = xml_name.split(".",1)[-1].strip() if "." in xml_name else xml_name

                        if not doc_title:
                            rag_logger.warning(f"[DB] 无法确定文档类型: {fname}")
                            # skip DB insert for this file
                        else:
                            # Build meta from visit/patient info
                            patient_meta = {}
                            meta_file = patient_dir / "_meta.json"
                            if meta_file.exists():
                                patient_meta = json.loads(meta_file.read_text(encoding="utf-8"))
                            visit_meta = {}
                            vm_file = visit_dir / "_visit.json"
                            if vm_file.exists():
                                visit_meta = json.loads(vm_file.read_text(encoding="utf-8"))

                            meta = {
                                "register_no": patient_meta.get("register_no", register_no),
                                "visit_no": visit_meta.get("visit_no", visit_dir.name),
                                "global_patient_id": patient_meta.get("global_patient_id", ""),
                                "global_visit_id": visit_meta.get("global_visit_id", ""),
                            }

                            # Map binding fields to DB row
                            bindings = result.get("bindings", result.get("field_bindings", []))
                            # Fallback: use html_fields if bindings is empty
                            if not bindings:
                                hf = result.get("html_fields", [])
                                bindings = [{"html_field": h.get("field",""), "value": h.get("value","")} for h in hf]
                            row = map_bindings_to_row(doc_title, bindings, meta)
                            row["doc_id"] = doc_id
                            table = get_table_for_doc(doc_title)

                            # Insert
                            db = get_db()
                            if db.test():
                                ok, err = db.client.upsert(table, row)
                                if ok:
                                    rag_logger.info(f"[DB] UPSERT {table}: {fname} → {len(row)} columns")
                                    try: fpath.unlink(missing_ok=True); binding_file.unlink(missing_ok=True)
                                    except Exception: pass
                                else:
                                    _log_error_to_db(register_no, visit_dir.name, doc_id, "DB_INSERT", err or "UPSERT failed")
                            else:
                                rag_logger.warning(f"[DB] 数据库不可用，跳过入库")
                    except Exception as e:
                        import traceback
                        rag_logger.warning(f"[DB] 入库失败 {fname}: {e}")
                        rag_logger.warning(f"[DB] Traceback: {traceback.format_exc()[-200:]}")
                else:
                    all_results.append({"filename": fname, "visit_no": visit_dir.name, "status": "failed", "reason": "绑定返回空"})
                    _log_error_to_db(register_no, visit_dir.name, doc_id, "BIND", "绑定返回空")
            except Exception as e:
                all_results.append({"filename": fname, "visit_no": visit_dir.name, "status": "error", "reason": str(e)})
                _log_error(doc_id, "BIND", str(e)[:200])

    return {
        "register_no": register_no,
        "results": all_results,
    }


@app.get("/api/medical/binding-status/{register_no}")
async def binding_status(register_no: str, visit_no: str = ""):
    """Get binding status for a patient's files across visits."""
    patient_dir = _PATIENTS_DIR / register_no
    if not patient_dir.exists():
        return {"error": "Patient not found"}, 404

    # Collect visit dirs
    if visit_no:
        visit_dirs = [patient_dir / visit_no]
    else:
        visit_dirs = [d for d in patient_dir.iterdir() if d.is_dir() and not d.name.startswith("_")]
    # Backward compat: flat old structure
    if not visit_dirs and list(patient_dir.glob("*.html")):
        visit_dirs = [patient_dir]

    files_status = {}
    for vd in visit_dirs:
        if not vd.exists(): continue
        for fpath in sorted(list(vd.glob("*.html")) + list(vd.glob("*.htm"))):
            fname = fpath.name
            bp = vd / "_bindings" / fname.replace(".html","_binding.json").replace(".htm","_binding.json")
            bd = None
            if bp.exists():
                try: bd = json.loads(bp.read_text(encoding="utf-8"))
                except Exception: pass
            files_status[fname] = {
                "uploaded": True, "bound": bp.exists(), "binding": bd,
                "visit_no": vd.name if vd != patient_dir else "default",
            }

    return {"register_no": register_no, "files": files_status}


@app.get("/api/medical/field-catalog")
async def get_field_catalog():
    """Get the field catalog built from XML templates."""
    from microharness.medical.field_catalog import get_catalog, list_document_types
    catalog = get_catalog()
    return {
        "document_types": list_document_types(),
        "derived_fields": catalog.get("derived_fields", {}),
    }


@app.get("/api/medical/catalog-config")
async def get_catalog_config():
    """Get the editable DOCUMENT_CATALOG config."""
    config_path = PROJECT_ROOT / "configs" / "medical_catalog.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Return current in-code catalog
    from microharness.medical.query_router import DOCUMENT_CATALOG
    return DOCUMENT_CATALOG


@app.post("/api/medical/catalog-config")
async def save_catalog_config(request: Request):
    """Save the edited DOCUMENT_CATALOG config."""
    data = await request.json()
    config_path = PROJECT_ROOT / "configs" / "medical_catalog.json"
    config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # Reload into the router module
    import microharness.medical.query_router as qr
    qr.DOCUMENT_CATALOG = data
    return {"status": "saved"}


@app.get("/api/medical/binding-result/{register_no}/{filename}")
async def get_binding_result(register_no: str, filename: str, visit_no: str = ""):
    """Get the binding result for a specific file (searches all visits if needed)."""
    patient_dir = _PATIENTS_DIR / register_no
    if not patient_dir.exists():
        return {"error": "Patient not found"}, 404
    # Search in visit dirs
    if visit_no:
        search_dirs = [patient_dir / visit_no]
    else:
        search_dirs = [d for d in patient_dir.iterdir() if d.is_dir() and not d.name.startswith("_")]
    if not search_dirs:
        search_dirs = [patient_dir]
    for vd in search_dirs:
        bp = vd / "_bindings" / filename.replace(".html","_binding.json").replace(".htm","_binding.json")
        if bp.exists():
            try:
                return json.loads(bp.read_text(encoding="utf-8"))
            except Exception as e:
                return {"error": str(e)}, 500
    return {"error": "Binding not found"}, 404


@app.post("/api/medical/query")
async def medical_query(request: Request):
    """Execute a filter query against patient binding data."""
    # Robust body parsing with encoding fallback
    try:
        data = await request.json()
    except Exception:
        body = await request.body()
        # Try GBK fallback for Chinese characters
        try:
            text = body.decode("gbk")
            data = json.loads(text)
        except Exception:
            text = body.decode("utf-8", errors="replace")
            try:
                data = json.loads(text)
            except Exception:
                return {"error": "无法解析请求体"}
    condition = data.get("condition", "")
    register_no = data.get("register_no", data.get("patient_id", "")).strip()
    visit_no = data.get("visit_no", "").strip()
    judge_model = data.get("judge_model", "qwen2.5:7b")
    router_model = data.get("router_model", "qwen2.5:3b")
    if not condition or not register_no:
        return {"error": "condition and register_no are required"}, 400

    from microharness.medical.query_router import QueryRouter
    from microharness.medical.field_catalog import get_catalog

    catalog = get_catalog()

    # Step 1: Route the query (with configurable model)
    router = QueryRouter(model=router_model)
    route = router.route(condition)
    import sys
    log = lambda msg: (print(msg, flush=True), sys.stdout.flush())
    log(f"\n{'='*60}")
    log(f"[Step1-拆解] 原始问题: {condition}")
    log(f"[Step1-拆解] 拆分方式: {route.get('source','?')} (模型:{router_model})")
    sub_queries = route.get("sub_queries", [condition])
    if not isinstance(sub_queries, list) or len(sub_queries) <= 1: sub_queries = [condition]
    if len(sub_queries) > 1:
        for i, sq in enumerate(sub_queries, 1):
            log(f"[Step1-拆解]   子问题{i}: {sq}")
    log(f"{'='*60}")

    # Step 1.5: LLM query enhancement (medical synonym expansion)
    enhanced_terms = condition
    try:
        enhance_prompt = f"""你是医学同义词扩展器。将查询中的核心术语扩展为同义词列表（用 | 分隔），用于在病历字段中匹配。只输出同义词组，不要其他文字。

查询：背痛 → 背痛|腰背痛|背部疼痛不适|胸背痛|脊柱疼痛
查询：发热 → 发热|发烧|体温升高|高热
查询：住院小于5天 → 住院小于5天|住院天数<5|住院时间短

查询：{condition}
输出："""
        from microharness.ollama import OllamaClient as OC2
        enhancer = OC2(model=router_model, timeout=30)
        enhanced = enhancer.chat([{"role":"user","content":enhance_prompt}], temperature=0.1).strip()
        if enhanced and 2 < len(enhanced) < 200:
            enhanced_terms = enhanced
            log(f"[ENHANCE] 扩展: {enhanced_terms}")
    except Exception as e:
        log(f"[ENHANCE] 跳过: {e}")

    if register_no:
        _query_start = time.time()
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from microharness.database.field_mapper import TABLE_MAP, DOC_FIELDS, COMMON_FIELDS
        from microharness.database.db_client import get_db as get_database

        # Step 2: Route first, then only SELECT needed columns from DB
        sub_queries = route.get("sub_queries", [condition])
        if not isinstance(sub_queries, list) or len(sub_queries) <= 1:
            sub_queries = [condition]

        from microharness.database.field_mapper import TABLE_MAP, DOC_FIELDS, COMMON_FIELDS
        from microharness.database.db_client import get_db as get_database
        from microharness.database.field_mapper import TABLE_MAP as _TM, DOC_FIELDS as _DF, COMMON_FIELDS as _CF, find_db_column
        _TEXT_COLS = {"chief_complaint","present_illness_history","past_medical_history",
            "social_history","maritalandobstetric_history","menstrual_history","family_history",
            "physical_examination","specific_findings","investigations","tcm_four_findings",
            "preliminary_diagnosis","admission_status","admission_diagnosis","discharge_diagnosis",
            "clinical_course","discharge_status","discharge_orders","surgical_procedure",
            "intra_op_events","progress_note","case_characteristics","diagnostic_basis",
            "differential_diagnosis","treatment_plan","diagnosis","allergies","note",
            "pre_op_diagnosis","intra_op_diagnosis"}

        _db_ok = None
        def _db_available():
            nonlocal _db_ok
            if _db_ok is None:
                try:
                    db = get_database()
                    _db_ok = db.test()
                except Exception:
                    _db_ok = False
            return _db_ok

        def query_db_for_route(sq_route):
            """Query DB for tables+columns specified by this route."""
            if not _db_available(): return []
            sq_docs = sq_route.get("target_medical_doc", [])
            sq_sections = sq_route.get("target_sections", [])
            results = []
            try:
                db = get_database()
                for doc_title in sq_docs:
                    info = _TM.get(doc_title, {})
                    table = info.get("table", "")
                    if not table: continue
                    columns = set()
                    for sec in sq_sections:
                        col = find_db_column(doc_title, sec)
                        if col: columns.add(col)
                    for c in ["registerno","visitnumber","doc_id","patient_name","papat_relpatientid","paadm_relvisitnumber"]:
                        columns.add(c)
                    if not columns: continue
                    select_parts = [f"SUBSTRING({c},1,4000) as {c}" if c in _TEXT_COLS else c for c in columns]
                    where = f"registerno = '{register_no}'"
                    if visit_no: where += f" AND visitnumber = '{visit_no}'"
                    sql = f"SELECT {', '.join(select_parts)} FROM {table} WHERE {where}"
                    log(f"  [Step2-DB] {sql[:250]}")
                    try:
                        rows = db.client.execute(sql)
                        field_map = _DF.get(doc_title, {})
                        rev_map = {v: k for k, v in field_map.items()}
                        rev_common = {v: k for k, v in _CF.items()}
                        for row in rows:
                            bindings = []
                            for col, val in row.items():
                                if val and str(val).strip() and col not in ("registerno","visitnumber","doc_id","papat_relpatientid","paadm_relvisitnumber"):
                                    field_name = rev_map.get(col) or rev_common.get(col) or col
                                    bindings.append({"html_field": field_name, "value": str(val), "xml_path": col})
                            if bindings:
                                results.append({"file": f"{doc_title} ({row.get('doc_id','')})",
                                    "template": info.get("doc_type", doc_title),
                                    "bindings": bindings, "visit_no": row.get("visitnumber", "")})
                    except Exception as e:
                        if "not found" in str(e) or "SQLCODE: -30" in str(e):
                            pass  # table doesn't exist, skip silently
                        else:
                            log(f"  [Step2-DB] 失败: {e}")
            except Exception as e:
                log(f"  [Step2-DB] DB不可用: {e}")
            return results

        # DB queries now happen inside check_one_condition → query_db_for_route

        sub_queries = route.get("sub_queries", [condition])
        if not isinstance(sub_queries, list) or len(sub_queries) <= 1:
            sub_queries = [condition]

        # ── For EACH sub-condition, check ALL files in parallel ──
        sub_queries = route.get("sub_queries", [condition])
        if not isinstance(sub_queries, list) or len(sub_queries) <= 1:
            sub_queries = [condition]

        def check_one_condition(sq):
            """Route + check all files for one sub-condition."""
            t0 = time.time()
            sq_route = router.route(sq)
            sq_docs = sq_route.get("target_medical_doc", [])
            sq_sections = sq_route.get("target_sections", [])
            sq_xml = sq_route.get("target_xml_paths", [])
            log(f"  [Step2-路由] 子问题: {sq}")
            log(f"  [Step2-路由]   → 文档: {sq_route.get('target_medical_doc',[])}")
            log(f"  [Step2-路由]   → 章节: {sq_sections[:6]}")
            log(f"  [Step2-路由]   → 来源: {sq_route.get('source','?')} 置信度:{sq_route.get('confidence',0):.0%}")
            # Show keyword match details
            kw = sq_route.get("matched_keywords") or sq_route.get("matched_keyword")
            if kw:
                log(f"  [Step2-路由]   → 命中关键词: {kw}")
            note = sq_route.get("judge_reason", "")
            if note:
                log(f"  [Step2-路由]   → 依据: {note[:80]}")
            # Show per-section metadata reasoning
            match_reason = sq_route.get("match_reason", {})
            if match_reason and isinstance(match_reason, dict):
                for sec, reason in list(match_reason.items())[:5]:
                    log(f"  [Step2-路由]     └ {sec}: {reason[:60]}")
            sq_matched = False
            sq_reason = ""
            sq_files = []

            # Query DB for THIS sub-condition's route (not pre-loaded)
            relevant_files = query_db_for_route(sq_route)
            log(f"  [Step2-路由]   → 匹配文件: {[ab['file'] for ab in relevant_files]}")

            # Check each relevant file in parallel
            def check_one_file(ab):
                sub_fields = []
                for b in ab["bindings"]:
                    path = b.get("xml_path", ""); xml_tag = path.split("/")[-1] if path else ""
                    label = b.get("html_field") or xml_tag; val = b.get("html_value") or b.get("value", "")
                    include = any(ts in label or ts in path or label in ts or xml_tag in ts for ts in sq_sections)
                    if not include:
                        include = any(tp in path or xml_tag in tp or tp in xml_tag for tp in sq_xml)
                    if not sq_sections and not sq_xml: include = True
                    if include: sub_fields.append(f"  {label}: {str(val)[:80]}")  # brief for LLM
                sub_summary = "\n".join(sub_fields[:15]) if sub_fields else "(无匹配字段)"

                if sub_summary == "(无匹配字段)":
                    log(f"    [Step3-取值] {ab['file']}: 无匹配字段 → 跳过")
                    return {"file": ab["file"], "matched": False, "reason": "无相关字段", "fields": ""}

                log(f"    [Step3-取值] {ab['file']}: 命中{len(sub_fields)}个字段")

                try:
                    from microharness.ollama import OllamaClient as JOC2
                    j = JOC2(model=judge_model, timeout=60)
                    resp = j.chat([{"role":"user","content": f"""判断这个条件是否满足。只输出JSON。

条件：{sq}

字段值：
{sub_summary[:2000]}

规则：
- "X年Y月之前/之后" → 提取字段中的日期，与X年Y月比较，之前=早于，之后=晚于
- "小于/短于X天" → 算住院天数=出院-入院，<X → true
- "大于/高于/超过X" → >X → true
- "包含/有XX" → 任一字段含XX → true

输出：{{"matched": true或false, "reason": "理由"}}"""}], temperature=0.1)
                    cleaned = resp.strip()
                    for fence in ("```json","```"):
                        if fence in cleaned:
                            p=cleaned.split(fence)
                            if len(p)>=2: cleaned=p[1].split("```")[0] if "```" in p[1] else p[1]; cleaned=cleaned.strip(); break
                    jd = json.loads(cleaned)
                    result = {"file": ab["file"], "matched": jd.get("matched",False),
                              "reason": jd.get("reason",resp[:80]), "fields": sub_summary[:2000]}
                    log(f"    [Step4-判断] {ab['file']}: {'✓ 符合' if result['matched'] else '✗ 不符合'} — {result['reason'][:60]}")
                    return result
                except Exception as e:
                    return {"file": ab["file"], "matched": False, "reason": f"失败:{str(e)[:60]}", "fields": ""}

            with ThreadPoolExecutor(max_workers=min(3, max(1,len(relevant_files)))) as ex:
                futures = {ex.submit(check_one_file, ab): ab for ab in relevant_files}
                for f in as_completed(futures):
                    r = f.result()
                    sq_files.append(r)
                    if r["matched"]:
                        sq_matched = True
                        sq_reason = (sq_reason + " | " if sq_reason else "") + f"{r['file']}:{r['reason']}"

            elapsed = round((time.time() - t0) * 1000)
            # Collect full evidence from matched files (read complete binding)
            evidence = {}
            for f in sq_files:
                if f.get("matched"):
                    fn = f["file"]
                    ab = next((x for x in relevant_files if x["file"] == fn), None)
                    if ab:
                        full_fields = []
                        for b in ab["bindings"]:
                            path = b.get("xml_path", ""); xml_tag = path.split("/")[-1] if path else ""
                            label = b.get("html_field") or xml_tag; val = b.get("html_value") or b.get("value", "")
                            if any(ts in label or ts in path or label in ts or xml_tag in ts for ts in sq_sections):
                                full_fields.append(f"{label}: {str(val)}")
                        evidence[fn] = "\\n".join(full_fields) if full_fields else "(无匹配字段)"
                    else:
                        evidence[fn] = "(未找到绑定数据)"
            return {"condition": sq, "matched": sq_matched, "reason": sq_reason or "无匹配",
                    "files": sq_files, "docs": sq_docs, "sections": sq_sections,
                    "elapsed_ms": elapsed, "evidence": evidence}

        # Run sub-conditions in PARALLEL
        per_condition_results = {}
        with ThreadPoolExecutor(max_workers=min(3, len(sub_queries))) as cex:
            futures = {cex.submit(check_one_condition, sq): sq for sq in sub_queries}
            for f in as_completed(futures):
                r = f.result()
                sq = futures[f]
                per_condition_results[sq] = r
                log(f"  [Step4-小结] {'✓' if r['matched'] else '✗'} 子条件「{sq[:30]}」 {r['reason'][:100]}")

        # ── Meta-judge: patient-level combination ──
        if len(per_condition_results) > 1:
            cond_summary = "\n".join(
                f"  {info['condition']}: {'✓符合' if info['matched'] else '✗不符合'} — {info['reason'][:100]}"
                for info in per_condition_results.values()
            )
            try:
                meta_prompt = f"""根据各子条件的判断结果，判断原始问题对这位患者是否成立。

原始问题：{condition}

子条件判断（每个条件独立检查了所有病历文件）：
{cond_summary}

规则：
- "并且/且/和" → 全部子条件满足才算符合 (AND)
- "或者/或" → 任一满足就算符合 (OR)
- 条件在不同文件中满足也是可以的（跨文件匹配）

输出JSON：{{"matched": true或false, "reason": "最终判断理由"}}"""
                from microharness.ollama import OllamaClient as MOC
                mj = MOC(model=judge_model, timeout=60)
                mresp = mj.chat([{"role":"user","content":meta_prompt}], temperature=0.1)
                mcleaned = mresp.strip()
                for fence in ("```json","```"):
                    if fence in mcleaned:
                        p=mcleaned.split(fence)
                        if len(p)>=2: mcleaned=p[1].split("```")[0] if "```" in p[1] else p[1]; mcleaned=mcleaned.strip(); break
                md = json.loads(mcleaned)
                matched = md.get("matched", False)
                reason = md.get("reason", mresp[:120])
            except Exception as e:
                matched = all(v["matched"] for v in per_condition_results.values())
                reason = " | ".join(f"{'✓' if v['matched'] else '✗'} {v['condition']}" for v in per_condition_results.values())
        else:
            only = list(per_condition_results.values())[0]
            matched = only["matched"]
            reason = only["reason"]

        total_ms = int((time.time() - _query_start) * 1000)
        log(f"  [Step5-整合] {'✓ 患者符合' if matched else '✗ 患者不符合'} | {reason[:120]}")
        log(f"  [总耗时] {total_ms}ms ({total_ms/1000:.1f}s)")
        log(f"{'='*60}\n")

        results = [{
            "register_no": register_no,
            "matched": matched,
            "reason": reason,
            "per_condition": per_condition_results,
            "all_files": list(set(f for r in per_condition_results.values() for f in [x["file"] for x in r.get("files",[])])),
        }]
        return {
            "condition": condition,
            "register_no": register_no,
            "route": route,
            "results": results,
            "matched_count": 1 if matched else 0,
            "total_ms": int((time.time() - _query_start) * 1000),
        }

    # No patient specified — just return the route
    return {
        "condition": condition,
        "route": route,
    }


@app.get("/templates/database_config.html")
async def serve_db_config_page():
    html_path = templates_dir / "database_config.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return {"error": "database_config.html not found"}


@app.get("/api/database/config")
async def get_db_config():
    from microharness.database.db_client import load_config
    return load_config()


@app.post("/api/database/config")
async def save_db_config(request: Request):
    data = await request.json()
    import microharness.database.db_client as dbc
    dbc._CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # Reset global instance so next get_db() reloads
    dbc._db = None
    return {"status": "saved"}


@app.get("/api/database/test")
async def test_db_config(type: str = "iris"):
    from microharness.database.db_client import IrisClient, MySQLClient, load_config
    cfg = load_config()
    try:
        if type == "iris":
            c = cfg.get("iris", {})
            client = IrisClient(c.get("base_url",""), c.get("namespace","HDCV2DEV"), c.get("username",""), c.get("password",""))
        else:
            c = cfg.get("mysql", {})
            client = MySQLClient(c.get("host","127.0.0.1"), c.get("port",3306), c.get("database",""), c.get("user",""), c.get("password",""))
        ok = client.test()
        return {"ok": ok}
    except Exception as e:
        return {"ok": False, "error": str(e)[:100]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)