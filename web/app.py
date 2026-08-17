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
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, HTTPException, Request, UploadFile, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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
from web.template_binding_routes import router as template_binding_router

# Ensure utf-8 output
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

app = FastAPI(title="NexusHarness", version="0.1.0")
app.include_router(template_binding_router)

# Project root for resolving relative paths
PROJECT_ROOT = Path(__file__).parent.parent

# RAG index loaded lazily on first use — avoids blocking startup
_rag_loaded = False

# ── Startup: warm up Ollama models in background ──────────────────
@app.on_event("startup")
async def _warmup_ollama():
    """Pre-load frequently-used models so first real request isn't slow."""
    import asyncio, time as _time, threading as _thr
    def _warm():
        _time.sleep(3)  # let uvicorn finish binding
        try:
            from microharness.ollama import OllamaClient
            # Only warm the primary model (qwen2.5:3b). Other models load on-demand
            # when configured via router_model / judge_model request params.
            default_model = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
            warm_models = [default_model]
            # Deduplicate
            warm_models = list(dict.fromkeys(warm_models))
            for model in warm_models:
                try:
                    c = OllamaClient(model=model, timeout=120)
                    if c.is_available():
                        t0 = _time.time()
                        c.chat([{"role":"user","content":"OK"}], temperature=0.0)
                        print(f"[Startup] 预热 {model} ({(_time.time()-t0)*1000:.0f}ms)", flush=True)
                except Exception:
                    pass
        except Exception as e:
            print(f"[Startup] Ollama 预热跳过: {e}", flush=True)
    _thr.Thread(target=_warm, daemon=True).start()

def _ensure_rag_loaded():
    global _rag_loaded
    if not _rag_loaded:
        try:
            rag.load_index()
        except Exception as e:
            print(f"[RAG] Index load failed: {e}")
        _rag_loaded = True

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
    """Upload endpoint disabled — medical filter uses DB + external APIs directly."""
    return {"status": "disabled", "message": "RAG upload is disabled. Use /api/medical/upload instead."}




@app.get("/api/rag/documents")
async def list_rag_documents():
    """List all documents in the knowledge base."""
    _ensure_rag_loaded()
    from microharness.rag.rag import rag
    return {"documents": rag.list_documents()}


@app.delete("/api/rag/documents/{doc_id}")
async def delete_rag_document(doc_id: str):
    """Delete a document from the knowledge base."""
    import asyncio
    loop = asyncio.get_running_loop()
    def _do_delete():
        from microharness.rag.rag import rag as _rag
        ok = _rag.delete_document(doc_id)
        if ok:
            _rag.save_index()
        return ok
    success = await loop.run_in_executor(None, _do_delete)
    if success:
        return {"status": "success"}
    return {"status": "error", "message": "Document not found"}, 404


@app.get("/api/rag/search")
async def search_rag(q: str, top_k: int = 3, vector_weight: float = None, bm25_weight: float = None):
    """Search the knowledge base."""
    _ensure_rag_loaded()
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
    _ensure_rag_loaded()
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
    _ensure_rag_loaded()
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
{clean_html(html_text)[:3000]}

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
# Dedicated pool for medical queries — isolates long-running LLM/DB work
# from the asyncio default executor so other endpoints stay responsive.
# Ollama concurrency is governed globally by OllamaClient._OLLAMA_SEMAPHORE (max 2).
_MEDICAL_QUERY_POOL = None
_MEDICAL_QUERY_COORDINATOR = None


def _get_medical_query_coordinator():
    global _MEDICAL_QUERY_COORDINATOR
    if _MEDICAL_QUERY_COORDINATOR is None:
        from microharness.medical.query_concurrency import MedicalQueryCoordinator

        _MEDICAL_QUERY_COORDINATOR = MedicalQueryCoordinator.from_env()
        limits = _MEDICAL_QUERY_COORDINATOR.snapshot()
        print(
            "[medical_query] concurrency initialized | "
            f"max_concurrency={limits['max_concurrency']} | "
            f"max_queue={limits['max_queue']} | "
            f"queue_timeout={limits['queue_timeout_seconds']}s",
            flush=True,
        )
    return _MEDICAL_QUERY_COORDINATOR


def _read_medical_json(path: Path) -> dict:
    try:
        if path.exists() and path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def _resolve_medical_query_visit_context(
    register_no: str,
    visit_no: str,
    global_patient_id: str,
    global_visit_id: str,
) -> tuple[str, str, str]:
    """Fill missing visit context from local patient metadata when unambiguous."""
    reg = str(register_no or "").strip()
    visit = str(visit_no or "").strip()
    gpid = str(global_patient_id or "").strip()
    gvid = str(global_visit_id or "").strip()
    if not reg:
        return visit, gpid, gvid

    patient_dir = _PATIENTS_DIR / reg
    patient_meta = _read_medical_json(patient_dir / "_meta.json")
    if not gpid:
        gpid = str(patient_meta.get("global_patient_id") or "").strip()

    visit_dirs = [
        item for item in sorted(patient_dir.iterdir())
        if item.is_dir() and not item.name.startswith("_")
    ] if patient_dir.exists() else []

    def apply_visit_meta(visit_dir: Path) -> None:
        nonlocal visit, gvid
        meta = _read_medical_json(visit_dir / "_visit.json")
        if not visit:
            visit = str(meta.get("visit_no") or visit_dir.name or "").strip()
        if not gvid:
            gvid = str(meta.get("global_visit_id") or "").strip()

    if visit:
        visit_dir = patient_dir / visit
        if visit_dir.exists():
            apply_visit_meta(visit_dir)
        return visit, gpid, gvid

    if gvid:
        gvid_suffix = gvid.split("_", 1)[1] if "_" in gvid else gvid
        for visit_dir in visit_dirs:
            meta = _read_medical_json(visit_dir / "_visit.json")
            meta_gvid = str(meta.get("global_visit_id") or "").strip()
            meta_visit = str(meta.get("visit_no") or visit_dir.name or "").strip()
            if meta_gvid == gvid or meta_visit == gvid_suffix:
                apply_visit_meta(visit_dir)
                break
        return visit, gpid, gvid

    if len(visit_dirs) == 1:
        apply_visit_meta(visit_dirs[0])

    return visit, gpid, gvid

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
                f"INSERT INTO hdc_userv2.emr_error_log (emr_hosdocid, register_no, visit_no, error_type, error_msg) "
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
                        f"SELECT DISTINCT registerno, visitnumber, papat_relpatientid, paadm_relvisitnumber, emr_hosdocid "
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
                        did = r.get("emr_hosdocid","")
                        patients_map[rn]["visits"][vn]["files"][f"{doc_title}({did})" if did else doc_title] = {"uploaded": True, "bound": True}
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

    # XML templates: prefer Docker-safe path, fallback to data dir
    xml_dir = str(PROJECT_ROOT / "templates_xml")
    if not Path(xml_dir).exists() or not list(Path(xml_dir).glob("*.xml")):
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
                            row["emr_hosdocid"] = doc_id
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
                _log_error_to_db(register_no, visit_dir.name, doc_id, "BIND", str(e)[:200])

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
async def get_catalog_config(effective: bool = False):
    """Get the local editable catalog or the currently effective merged catalog."""
    if effective:
        from microharness.medical.query_router import DOCUMENT_CATALOG
        return DOCUMENT_CATALOG

    config_path = PROJECT_ROOT / "configs" / "medical_catalog.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    from microharness.medical.query_router import _DEFAULT_DOCUMENT_CATALOG
    return _DEFAULT_DOCUMENT_CATALOG


@app.post("/api/medical/catalog-config")
async def save_catalog_config(request: Request):
    """Save the edited DOCUMENT_CATALOG config."""
    data = await request.json()
    config_path = PROJECT_ROOT / "configs" / "medical_catalog.json"
    config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    import microharness.medical.query_router as qr
    source_status = qr.reload_document_catalog()
    return {"status": "saved", "source_status": source_status}


@app.get("/api/medical/catalog-source")
async def get_catalog_source():
    """Get the configured and currently effective medical metadata source."""
    from microharness.medical.catalog_source import load_source_config
    import microharness.medical.query_router as qr

    return {
        **load_source_config(),
        "status": dict(qr.CATALOG_SOURCE_STATUS),
    }


@app.post("/api/medical/catalog-source")
async def set_catalog_source(request: Request):
    """Persist and immediately activate the selected medical metadata source."""
    from microharness.medical.catalog_source import save_source_config
    import microharness.medical.query_router as qr

    data = await request.json()
    try:
        settings = save_source_config(
            source=data.get("source"),
            external_url=data.get("external_url"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    status = qr.reload_document_catalog()
    return {"status": "saved", **settings, "source_status": status}


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


@app.get("/api/medical/query/status")
async def medical_query_status(request_id: str = ""):
    """Return process-local medical-query capacity or one request's state."""
    return _get_medical_query_coordinator().snapshot(request_id or None)


@app.post("/api/medical/query")
async def medical_query(request: Request):
    """Execute a filter query with bounded, observable concurrency."""
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
    global_patient_id = data.get("global_patient_id", "").strip()
    global_visit_id = data.get("global_visit_id", "").strip()
    raw_visit_no = visit_no
    raw_global_visit_id = global_visit_id
    visit_no, global_patient_id, global_visit_id = _resolve_medical_query_visit_context(
        register_no,
        visit_no,
        global_patient_id,
        global_visit_id,
    )
    if (visit_no, global_visit_id) != (raw_visit_no, raw_global_visit_id):
        print(
            "[medical_query] resolved visit context | "
            f"register_no={register_no} | visit_no={visit_no or '(empty)'} | "
            f"global_visit_id={global_visit_id or '(empty)'}",
            flush=True,
        )
    judge_model = data.get("judge_model", "qwen2.5:3b")
    router_model = data.get("router_model", "medaibase/medgemma1.5:4b")
    planner_model = data.get("planner_model", "").strip() or "deepseek-r1:1.5b"  # 默认启用
    if not condition:
        return {"error": "condition is required"}, 400
    if not register_no and not global_patient_id:
        return {"error": "register_no or global_patient_id is required"}, 400

    import uuid
    from microharness.medical.query_concurrency import (
        MedicalQueryDuplicateId,
        MedicalQueryQueueFull,
        MedicalQueryQueueTimeout,
    )

    supplied_request_id = str(data.get("request_id", "")).strip()
    request_id = supplied_request_id[:128] or str(uuid.uuid4())
    coordinator = _get_medical_query_coordinator()
    trace_models = {
        "router": router_model,
        "judge": judge_model,
        "planner": planner_model,
    }

    def _request_trace(
        response=None,
        *,
        lifecycle_status="completed",
        error="",
        admission_state=None,
    ):
        from microharness.medical.request_trace import build_medical_query_trace

        return build_medical_query_trace(
            response or {},
            request_id=request_id,
            admission=admission_state or coordinator.snapshot(request_id),
            models=trace_models,
            lifecycle_status=lifecycle_status,
            error=error,
        )

    def _log_request_trace(trace):
        from microharness.medical.request_trace import medical_query_trace_log

        print(
            "[medical_query][trace] "
            + json.dumps(medical_query_trace_log(trace), ensure_ascii=False),
            flush=True,
        )

    try:
        admission = await coordinator.acquire(request_id)
        print(
            "[medical_query] admitted | "
            f"request_id={request_id} | active={admission['active_count']}/"
            f"{admission['max_concurrency']} | waiting={admission['queue_length']}",
            flush=True,
        )
    except MedicalQueryQueueFull:
        queue_state = coordinator.snapshot(request_id)
        trace = _request_trace(
            lifecycle_status="rejected",
            error="medical query queue is full",
            admission_state=queue_state,
        )
        _log_request_trace(trace)
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": "10"},
            content={
                "error": "当前病历筛选任务较多，等待队列已满，请稍后重试",
                "request_id": request_id,
                "queue": queue_state,
                "request_trace": trace,
            },
        )
    except MedicalQueryQueueTimeout:
        queue_state = coordinator.snapshot(request_id)
        trace = _request_trace(
            lifecycle_status="queue_timeout",
            error="medical query queue wait timed out",
            admission_state=queue_state,
        )
        _log_request_trace(trace)
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "10"},
            content={
                "error": "病历筛选排队等待超时，请稍后重试",
                "request_id": request_id,
                "queue": queue_state,
                "request_trace": trace,
            },
        )
    except MedicalQueryDuplicateId:
        queue_state = coordinator.snapshot(request_id)
        trace = _request_trace(
            lifecycle_status="rejected",
            error="duplicate active medical query request_id",
            admission_state=queue_state,
        )
        _log_request_trace(trace)
        return JSONResponse(
            status_code=409,
            content={
                "error": "request_id 对应的病历筛选任务仍在执行",
                "request_id": request_id,
                "queue": queue_state,
                "request_trace": trace,
            },
        )

    global _MEDICAL_QUERY_POOL
    if _MEDICAL_QUERY_POOL is None:
        from concurrent.futures import ThreadPoolExecutor
        _MEDICAL_QUERY_POOL = ThreadPoolExecutor(
            max_workers=coordinator.max_concurrency,
            thread_name_prefix="medquery-",
        )

    loop = asyncio.get_running_loop()
    work_future = None
    try:
        work_future = loop.run_in_executor(
            _MEDICAL_QUERY_POOL,
            _run_medical_query,
            condition, register_no, visit_no, global_patient_id, global_visit_id,
            judge_model, router_model, planner_model,
        )
        result = await asyncio.shield(work_future)
        coordinator.release(request_id, "completed", "执行完成")
        if isinstance(result, dict):
            result.setdefault("request_id", request_id)
            trace = _request_trace(result, lifecycle_status="completed")
            result["request_trace"] = trace
            _log_request_trace(trace)
        return result
    except asyncio.CancelledError:
        if work_future is None:
            coordinator.release(request_id, "cancelled", "请求已取消")
            raise
        coordinator.mark_disconnected(request_id)

        def _release_after_disconnect(future):
            try:
                future.result()
                coordinator.release(request_id, "completed", "后台执行完成")
            except asyncio.CancelledError:
                coordinator.release(request_id, "cancelled", "后台任务已取消")
            except Exception:
                coordinator.release(request_id, "failed", "后台执行失败")

        work_future.add_done_callback(_release_after_disconnect)
        raise
    except Exception as exc:
        coordinator.release(request_id, "failed", "执行失败")
        import traceback
        tb = traceback.format_exc()
        print(f"[medical_query] 未处理异常: {exc}\n{tb}", flush=True)
        error_response = {
            "request_id": request_id,
            "condition": condition,
            "register_no": register_no,
            "matched_count": 0,
            "判断状态": "无法判断",
            "可判定": False,
            "error": "病历筛选执行失败",
            "reason": str(exc)[:300],
        }
        if str(os.environ.get("MEDICAL_QUERY_DEBUG", "")).lower() in {"1", "true", "yes", "on"}:
            error_response["debug_trace"] = tb[-2000:]
        trace = _request_trace(
            error_response,
            lifecycle_status="failed",
            error=str(exc),
        )
        error_response["request_trace"] = trace
        _log_request_trace(trace)
        return error_response


def _precompute_hints(fields_text: str) -> str:
    """Scan binding fields for dates/numbers and pre-compute derived values.

    Automatically discovers date pairs and computes differences so small
    LLMs don't need to do arithmetic themselves. No hardcoded field names.
    Handles both per-field (label: val\\n) and compact (label: val | ...) formats.
    """
    import re
    from datetime import datetime

    hints = []
    datetime_re = re.compile(r'(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}(?::\d{2})?))?')

    # ── Parse all (label, value) pairs regardless of format ──
    pairs = []  # (label, value_str)
    for line in fields_text.split('\n'):
        line = line.strip()
        if not line:
            continue
        # Compact format: "  [前缀] 字段: 值 | 字段: 值 | ..."
        if ' | ' in line:
            # Remove leading record prefix like "[诊断1]"
            for part in line.split(' | '):
                part = part.strip()
                if ':' in part:
                    label, val = part.split(':', 1)
                    label = label.strip()
                    # Strip record prefix like "[诊断1]" or "  [就诊1]"
                    if label.startswith('['):
                        label = label.split('] ', 1)[-1] if '] ' in label else label.split(']', 1)[-1]
                    pairs.append((label, val.strip()))
        else:
            # Traditional per-field format: "  label: value"
            if ':' in line:
                label, val = line.split(':', 1)
                label = label.strip().split()[-1]  # last word as label
                pairs.append((label, val.strip()))

    # ── Date entries and pairwise differences ──
    date_entries = []
    for label, val in pairs:
        m = datetime_re.search(val)
        if m:
            time_part = m.group(2) or "00:00:00"
            if len(time_part) == 5:
                time_part += ":00"
            date_entries.append((label, f"{m.group(1)} {time_part}"))

    seen = set()
    for i in range(len(date_entries)):
        for j in range(i + 1, len(date_entries)):
            n1, d1 = date_entries[i]
            n2, d2 = date_entries[j]
            try:
                dt1 = datetime.strptime(d1, '%Y-%m-%d %H:%M:%S')
                dt2 = datetime.strptime(d2, '%Y-%m-%d %H:%M:%S')
                if dt2 >= dt1:
                    later_name, earlier_name = n2, n1
                    delta_seconds = (dt2 - dt1).total_seconds()
                else:
                    later_name, earlier_name = n1, n2
                    delta_seconds = (dt1 - dt2).total_seconds()
                diff = int(delta_seconds // 86400)
                hours = round(delta_seconds / 3600, 2)
                if 0 < diff < 365 * 10 and (later_name, earlier_name, "天") not in seen:
                    seen.add((later_name, earlier_name, "天"))
                    hints.append(f'[预计算] {later_name} - {earlier_name}(天) = {diff}天')
                if 0 < hours < 24 * 365 * 10 and (later_name, earlier_name, "小时") not in seen:
                    # 保留小时级预计算，支持"小于24小时/大于48小时"等时长条件。
                    seen.add((later_name, earlier_name, "小时"))
                    hints.append(f'[预计算] {later_name} - {earlier_name}(小时) = {hours}小时')
            except Exception:
                pass

    # ── Pure numeric values ──
    num_re = re.compile(r'^([+-]?\d+\.?\d*)\s*(岁|天|日|小时|分钟|周|月|个月|年|次|个|度|%)?$')
    for label, val in pairs:
        m = num_re.match(val.strip())
        if m:
            try:
                n = float(m.group(1))
                if 0 < abs(n) < 10000:
                    unit = m.group(2) or ""
                    hints.append(f'[预计算] {label} = {n}{unit}')
            except Exception:
                pass

    return '\n'.join(hints) if hints else ''


def _parse_cn_number(raw: str) -> Optional[float]:
    """Parse Arabic or Chinese numerals without relying on fixed query phrases."""
    from microharness.medical.temporal_parser import parse_cn_number
    return parse_cn_number(raw)


def _parse_numeric_comparison(condition: str) -> Optional[dict]:
    """Extract a generic numeric comparison from Chinese natural language."""
    from microharness.medical.temporal_parser import parse_numeric_comparison
    parsed = parse_numeric_comparison(condition)
    if not parsed:
        return None
    return {
        "keyword": parsed.subject,
        "op": parsed.operator,
        "threshold": parsed.threshold,
        "unit": parsed.unit,
    }


def _has_explicit_value_predicate(condition: str) -> bool:
    from microharness.medical.query_ir_validator import has_explicit_value_predicate
    return has_explicit_value_predicate(condition)


def _is_executable_numeric_condition(condition: str) -> bool:
    from microharness.medical.query_ir_validator import is_executable_numeric_condition
    return is_executable_numeric_condition(condition)


def _normalize_time_unit(unit: str) -> str:
    from microharness.medical.temporal_parser import normalize_time_unit
    return normalize_time_unit(unit)


def _convert_numeric_unit(value: float, from_unit: str, to_unit: str) -> float:
    """Convert common duration units for comparing precomputed hints."""
    from microharness.medical.temporal_parser import convert_numeric_unit
    return convert_numeric_unit(value, from_unit, to_unit)


def _is_time_scope_condition(
    text: str,
    *,
    temporal: object = None,
    allow_text_fallback: bool = True,
) -> bool:
    """Whether a condition needs admission/discharge dates as auxiliary scope."""
    if temporal is not None:
        if isinstance(temporal, dict):
            values = temporal.values()
        else:
            values = (
                getattr(temporal, "scope", ""),
                getattr(temporal, "event", ""),
                getattr(temporal, "relation", ""),
                getattr(temporal, "duration", None),
            )
        return any(value is not None and str(value).strip() for value in values)
    if not allow_text_fallback:
        return False
    return bool(re.search(
        r"(住院期间|住院期内|本次住院|入院后|入院前|入院时|出院前|出院后|出院时|"
        r"术前|术后|手术前|手术后|术中|手术中|"
        r"\d+\s*(?:分钟|小时|天|日|周|月|个月)\s*(?:内|前|后))",
        text or "",
    ))


def _event_anchor_route(
    text: str,
    *,
    temporal: object = None,
    allow_text_fallback: bool = True,
) -> tuple[list, list]:
    try:
        from microharness.medical.time_window import get_anchor_route_for_condition
        return get_anchor_route_for_condition(
            text or "",
            temporal=temporal,
            allow_text_fallback=allow_text_fallback,
        )
    except Exception:
        return [], []


def _prune_primary_service_route(
    condition: str,
    docs: list,
    sections: list,
    service_ids: list,
    *,
    temporal: object = None,
    allow_text_fallback: bool = True,
) -> tuple[list, list, str]:
    """Reduce noisy document routing when a structured service is the evidence source."""
    service_ids = service_ids or []
    if "lab-results" not in service_ids:
        return list(docs or []), list(sections or []), ""

    anchor_docs, anchor_sections = _event_anchor_route(
        condition,
        temporal=temporal,
        allow_text_fallback=allow_text_fallback,
    )
    if anchor_docs and anchor_sections:
        return anchor_docs, anchor_sections, "检验指标以lab-results为主证据，病历时间锚点作为辅助证据"
    if _is_time_scope_condition(
        condition,
        temporal=temporal,
        allow_text_fallback=allow_text_fallback,
    ):
        return [], [], "检验指标以lab-results为主证据，住院范围由就诊ID/就诊信息辅助限定，跳过病历正文检索"
    return [], [], "检验指标以lab-results为主证据，跳过病历正文检索"


def _primary_service_for_condition(
    condition: str,
    cond: dict | None = None,
    *,
    allow_text_fallback: bool = True,
) -> str:
    """Pick one primary structured evidence service for a sub-condition.

    This prevents unrelated services from contributing negative evidence, e.g.
    a drug condition should not be judged by lab-results just because another
    sibling condition needed lab data.
    """
    cond = cond or {}
    entity_type = str(cond.get("entity_type") or "")
    semantic_class = str(cond.get("semantic_class") or "")
    skills = set(cond.get("target_skills") or [])
    text = str(condition or "")
    if entity_type == "lab" or semantic_class == "检验指标" or "lab-results" in skills:
        return "lab-results"
    if entity_type == "drug" or semantic_class == "用药医嘱" or "drug-interaction" in skills:
        return "drug-interaction"
    if entity_type == "diagnosis" or semantic_class in {"疾病/症状存在", "入院前/既往存在"} or "diagnosis-query" in skills:
        return "diagnosis-query"
    if not allow_text_fallback:
        return ""
    try:
        from microharness.services.service_catalog import load_services

        drug_service = load_services().get("drug-interaction", {})
        triggers = drug_service.get("triggers", []) if isinstance(drug_service, dict) else []
    except Exception:
        triggers = []
    if any(str(token) and str(token) in text for token in triggers):
        return "drug-interaction"
    return ""


def _resolve_executable_route_sources(
    target_docs: list,
    service_candidates: list,
    document_catalog: dict,
    service_catalog: dict,
    table_map: dict,
) -> dict:
    """Resolve IR route candidates that can actually execute in this deployment."""
    requested_docs = list(dict.fromkeys(
        str(doc).strip() for doc in (target_docs or []) if str(doc).strip()
    ))
    executable_docs = []
    unresolved_docs = []
    for doc in requested_docs:
        document_metadata = (document_catalog or {}).get(doc)
        table_metadata = (table_map or {}).get(doc)
        if isinstance(document_metadata, dict) and isinstance(table_metadata, dict) and table_metadata.get("table"):
            executable_docs.append(doc)
        else:
            unresolved_docs.append(doc)

    requested_services = []
    for candidate in service_candidates or []:
        if isinstance(candidate, dict):
            service_id = candidate.get("id") or candidate.get("name")
        else:
            service_id = candidate
        service_id = str(service_id or "").strip()
        if service_id and service_id not in requested_services:
            requested_services.append(service_id)

    executable_services = []
    unresolved_services = []
    for service_id in requested_services:
        service_metadata = (service_catalog or {}).get(service_id)
        if isinstance(service_metadata, dict) and str(service_metadata.get("url") or "").strip():
            executable_services.append(service_id)
        else:
            unresolved_services.append(service_id)

    return {
        "documents": executable_docs,
        "services": executable_services,
        "unresolved_documents": unresolved_docs,
        "unresolved_services": unresolved_services,
        "should_fallback": not executable_docs and not executable_services,
    }


def _prejudge(condition: str, hints: str) -> Optional[dict]:
    """Try to answer simple numeric comparisons directly in Python, no LLM.

    Detects patterns like "住院天数小于20天" where the pre-computed hints
    already contain the value, and does the comparison deterministically.
    Returns None if can't answer (falls through to LLM judge).
    """
    import re as _re
    # Parse hints into dict. Also keep raw line for unit-based fallback matching.
    # "[预计算] encEndDate - encStartDate = 3天" → key="encEndDate - encStartDate", val=3, raw=full_line
    hint_values = {}   # key → float value
    hint_raw = {}      # key → original line (contains unit like "天")
    for line in hints.split('\n'):
        m = _re.match(r'\[预计算\]\s+(.+?)\s*=\s*([\d.]+)', line)
        if m:
            key = m.group(1).strip()
            try:
                hint_values[key] = float(m.group(2))
                hint_raw[key] = line
            except ValueError:
                pass
    if not hint_values:
        return None

    # Normalize condition: remove "天", "岁", "个" etc. units for matching
    cond_clean = condition.strip()

    parsed_cmp = _parse_numeric_comparison(cond_clean)
    if not parsed_cmp:
        return None

    keyword = parsed_cmp["keyword"]
    op_raw = parsed_cmp["op"]
    threshold = parsed_cmp["threshold"]
    cond_unit = _normalize_time_unit(parsed_cmp.get("unit", ""))
    if not cond_unit and keyword in {"住院天数", "住院时间", "住院时长", "住院日"}:
        cond_unit = "天"

    # Map operator to comparison function
    op_map = {
        '小于': lambda a, b: a < b, '少于': lambda a, b: a < b, '低于': lambda a, b: a < b,
        '大于': lambda a, b: a > b, '多于': lambda a, b: a > b, '超过': lambda a, b: a > b,
        '不超过': lambda a, b: a <= b, '至多': lambda a, b: a <= b, '以下': lambda a, b: a <= b,
        '不低于': lambda a, b: a >= b, '不少于': lambda a, b: a >= b,
        '至少': lambda a, b: a >= b, '以上': lambda a, b: a >= b,
        '等于': lambda a, b: a == b,
        '<': lambda a, b: a < b, '>': lambda a, b: a > b,
        '<=': lambda a, b: a <= b, '>=': lambda a, b: a >= b,
        '=': lambda a, b: a == b,
        '≤': lambda a, b: a <= b, '≥': lambda a, b: a >= b,
    }
    compare = op_map.get(op_raw)
    if not compare:
        return None
    op_display = {
        '小于': '<', '少于': '<', '低于': '<',
        '大于': '>', '多于': '>', '超过': '>',
        '不超过': '≤', '至多': '≤', '以下': '≤',
        '不低于': '≥', '不少于': '≥', '至少': '≥', '以上': '≥',
        '等于': '='
    }.get(op_raw, op_raw)

    def _hint_unit(raw_line: str) -> str:
        hm = _re.search(r'=\s*[\d.]+\s*(天|小时|分钟|岁|个|次|度|%)?', raw_line or "")
        return _normalize_time_unit(hm.group(1) if hm else "")

    def _threshold_for_hint(hint_key: str) -> float:
        h_unit = _hint_unit(hint_raw.get(hint_key, ""))
        return _convert_numeric_unit(threshold, cond_unit, h_unit)

    # Find matching hint value — partial match on key or keyword
    for hint_key, hint_val in hint_values.items():
        if keyword in hint_key or hint_key in keyword:
            threshold_cmp = _threshold_for_hint(hint_key)
            result = compare(hint_val, threshold_cmp)
            reason = f"{keyword} = {hint_val} {op_display} {threshold_cmp} → {'✓符合' if result else '✗不符合'}"
            return {"matched": result, "reason": reason}

    # Fallback: match by unit (e.g., "住院天数小于20天" → unit "天"
    # matches hint "[预计算] encEndDate - encStartDate = 3天" even though
    # "住院天数" doesn't literally appear in the parsed hint key)
    if cond_unit:
        unit = cond_unit
        unit_hints = {k: v for k, v in hint_values.items()
                      if unit in hint_raw.get(k, '')}
        if len(unit_hints) == 1:
            hk, hv = next(iter(unit_hints.items()))
            threshold_cmp = _threshold_for_hint(hk)
            result = compare(hv, threshold_cmp)
            reason = f"{keyword} ≈ {hk} = {hv} {op_display} {threshold_cmp} → {'✓符合' if result else '✗不符合'}"
            return {"matched": result, "reason": reason}

        # Compatible duration fallback: query may use "周/月/分钟" while hints
        # are stored as "天/小时". Pick the closest precomputed granularity.
        duration_units = {"分钟", "小时", "天", "周", "月"}
        if unit in duration_units:
            preferred_hint_unit = "小时" if unit in {"分钟", "小时"} else "天"
            compatible = {
                k: v for k, v in hint_values.items()
                if _hint_unit(hint_raw.get(k, "")) == preferred_hint_unit
            }
            if len(compatible) == 1:
                hk, hv = next(iter(compatible.items()))
                threshold_cmp = _threshold_for_hint(hk)
                result = compare(hv, threshold_cmp)
                reason = f"{keyword} ≈ {hk} = {hv} {op_display} {threshold_cmp} → {'✓符合' if result else '✗不符合'}"
                return {"matched": result, "reason": reason}

    return None


def _is_age_condition(condition: str, keyword: str = "") -> bool:
    """Age comparisons require an explicit age field; do not infer from context."""
    parsed = _parse_numeric_comparison(condition)
    if str(keyword or "").strip() == "年龄":
        return True
    if parsed and parsed.get("unit") == "岁":
        subject = str(parsed.get("keyword") or "").strip()
        return subject in {"", "年龄"}
    return bool(re.search(r"(年龄|岁\s*(?:以上|以下|及以上|及以下))", condition or ""))


def _is_numeric_only_condition(condition: str) -> bool:
    """条件是否只要求数值/日期比较？（无医学语义）

    "住院天数大于5天" → True     "血糖>7" → True
    "背痛的患者" → False          "高血压" → False
    "开了维生素B1的患者" → False  (B1中的数字不是比较条件)

    关键：必须有比较词(大于/小于等)紧邻数字，而非任意数字。
    药物名/诊断名中的数字不算数值条件。
    """
    from microharness.medical.temporal_parser import is_numeric_comparison
    return is_numeric_comparison(condition)


def _extract_core_keyword(condition: str) -> str:
    """从自然语言条件中提取核心关键字，用于字面子串校验。

    这不是语义理解，不涉及医学规则。
    只是机械地剥离中文语法功能词（的前缀/后缀/比较词/数字），
    剩下的就是用户真正想查的关键字。

    示例：
      "开了维生素B1的患者" → 去"开了"+去"的患者" → "维生素B1"
      "诊断为高血压"         → 去"诊断为"         → "高血压"
      "住院天数小于5天"      → 去"小于5天"       → "住院天数"（但数值条件不会走到这里，会被_prejudge拦截）
      "背痛"                 → 无修饰词可剥离    → "背痛"

    返回空字符串表示条件中提取不出关键字（纯数值条件或纯修饰词）。
    """
    import re as _kre
    text = condition.strip()
    # Strip generic time/phase context and result-state words before literal
    # matching. This keeps service prefilters focused on the clinical entity
    # ("白细胞计数") instead of the whole natural-language clause
    # ("住院期间白细胞计数指标偏高").
    num_pat = r'(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)'
    unit_pat = r'(?:天|日|小时|分钟|周|月|个月)'
    text = _kre.sub(
        rf'(^.+?\s*(?:前|后)\s*(?:当天|当日)|'
        rf'^.+?\s*(?:前|后)\s*{num_pat}\s*{unit_pat}(?:内|前|后)?|'
        rf'^.+?\s*{num_pat}\s*{unit_pat}\s*(?:前|后|内)|'
        rf'(?:当天|当日)|'
        rf'住院期间|住院期内|入院前|入院后|出院前|出院后|术前|术后|手术前|手术后|'
        rf'第?{num_pat}\s*{unit_pat}(?:内|前|后)?)',
        '',
        text,
    )
    text = _kre.sub(r'^(手术|入院|出院)(?:时|中|期间)?', '', text)
    text = _kre.sub(r'(指标|检验|化验|项目|结果|数值|水平|计数值)', '', text)
    text = _kre.sub(
        r'(偏高|偏低|升高|降低|增高|减少|异常|阳性|阴性|高于参考范围|低于参考范围)',
        '',
        text,
    )
    # 剥离句首动词/助词："开了XX"、"患有XX"、"有诊断有XX" → "XX"。
    # 小模型和用户口语常会叠加功能词，循环剥离避免留下"诊断有XX"。
    prefix_pat = r'^(该患者|此患者|患者|该病人|此病人|病人|该病例|此病例|病例|患儿|是否|有无|既往有|既往患有|既往存在|既往诊断为|既往诊断有|既往|有诊断为|有诊断有|有诊断|诊断为|诊断有|确诊为|确诊有|开了|开过|服用了|服用过|使用了|使用过|注射了|注射过|吃了|吃过|打了|用了|用过|做了|做过|患有|存在|有过|有)'
    while True:
        stripped = _kre.sub(prefix_pat, '', text)
        if stripped == text:
            break
        text = stripped
    text = _kre.sub(
        r'\s*(大于|小于|高于|低于|不超过|不低于|等于|>=|<=|>|<|=|≥|≤|＞|＜)\s*'
        r'[\d.]+\s*(?:[×x*]\s*10\S*)?(?:\S*/\S*)?(?:岁|天|日|小时|分钟|周|月|个月|次|分|度|%)?',
        '',
        text,
    )
    # 剥离句尾修饰语："XX的患者"、"XX的病人" → "XX"
    text = _kre.sub(r'(的患者|的病人|的病例|的人|了)$', '', text)
    # 病史/既往史是语义修饰，不是疾病本体；"有高血压史"核心词应为"高血压"。
    text = _kre.sub(r'(疾病史|既往史|病史|史)$', '', text)
    # 剥离比较运算符+数字+单位："小于5天"、"大于3次" → 删掉，保留前面的主体
    text = _kre.sub(r'\s*(小于|大于|不超过|不低于|等于|>=|<=|>|<|=|≥|≤)\s*[\d.]+\s*(天|岁|个|次|分|度|%|mmol)?', '', text)
    # 剥离残余虚词/空白
    text = _kre.sub(r'[\s　的么吗呢]+', '', text)
    # 取最长的连续片段作为关键字（至少2个字符，避免过于泛化的单字匹配）
    chunks = [c for c in _kre.split(r'[,，;；、\s]+', text) if len(c) >= 2]
    return max(chunks, key=len) if chunks else ""


def _char_overlap_match(keyword: str, text: str, threshold: float = 0.75) -> bool:
    """Check if enough characters of keyword appear in text (not just contiguous substring).

    Why "背痛" → "胸背部疼痛": str.contains says False (不连续), this says True (背+痛都在).
    Why "背部痛" → "背痛": 2/3 chars match → True (允许差1字).
    Why "维生素B1" → "维生素B1片": both approaches say True.
    Why "糖尿病" → "血糖偏高": 0/3 → False.

    Rule: 2 chars must all match; 3+ chars allow at most 1 missing.
    """
    if not keyword or not text:
        return False
    kw_chars = list(keyword)
    found = sum(1 for c in kw_chars if c in text)
    if len(kw_chars) <= 2:
        return found == len(kw_chars)  # all must match
    return found >= len(kw_chars) - 1  # allow 1 missing


def _append_unique(seq: list, values: list) -> list:
    """Append values while preserving order."""
    from microharness.medical.semantic_rules import append_unique
    return append_unique(seq, values)


def _is_duration_comparison_condition(condition: str) -> bool:
    """Generic duration comparison detector, independent of disease/drug terms."""
    from microharness.medical.temporal_parser import is_duration_comparison
    return is_duration_comparison(condition)


def _split_compound_clauses(condition: str) -> tuple[list, str]:
    """Conservative split for explicit Chinese AND/OR connectors."""
    from microharness.medical.semantic_rules import split_compound_clauses
    return split_compound_clauses(condition)


def _augment_medical_analysis_routes(analysis: dict, original_condition: str) -> dict:
    from microharness.medical.semantic_rules import augment_analysis_routes
    return augment_analysis_routes(
        analysis, original_condition, fallback_keyword_fn=_extract_core_keyword
    )


def _maybe_split_compound_analysis(analysis: dict, original_condition: str) -> dict:
    from microharness.medical.semantic_rules import maybe_split_compound_analysis
    return maybe_split_compound_analysis(
        analysis, original_condition, fallback_keyword_fn=_extract_core_keyword
    )


def _preserve_literal_clause_texts(analysis: dict, original_condition: str) -> dict:
    from microharness.medical.query_ir_validator import preserve_literal_clause_texts
    return preserve_literal_clause_texts(
        analysis,
        original_condition,
        fallback_keyword_fn=_extract_core_keyword,
    )


def _preserve_single_temporal_condition(analysis: dict, original_condition: str) -> dict:
    from microharness.medical.query_ir_validator import preserve_single_temporal_condition
    return preserve_single_temporal_condition(
        analysis,
        original_condition,
        fallback_keyword_fn=_extract_core_keyword,
    )


def _is_non_executable_subcondition(text: str) -> bool:
    from microharness.medical.query_structure import is_non_executable_subcondition
    return is_non_executable_subcondition(text)


def _augment_structural_conditions(analysis: dict, original_condition: str) -> dict:
    from microharness.medical.query_structure import augment_structural_conditions
    return augment_structural_conditions(
        analysis,
        original_condition,
        fallback_keyword_fn=_extract_core_keyword,
        executable_numeric_fn=_is_executable_numeric_condition,
    )


def _repair_analysis_structure(analysis: dict, original_condition: str) -> dict:
    from microharness.medical.query_structure import repair_analysis_structure
    return repair_analysis_structure(
        analysis, original_condition, fallback_keyword_fn=_extract_core_keyword
    )


def _decompose_semantic(condition: str, model: str) -> dict:
    """LLM 拆解子条件 → 核心关键词 + 语义修饰词 + 追加章节。

    "背痛治好的患者" → keyword="背痛", modifiers=["治好"], extra_sections=["出院情况","诊疗经过"]
    "住院天数小于5天" → keyword="住院天数", modifiers=[], extra_sections=[] (数值，无修饰词)
    "开了阿司匹林" → keyword="阿司匹林", modifiers=[], extra_sections=[]

    修饰词不是写死在代码里的——LLM 根据语义自主判断。
    extra_sections 由 LLM 根据 DOCUMENT_CATALOG 章节用途自主推理，
    代码只做校验（过滤不存在的章节名）。
    """
    from microharness.medical.query_router import DOCUMENT_CATALOG
    from microharness.ollama.model_profile import get_profile as _dmp
    from microharness.ollama.prompt_adapter import build_decompose_prompt
    _dprofile = _dmp(model)
    # Build compact section catalog for LLM reference
    sec_catalog = {}
    for doc, info in DOCUMENT_CATALOG.items():
        sec_catalog[doc] = {s["name"]: s["purpose"] for s in info.get("sections", [])}

    prompt = build_decompose_prompt(_dprofile, condition, sec_catalog)
    try:
        from microharness.ollama import OllamaClient
        from microharness.medical.query_router import parse_llm_json
        c = OllamaClient(model=model, timeout=60,
                        format_json=(_dprofile.json_mode == "format_json"))
        resp = c.chat([{"role": "user", "content": prompt}], temperature=0.1)
        result = parse_llm_json(resp, context=f"语义拆解:{condition[:30]}")
        if isinstance(result, dict) and result.get("keyword"):
            # Validate modifiers: filter out numeric comparisons (not real modifiers)
            import re as _dmre
            mods = result.get("modifiers", [])
            if isinstance(mods, list):
                result["modifiers"] = [m for m in mods
                    if isinstance(m, str) and len(m) >= 1
                    and not _dmre.search(r'\d', m)  # no numbers
                    and m not in ("患者", "病人", "病例")  # not generic words
                    and _char_overlap_match(m, condition)]  # must exist in query
            else:
                result["modifiers"] = []
            # Validate extra_sections against catalog
            valid_secs = set()
            for doc, info in DOCUMENT_CATALOG.items():
                for s in info.get("sections", []):
                    valid_secs.add(s["name"])
            raw = result.get("extra_sections", [])
            if isinstance(raw, list):
                result["extra_sections"] = [s for s in raw if s in valid_secs]
                filtered = [s for s in raw if s not in valid_secs]
                if filtered:
                    print(f"[语义拆解] 过滤无效章节{filtered}", flush=True)
            else:
                result["extra_sections"] = []
            # If no valid modifiers, clear extra_sections too (no need to check)
            if not result["modifiers"]:
                result["extra_sections"] = []
            print(f"[语义拆解] {condition[:40]} → kw={result.get('keyword','')} mod={result.get('modifiers',[])} sec={result.get('extra_sections',[])}", flush=True)
            return result
    except Exception as e:
        print(f"[语义拆解] 失败({condition[:30]}): {e}", flush=True)
    # Fallback: return condition as-is
    return {"keyword": condition, "modifiers": [], "extra_sections": []}


# ═══════════════════════════════════════════════════════════════════
# Shared DB query helper — usable by both legacy pipeline and scheduler
# ═══════════════════════════════════════════════════════════════════

def _query_db(sq_route: dict, register_no: str, visit_no: str,
              global_patient_id: str, global_visit_id: str,
              log_fn=None, db_health_check=None) -> list:
    """Query DB for tables+columns specified by this route.

    Extracted from _run_medical_query so the scheduler's query_db action
    can reuse the same logic. Returns list of binding dicts.
    """
    if log_fn is None:
        log_fn = lambda msg: None

    from microharness.database.field_mapper import TABLE_MAP, DOC_FIELDS, COMMON_FIELDS, find_db_column
    from microharness.database.db_client import get_db as get_database
    from microharness.medical.patient_query import (
        MissingPatientIdentityError,
        build_patient_where_clause,
    )

    targets = sq_route.get("targets", {})
    if not targets:
        targets = {d: sq_route.get("target_sections", []) for d in sq_route.get("target_medical_doc", [])}
    if not targets:
        return []

    def _target_summary() -> str:
        parts = []
        for doc, sections in (targets or {}).items():
            sec_list = [str(s) for s in (sections or []) if str(s).strip()]
            if sec_list:
                parts.append(f"{doc}（{ '、'.join(sec_list[:8]) }）")
            else:
                parts.append(str(doc))
        return "；".join(parts) if parts else "目标病历章节"

    def _db_unavailable_result(debug_error: str = "") -> list:
        target_text = _target_summary()
        user_message = f"未取得病历文档数据：{target_text}，当前无法用这些章节判断"
        return [{
            "file": "病历文档查询 (未取得数据)",
            "template": "Database",
            "bindings": [
                {"html_field": "数据源状态", "value": "未取得数据", "xml_path": "db/status"},
                {"html_field": "目标章节", "value": target_text, "xml_path": "db/target_sections"},
                {"html_field": "说明", "value": user_message, "xml_path": "db/message"},
            ],
            "visit_no": visit_no or "",
            "service_error": True,
            "error": user_message,
            "debug_error": str(debug_error or "")[:200],
        }]

    def _missing_patient_identity_result() -> list:
        user_message = "缺少患者或就诊标识，已停止病历文档查询，避免扩大查询范围"
        return [{
            "file": "病历文档查询 (缺少患者身份)",
            "template": "Database",
            "bindings": [
                {"html_field": "数据源状态", "value": "未执行", "xml_path": "db/status"},
                {"html_field": "目标章节", "value": _target_summary(), "xml_path": "db/target_sections"},
                {"html_field": "说明", "value": user_message, "xml_path": "db/message"},
            ],
            "visit_no": "",
            "service_error": True,
            "error": user_message,
            "error_code": "MISSING_PATIENT_IDENTITY",
        }]

    def _compact_db_error(err) -> str:
        text = re.sub(r"\s+", " ", str(err or "")).strip()
        if not text or text in {"(0, '')", "(0, \"\")"}:
            return "数据库查询未返回有效错误信息"
        if "Packet sequence number wrong" in text:
            return "数据库连接状态异常，已丢弃当前连接，后续查询将重新连接"
        return text[:160]

    _TEXT_COLS = {"chief_complaint","present_illness_history","past_medical_history",
        "social_history","maritalandobstetric_history","menstrual_history","family_history",
        "physical_examination","specific_findings","investigations","tcm_four_findings",
        "preliminary_diagnosis","admission_status","admission_diagnosis","discharge_diagnosis",
        "clinical_course","discharge_status","discharge_orders","surgical_procedure",
        "intra_op_events","progress_note","case_characteristics","diagnostic_basis",
        "differential_diagnosis","treatment_plan","diagnosis","allergies","note",
        "pre_op_diagnosis","intra_op_diagnosis"}

    try:
        patient_where = build_patient_where_clause(
            register_no=register_no,
            visit_no=visit_no,
            global_patient_id=global_patient_id,
            global_visit_id=global_visit_id,
        )
    except MissingPatientIdentityError:
        log_fn("  [DB] 缺少患者或就诊标识，病历文档查询已停止")
        return _missing_patient_identity_result()

    # Lazy DB availability check. The main medical-query pipeline supplies a
    # request-scoped checker so parallel sub-conditions share one test call.
    try:
        db = get_database()
        log_fn(f"  [DB] 当前启用数据库: {str(getattr(db, 'config', {}).get('type', 'iris')).lower()}")
        if db_health_check is None:
            db_ok, db_error = bool(db.test()), ""
        else:
            db_ok, db_error = db_health_check(db)
        if not db_ok:
            return _db_unavailable_result(db_error or "数据库连通性检测未通过")
    except Exception as e:
        return _db_unavailable_result(str(e))

    results = []
    db_errors = []
    try:
        for doc_title, doc_sections in targets.items():
            info = TABLE_MAP.get(doc_title, {})
            table = info.get("table", "")
            if not table: continue
            columns = set()
            matched_cols = {}
            missed_secs = []
            for sec in doc_sections:
                col = find_db_column(doc_title, sec)
                if col:
                    columns.add(col)
                    matched_cols[sec] = col
                else:
                    missed_secs.append(sec)
            clinical_cols = {c for c in columns if c not in ("registerno","visitnumber","emr_hosdocid","patient_name","papat_relpatientid","paadm_relvisitnumber")}
            if not clinical_cols:
                doc_avail = list(DOC_FIELDS.get(doc_title, {}).keys())
                log_fn(f"  [DB] ⚠️ {doc_title}: 路由章节{list(doc_sections)}全部未映射→跳过 | 可用: {doc_avail}")
                continue
            if missed_secs:
                known_sections = set(COMMON_FIELDS.keys())
                for _doc_map in DOC_FIELDS.values():
                    known_sections.update(_doc_map.keys())
                ignored_secs = [s for s in missed_secs if s in known_sections]
                unknown_secs = [s for s in missed_secs if s not in known_sections]
                detail = []
                if ignored_secs:
                    detail.append(f"忽略非本表章节{ignored_secs}")
                if unknown_secs:
                    detail.append(f"未映射{unknown_secs}")
                if detail:
                    log_fn(f"  [DB] {doc_title}: 命中{matched_cols} | {' | '.join(detail)}")
            for c in ["registerno","visitnumber","emr_hosdocid","patient_name","papat_relpatientid","paadm_relvisitnumber"]:
                columns.add(c)
            select_parts = [f"SUBSTRING({c},1,4000) as {c}" if c in _TEXT_COLS else c for c in columns]
            sql = f"SELECT {', '.join(select_parts)} FROM {table} WHERE {patient_where.strict_where}"
            if str(os.environ.get("MEDICAL_QUERY_DEBUG", "")).lower() in {"1", "true", "yes", "on"}:
                log_fn(f"  [DB][debug] SQL: {sql[:500]}")
            try:
                rows = db.client.execute(sql)
                if not rows and patient_where.fallback_where:
                    fallback_sql = (
                        f"SELECT {', '.join(select_parts)} FROM {table} "
                        f"WHERE {patient_where.fallback_where}"
                    )
                    log_fn(
                        "  [DB] 严格条件0行 → 使用本地患者/就诊标识重查 "
                        f"({', '.join(patient_where.fallback_fields)})"
                    )
                    if str(os.environ.get("MEDICAL_QUERY_DEBUG", "")).lower() in {"1", "true", "yes", "on"}:
                        log_fn(f"  [DB][debug] fallback SQL: {fallback_sql[:500]}")
                    rows = db.client.execute(fallback_sql)
                if not rows:
                    log_fn(f"  [DB] 返回0行 ({table})")
                field_map = DOC_FIELDS.get(doc_title, {})
                rev_map = {v: k for k, v in field_map.items()}
                rev_common = {v: k for k, v in COMMON_FIELDS.items()}
                for row in rows:
                    bindings = []
                    for col, val in row.items():
                        if val and str(val).strip() and col not in ("registerno","visitnumber","emr_hosdocid","papat_relpatientid","paadm_relvisitnumber"):
                            field_name = rev_map.get(col) or rev_common.get(col) or col
                            bindings.append({"html_field": field_name, "value": str(val), "xml_path": col})
                    if bindings:
                        rid = row.get("emr_hosdocid","") or row.get("registerno","")
                        results.append({"file": f"{doc_title}" + (f" ({rid})" if rid else ""),
                            "template": info.get("doc_type", doc_title),
                            "bindings": bindings, "visit_no": row.get("visitnumber", "")})
            except Exception as e:
                if "not found" in str(e) or "SQLCODE: -30" in str(e):
                    pass
                else:
                    db_errors.append(f"{doc_title}: {_compact_db_error(e)}")
                    log_fn(f"  [DB] {doc_title}: 未取得数据（{_compact_db_error(e)}）")
    except Exception as e:
        db_errors.append(_compact_db_error(e))
        log_fn(f"  [DB] 病历文档查询未取得数据（{_compact_db_error(e)}）")
    if not results and db_errors:
        return _db_unavailable_result("；".join(db_errors))
    return results


def _source_display_label(source_id: str, service_catalog: dict | None = None) -> str:
    """Resolve a user-facing source name from the current dynamic catalog."""
    source_key = str(source_id or "").split(".", 1)[0].strip()
    metadata = (service_catalog or {}).get(source_key)
    if isinstance(metadata, dict):
        return str(
            metadata.get("label")
            or metadata.get("display_name")
            or metadata.get("name")
            or source_key
        ).strip()
    return source_key


def _run_medical_query(condition: str, register_no: str, visit_no: str,
                       global_patient_id: str, global_visit_id: str,
                       judge_model: str, router_model: str,
                       planner_model: str = None) -> dict:
    """All blocking LLM/DB work runs in thread pool so other endpoints stay responsive."""
    _full_query_start = time.perf_counter()
    _stage_timings = {
        "normalization_ms": 0,
        "metadata_ms": 0,
        "understanding_ms": 0,
        "evidence_plan_ms": 0,
        "structured_services_ms": 0,
        "condition_execution_ms": 0,
        "evidence_enrichment_ms": 0,
        "explanation_polish_ms": 0,
    }

    def _record_stage(name: str, started_at: float) -> int:
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        _stage_timings[name] = elapsed_ms
        return elapsed_ms

    def _finalize_timing_response(response: dict) -> dict:
        total_ms = int((time.perf_counter() - _full_query_start) * 1000)
        response["timings"] = {**_stage_timings, "total_ms": total_ms}
        response["total_ms"] = total_ms
        return response

    import microharness.medical.query_router as medical_query_router
    QueryRouter = medical_query_router.QueryRouter
    from microharness.medical.field_catalog import get_catalog
    import sys
    log = lambda msg: (print(msg, flush=True), sys.stdout.flush())
    debug_enabled = str(os.environ.get("MEDICAL_QUERY_DEBUG", "")).lower() in {"1", "true", "yes", "on"}
    debug_log = lambda msg: log(msg) if debug_enabled else None
    def _compact_log_text(value, limit: int = 160) -> str:
        text = re.sub(r"<[^>]+>", " ", str(value or ""))
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]
    def _service_result_summary(results) -> str:
        results = results or []
        errors = [r for r in results if isinstance(r, dict) and r.get("service_error")]
        if errors:
            msg = _compact_log_text(errors[0].get("error", "未取得结构化接口数据"), 120)
            return msg
        if len(results) == 1 and isinstance(results[0], dict):
            file_name = str(results[0].get("file", ""))
            m = re.search(r"\((\d+)条\)", file_name)
            if m:
                return f"{m.group(1)}条记录"
        return f"{len(results)}组结果"
    original_condition = condition
    _stage_started = time.perf_counter()
    try:
        from microharness.agent.query_normalizer import normalize_query
        _normalization = normalize_query(condition, model=router_model)
        condition = _normalization.normalized or condition
        log(f"[归一化] {original_condition} → {condition} ({_normalization.source}, confidence={_normalization.confidence})")
    except Exception as _norm_e:
        _normalization = None
        log(f"[归一化] 跳过: {_norm_e}")
    _record_stage("normalization_ms", _stage_started)

    _field_labels_for_response = {}
    _service_catalog_for_evidence_plan = {}
    try:
        from microharness.services.service_catalog import load_services as _load_services_for_response
        _service_catalog_for_evidence_plan = _load_services_for_response()
        for _svc in _service_catalog_for_evidence_plan.values():
            if isinstance(_svc, dict):
                _field_labels_for_response.update(_svc.get("field_labels") or {})
    except Exception:
        pass

    def _sanitize_response(obj, preserve_machine_fields: bool = False):
        """Remove internal routing fields that should not be user-facing."""
        if isinstance(obj, dict):
            machine_value_keys = {
                "status",
                "reason_code",
                "data_quality",
                "source_role",
                "conflict_level",
                "condition_id",
                "source_id",
                "service_id",
                "source_kind",
                "source_label",
                "domain",
                "entity_type",
                "evidence_type",
                "evidence_types",
                "record_type",
                "record_id",
                "record_id_label",
                "record_id_field",
                "record_id_fields",
                "evidence_model_version",
            }
            return {
                k: _sanitize_response(
                    v,
                    preserve_machine_fields=(
                        preserve_machine_fields
                        or k in machine_value_keys
                        or k in {"evidence_items", "condition_result", "condition_results"}
                    ),
                )
                for k, v in obj.items()
                if k not in {"target_skills", "cot_response"}
            }
        if isinstance(obj, list):
            return [_sanitize_response(v, preserve_machine_fields) for v in obj]
        if isinstance(obj, str):
            if preserve_machine_fields:
                return obj
            cleaned = obj
            for eng, label in _field_labels_for_response.items():
                if eng and label:
                    cleaned = cleaned.replace(eng, label)
            try:
                from microharness.medical.display_text import sanitize_user_text
                cleaned = sanitize_user_text(cleaned)
            except Exception:
                pass
            return cleaned
        return obj

    def _judgment_status(
        matched: bool,
        reason: str,
        per_condition: dict = None,
        *,
        use_and: bool = True,
    ) -> tuple[str, bool]:
        from microharness.medical.evidence import judgment_status
        return judgment_status(matched, reason, per_condition, use_and=use_and)

    # Reject requests outside the medical-filter capability before loading
    # metadata or invoking the query-understanding model.
    from microharness.medical.scope_guard import (
        build_scope_rejection_response,
        evaluate_medical_filter_scope,
    )
    _scope_decision = evaluate_medical_filter_scope(condition)
    if not _scope_decision.allowed:
        log(f"[ScopeGuard] rejected: {_scope_decision.code} ({_scope_decision.signals})")
        _scope_result = build_scope_rejection_response(
            condition,
            _scope_decision,
            original_condition=original_condition,
        )
        if _normalization is not None:
            _scope_result["\u67e5\u8be2\u5f52\u4e00\u5316"] = _normalization.to_dict()
        return _finalize_timing_response(_sanitize_response(_scope_result))

    _stage_started = time.perf_counter()
    query_document_catalog, source_status = medical_query_router.reload_document_catalog_snapshot()
    log(medical_query_router.format_catalog_source_log("[病历筛选元数据实时刷新]", source_status))
    _record_stage("metadata_ms", _stage_started)

    # ═══════════════════════════════════════════════════════════════════
    # Stage 0: Unified Query Understanding (1 LLM call replaces 4 stages)
    # Merges: analyze_query + router.route + _decompose_semantic + match_services
    # ═══════════════════════════════════════════════════════════════════
    from microharness.agent.query_understanding import understand_query
    from microharness.medical.query_ir import build_query_ir
    _stage_started = time.perf_counter()
    analysis = understand_query(
        condition,
        model=router_model,
        document_catalog=query_document_catalog,
    )
    analysis = _repair_analysis_structure(analysis, condition)
    _query_ir = build_query_ir(analysis, condition)
    from microharness.medical.query_ir_quality import (
        assess_query_ir,
        build_ir_ambiguity_response,
    )
    _ir_quality = assess_query_ir(_query_ir, condition, analysis)
    _ir_retried = False
    if not _ir_quality.valid:
        _ir_retried = True
        retry_feedback = "\n".join(
            f"- {item.code}: {item.message}"
            for item in _ir_quality.issues
        )
        log(f"[IR质量门禁] 首次IR不完整，执行一次结构化重试: {retry_feedback.replace(chr(10), '；')}")
        retry_analysis = understand_query(
            condition,
            model=router_model,
            document_catalog=query_document_catalog,
            retry_feedback=retry_feedback,
        )
        retry_analysis = _repair_analysis_structure(retry_analysis, condition)
        retry_query_ir = build_query_ir(retry_analysis, condition)
        retry_quality = assess_query_ir(retry_query_ir, condition, retry_analysis)
        analysis = retry_analysis
        _query_ir = retry_query_ir
        _ir_quality = retry_quality

    if not _ir_quality.valid:
        log(
            "[IR质量门禁] 重试后仍存在关键歧义，停止执行: "
            + "；".join(item.code for item in _ir_quality.issues)
        )
        ambiguity_result = build_ir_ambiguity_response(
            condition,
            _query_ir,
            _ir_quality,
            original_condition=original_condition,
            analysis=analysis,
            retried=_ir_retried,
        )
        if _normalization is not None:
            ambiguity_result["查询归一化"] = _normalization.to_dict()
        _record_stage("understanding_ms", _stage_started)
        return _finalize_timing_response(_sanitize_response(ambiguity_result))

    log(
        f"[IR质量门禁] 通过 | score={_ir_quality.score:.2f} "
        f"| warnings={len(_ir_quality.warnings)} | retried={'是' if _ir_retried else '否'}"
    )
    _record_stage("understanding_ms", _stage_started)
    from microharness.medical.evidence_plan import (
        apply_evidence_plan_to_analysis,
        build_evidence_plan,
    )
    _stage_started = time.perf_counter()
    _evidence_plan = build_evidence_plan(
        _query_ir,
        document_catalog=query_document_catalog,
        service_catalog=_service_catalog_for_evidence_plan,
    )
    _planned_source_count = sum(len(item.sources) for item in _evidence_plan.conditions)
    log(
        f"[EvidencePlan] 条件数={len(_evidence_plan.conditions)} "
        f"| 候选来源={_planned_source_count} "
        f"| 未解析={_evidence_plan.unresolved_count}"
    )
    analysis = apply_evidence_plan_to_analysis(analysis, _evidence_plan)
    _consumed_source_count = sum(
        len(item.get("evidence_plan_source_ids") or [])
        for item in analysis.get("conditions", [])
        if isinstance(item, dict)
    )
    log(f"[EvidencePlan] 已注入现有执行链来源={_consumed_source_count}")
    _record_stage("evidence_plan_ms", _stage_started)

    # Negation
    _negate = analysis.get("negated", False)
    if _negate:
        log(f"[分析] 否定查询 → 结果将取反")

    # ═══════════════════════════════════════════════════════════════════
    # Stage 1: Route by query type (scheduler for temporal)
    # ═══════════════════════════════════════════════════════════════════
    # Temporal check: query has time-offset pattern or temporal indicator.
    _num_pat = r'(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)'
    _unit_pat = r'(?:分钟|小时|天|日|周|月|个月)'
    try:
        from microharness.medical.time_window import requires_period_window
        _period_needed = requires_period_window(condition)
    except Exception:
        _period_needed = False
    _has_time_offset = bool(
        re.search(rf'{_num_pat}\s*{_unit_pat}\s*[前后内]', condition) or
        re.search(rf'[前后]\s*{_num_pat}\s*{_unit_pat}', condition) or
        re.search(rf'第\s*{_num_pat}\s*{_unit_pat}', condition) or
        re.search(r'[前后]?(当天|当日)', condition) or
        _period_needed
    )
    _is_compound_temporal = (
        str(analysis.get("type", "")).lower() == "compound"
        or len(analysis.get("conditions", []) or []) > 1
        or bool(analysis.get("connector"))
    )
    _scheduler_started = time.perf_counter()
    from microharness.medical.semantic_rules import uses_deterministic_medication_pipeline
    _use_unified_medication_pipeline = uses_deterministic_medication_pipeline(analysis)
    if _has_time_offset and planner_model and _use_unified_medication_pipeline:
        log('[Scheduler] 时间用药条件 -> 使用统一结构化用药管线')
    if _has_time_offset and planner_model and not _is_compound_temporal and not _use_unified_medication_pipeline:
        # ── Scheduler pipeline ──
        try:
            from microharness.agent.scheduler.planner import QueryPlanner
            planner = QueryPlanner(model=planner_model)
            plan = planner.generate_plan(condition, analysis=analysis, router_model=router_model)
            if plan and plan.get("plan"):
                log(f"[Scheduler] 执行计划 {len(plan['plan'])} 步 → 执行引擎")
                from microharness.agent.scheduler.executor import ExecutionEngine
                from microharness.agent.scheduler.tools import ExecutionContext as ExecCtx
                ctx = ExecCtx(
                    condition=condition,
                    register_no=register_no, visit_no=visit_no,
                    global_patient_id=global_patient_id, global_visit_id=global_visit_id,
                    router_model=router_model, judge_model=judge_model,
                )
                engine = ExecutionEngine(ctx)
                result = engine.execute(plan["plan"])
                if result.get("results") and not result["results"][0].get("reason", "").startswith("调度层回退"):
                    first_result = result["results"][0]
                    if not first_result.get("per_condition"):
                        log("[Scheduler] 结果缺少per_condition证据 → 走现有管线")
                        raise RuntimeError("scheduler result missing per_condition evidence")
                    result["查询IR"] = _query_ir.to_dict()
                    result["IR质量"] = _ir_quality.to_dict() | {"retried": _ir_retried}
                    result["证据计划"] = _evidence_plan.to_dict()
                    if _negate and result.get("results"):
                        # Check internal negation (same logic as main path)
                        _has_int_neg = any(
                            any(neg in m for neg in ("没有", "无", "不", "未", "没"))
                            for c in analysis.get("conditions", [])
                            for m in c.get("modifiers", [])
                        )
                        if not _has_int_neg:
                            result["results"][0]["matched"] = not result["results"][0].get("matched", False)
                            result["results"][0]["reason"] = f"[取反] {result['results'][0].get('reason', '')}"
                        result["matched_count"] = 1 if result["results"][0]["matched"] else 0
                    from microharness.medical.evidence import assess_patient_confidence
                    patient_confidence = assess_patient_confidence(
                        bool(first_result.get("matched")),
                        str(first_result.get("reason", "")),
                        first_result.get("per_condition", {}),
                        use_and=_use_and,
                    )
                    status = patient_confidence["判断状态"]
                    conclusive = patient_confidence["可判定"]
                    first_result["判断状态"] = status
                    first_result["可判定"] = conclusive
                    first_result["置信度"] = patient_confidence["置信度"]
                    first_result["置信等级"] = patient_confidence["置信等级"]
                    first_result["依据等级"] = patient_confidence["依据等级"]
                    result["判断状态"] = status
                    result["可判定"] = conclusive
                    result["置信度"] = patient_confidence["置信度"]
                    result["置信等级"] = patient_confidence["置信等级"]
                    result["依据等级"] = patient_confidence["依据等级"]
                    result["原始条件"] = original_condition
                    result["规范条件"] = condition
                    if _normalization is not None:
                        result["查询归一化"] = _normalization.to_dict()
                    _record_stage("condition_execution_ms", _scheduler_started)
                    _enrichment_started = time.perf_counter()
                    from microharness.medical.evidence import enrich_response_with_evidence_model
                    result = enrich_response_with_evidence_model(result, _query_ir)
                    _record_stage("evidence_enrichment_ms", _enrichment_started)
                    return _finalize_timing_response(_sanitize_response(result))
                log(f"[Scheduler] 执行引擎回退 → 走现有管线")
            else:
                log(f"[Scheduler] 计划生成失败 → 走现有管线")
        except Exception as e:
            log(f"[Scheduler] 调度层异常({e}) → 走现有管线")
    elif _has_time_offset and planner_model and _is_compound_temporal:
        log("[Scheduler] 复合时间条件 → 跳过调度层，使用统一子条件管线")

    # ── Unified pipeline (uses understand_query results directly) ──
    analysis = _repair_analysis_structure(analysis, condition)
    _query_ir = build_query_ir(analysis, condition)
    raw_conditions = [
        c for c in (analysis.get("conditions", []) or [])
        if isinstance(c, dict) and str(c.get("text") or "").strip()
    ]
    filtered_conditions = [
        c for c in raw_conditions
        if not _is_non_executable_subcondition(c.get("text", ""))
    ]
    if filtered_conditions and len(filtered_conditions) != len(raw_conditions):
        dropped = [c.get("text", "") for c in raw_conditions if c not in filtered_conditions]
        log(f"[Step1-理解] 过滤非执行子条件: {dropped}")
        analysis["conditions"] = filtered_conditions
    analysis["conditions"] = filtered_conditions or raw_conditions
    _query_ir = build_query_ir(analysis, condition)
    from microharness.medical.condition_execution import (
        build_condition_execution_specs,
    )
    _execution_specs = build_condition_execution_specs(
        _query_ir,
        analysis,
        fallback_keyword_fn=_extract_core_keyword,
    )
    sub_queries = [spec.text for spec in _execution_specs]

    connector = analysis.get("connector")
    _use_and = connector != "or"  # default to AND

    catalog = get_catalog()

    log(f"\n{'='*60}")
    log(f"[Step1-理解] 原始问题: {condition}")
    log(f"[Step1-理解] 分析: type={analysis.get('type', 'simple')} connector={analysis.get('connector')} negated={_negate} source={analysis.get('source')}")
    from microharness.medical.condition_summary import summarize_condition_structure
    for spec in _execution_specs:
        sq = spec.text
        structure = summarize_condition_structure(
            sq,
            spec.condition_dict(),
            fallback_keyword_fn=(
                _extract_core_keyword if spec.legacy_fallback_allowed else None
            ),
        )
        log(
            f"[Step1-结构] 条件{spec.position}: 主体={structure['主体']} | "
            f"限定={structure['限定']} | 判断={structure['判断']}"
        )
    if len(_execution_specs) > 1:
        for spec in _execution_specs:
            log(f"[Step1-理解]   子问题{spec.position}: {spec.text}")
    log(f"{'='*60}")

    if register_no or global_patient_id:
        from concurrent.futures import Future, ThreadPoolExecutor, as_completed
        import threading
        from microharness.database.field_mapper import TABLE_MAP, DOC_FIELDS, COMMON_FIELDS
        from microharness.database.db_client import get_db as get_database

        # ── External services: use understand_query results directly (no LLM) ──
        from microharness.services.service_catalog import load_services, match_services
        from microharness.services.http_client import call_service_as_binding

        services = load_services()
        _base_url = services.get("base_url", "").rstrip("/")
        _sub_svc_map = {}  # execution_key → list of matched service dicts
        _svc_needed = {}   # unique service_id → svc_dict
        _svc_results = {}  # service_id → list of binding results
        _svc_by_label = {}
        _svc_futures = {}
        _svc_lock = threading.RLock()
        try:
            _svc_max_concurrency = max(1, int(os.environ.get("MEDICAL_SERVICE_MAX_CONCURRENCY", "3")))
        except (TypeError, ValueError):
            _svc_max_concurrency = 3

        def _register_service(svc: dict):
            fsid = svc.get("id") or svc.get("name")
            if not fsid:
                return "", None
            svc_url = svc.get("url", "")
            if _base_url and svc_url and not svc_url.startswith("http"):
                svc_url = f"{_base_url}/{svc_url.lstrip('/')}"
            svc_with_id = {**svc, "url": svc_url, "id": fsid}
            with _svc_lock:
                registered = _svc_needed.setdefault(fsid, svc_with_id)
                label = registered.get("label", registered.get("name", fsid))
                _svc_by_label[label] = {
                    "id": fsid,
                    "returns": registered.get("returns", ""),
                    "description": registered.get("description", ""),
                }
            return fsid, registered

        def _call_service_once(fsid: str, svc: dict, query_text: str,
                               source: str, log_prefix: str):
            """Execute one patient-wide service once and share its Future."""
            with _svc_lock:
                future = _svc_futures.get(fsid)
                owner = future is None
                if owner:
                    future = Future()
                    _svc_futures[fsid] = future
            if owner:
                try:
                    results = call_service_as_binding(
                        svc, {"condition": query_text}, register_no=register_no,
                        global_patient_id=global_patient_id,
                        visit_no=visit_no, global_visit_id=global_visit_id,
                    ) or []
                    with _svc_lock:
                        _svc_results[fsid] = results
                    log(f"{log_prefix} {fsid}: {_service_result_summary(results)} (from {source})")
                    future.set_result(results)
                except Exception as exc:
                    with _svc_lock:
                        _svc_results[fsid] = []
                    log(f"{log_prefix} {fsid}: 失败 - {exc} (from {source})")
                    future.set_result([])
            return future.result()

        def _ensure_services_for_query(
            execution_key: str,
            query_text: str,
            svc_list: list,
            source: str = "metadata",
        ):
            """Register matched services and reuse their request-scoped result."""
            added = []
            for svc in svc_list or []:
                fsid, registered = _register_service(svc)
                if not fsid:
                    continue
                added.append(registered)
            if added:
                with _svc_lock:
                    bucket = _sub_svc_map.setdefault(execution_key, [])
                    seen = {s.get("id") for s in bucket}
                    for svc in added:
                        if svc.get("id") not in seen:
                            bucket.append(svc)
                for svc in added:
                    _call_service_once(
                        svc["id"], svc, query_text, source=source,
                        log_prefix="  [Step2-服务]",
                    )
            return added

        # Use analysis results for service matching (no additional LLM call)
        for execution_spec in _execution_specs:
            skill_ids = execution_spec.target_services
            matched = []
            for sid in skill_ids:
                svc = services.get(sid)
                if svc and isinstance(svc, dict):
                    # Resolve URL: prepend base_url if url is a relative path
                    svc_url = svc.get("url", "")
                    if _base_url and not svc_url.startswith("http"):
                        svc_url = f"{_base_url}/{svc_url.lstrip('/')}"
                    svc_with_id = {**svc, "url": svc_url, "id": sid}
                    matched.append(svc_with_id)
                    _register_service(svc_with_id)
            _sub_svc_map[execution_spec.execution_key] = matched

        # Build label→service_meta map for injecting skill guidance into judge prompts
        for _sid, _svc in _svc_needed.items():
            _label = _svc.get("label", _svc.get("name", _sid))
            _svc_by_label[_label] = {"id": _sid, "returns": _svc.get("returns", ""),
                                       "description": _svc.get("description", "")}

        # ── Call independent structured services once with bounded concurrency ──
        _stage_started = time.perf_counter()
        if _svc_needed:
            service_workers = min(_svc_max_concurrency, len(_svc_needed))
            log(
                f"[结构化服务并行] 服务数={len(_svc_needed)} | "
                f"最大并发={service_workers}"
            )
            with ThreadPoolExecutor(max_workers=service_workers) as service_executor:
                service_futures = [
                    service_executor.submit(
                        _call_service_once,
                        sid,
                        svc,
                        condition,
                        "analysis",
                        "[服务调用]",
                    )
                    for sid, svc in list(_svc_needed.items())
                ]
                for service_future in as_completed(service_futures):
                    service_future.result()
        _record_stage("structured_services_ms", _stage_started)

        from microharness.database.field_mapper import TABLE_MAP as _TM, DOC_FIELDS as _DF, COMMON_FIELDS as _CF, find_db_column
        from microharness.database.db_client import get_db as get_database

        _db_health_lock = threading.Lock()
        _db_health_state = {"checked": False, "ok": False, "error": ""}

        def _request_db_health_check(db):
            """Run the database connectivity test at most once per request."""
            with _db_health_lock:
                if not _db_health_state["checked"]:
                    health_started = time.perf_counter()
                    try:
                        _db_health_state["ok"] = bool(db.test())
                        if not _db_health_state["ok"]:
                            _db_health_state["error"] = "数据库连通性检测未通过"
                    except Exception as exc:
                        _db_health_state["ok"] = False
                        _db_health_state["error"] = str(exc)
                    _db_health_state["checked"] = True
                    health_ms = int((time.perf_counter() - health_started) * 1000)
                    log(
                        f"  [DB] 请求级连通性检测: "
                        f"{'通过' if _db_health_state['ok'] else '失败'} | {health_ms}ms"
                    )
                return _db_health_state["ok"], _db_health_state["error"]

        def query_db_for_route(sq_route):
            """Query DB for tables+columns specified by this route."""
            return _query_db(
                sq_route,
                register_no,
                visit_no,
                global_patient_id,
                global_visit_id,
                log_fn=log,
                db_health_check=_request_db_health_check,
            )

        # ── For EACH sub-condition, check ALL files in parallel ──
        def check_one_condition(execution_spec):
            """Check all files for one sub-condition using pre-computed analysis."""
            t0 = time.time()
            sq = execution_spec.text
            execution_key = execution_spec.execution_key

            # Consume normalized IR fields directly. Only legacy analysis is
            # allowed to use text-trigger compatibility fallback below.
            cond_a = execution_spec.condition_dict()
            sq_docs = list(execution_spec.target_docs)
            sq_sections = list(execution_spec.target_sections)
            sq_targets = execution_spec.targets_dict()
            ir_docs = list(sq_docs or [])
            ir_sections = list(sq_sections or [])
            ir_targets = dict(sq_targets or {})
            ir_services = list(execution_spec.target_services)
            route_source = execution_spec.execution_source
            sq_keyword = execution_spec.keyword
            sq_modifiers = list(execution_spec.modifiers)
            if execution_spec.outcome_state and not sq_modifiers:
                sq_modifiers = [execution_spec.outcome_state]
            sq_numeric_required = execution_spec.numeric_execution_required
            sq_semantic_class = execution_spec.semantic_class
            sq_primary_entity = execution_spec.canonical_entity
            sq_entity_candidates = list(execution_spec.entity_candidates)
            sq_execution_entity = sq_primary_entity or sq_keyword or sq

            route_availability = _resolve_executable_route_sources(
                sq_docs,
                _sub_svc_map.get(execution_key, []),
                query_document_catalog,
                services,
                _TM,
            )
            sq_docs = route_availability["documents"]
            executable_service_ids = set(route_availability["services"])
            _sub_svc_map[execution_key] = [
                svc for svc in _sub_svc_map.get(execution_key, [])
                if isinstance(svc, dict)
                and (svc.get("id") or svc.get("name")) in executable_service_ids
            ]
            if route_availability["unresolved_documents"]:
                log(
                    "  [Step2-路由]   → 未解析文档（保留诊断，不用于查库）: "
                    f"{route_availability['unresolved_documents']}"
                )
            if route_availability["unresolved_services"]:
                log(
                    "  [Step2-路由]   → 不可执行服务（保留诊断）: "
                    f"{route_availability['unresolved_services']}"
                )

            # Fallback is only needed when the IR has no executable evidence source.
            if route_availability["should_fallback"]:
                route_source = "fallback"
                log("  [Step2-路由]   → 无有效文档或结构化服务，执行fallback路由")
                router = QueryRouter(
                    model=router_model,
                    document_catalog=query_document_catalog,
                )
                fallback_route = router.route(sq)
                sq_docs = fallback_route.get("target_medical_doc", [])
                sq_sections = fallback_route.get("target_sections", [])
                sq_targets = fallback_route.get("targets", {}) if isinstance(fallback_route.get("targets", {}), dict) else {}
                log(f"  [Step2-路由] 子问题: {sq} (fallback路由)")
                # Also check for services from concept_match route
                fallback_skills = fallback_route.get("target_services", [])
                if fallback_skills:
                    _ensure_services_for_query(
                        execution_key,
                        sq,
                        [
                            {**services[fsid], "id": fsid}
                            for fsid in fallback_skills
                            if fsid in services and isinstance(services[fsid], dict)
                        ],
                        source="concept_match",
                    )
                metadata_services = match_services(sq, services=services, model=None)
                if metadata_services:
                    _ensure_services_for_query(
                        execution_key,
                        sq,
                        metadata_services,
                        source="metadata",
                    )
            else:
                log(f"  [Step2-理解] 子问题: {sq}")
                log(f"  [Step2-理解]   → 文档: {sq_docs}")
                log(f"  [Step2-理解]   → 章节: {sq_sections[:6]}")
                log(f"  [Step2-理解]   → 关键词: {sq_keyword} 修饰词: {sq_modifiers}")
                if sq_docs and executable_service_ids:
                    log("  [Step2-路由]   → 文档与结构化服务联合取证，跳过fallback")
                elif sq_docs:
                    log("  [Step2-路由]   → 纯文档路由，跳过fallback")
                else:
                    log("  [Step2-路由]   → 纯结构化服务路由，跳过fallback")

            # A fallback router can still return catalog entries that are not
            # mapped to a local table. Keep those diagnostics out of DB execution.
            fallback_availability = _resolve_executable_route_sources(
                sq_docs,
                _sub_svc_map.get(execution_key, []),
                query_document_catalog,
                services,
                _TM,
            )
            sq_docs = fallback_availability["documents"]
            if fallback_availability["unresolved_documents"]:
                log(
                    "  [Step2-路由]   → fallback未解析文档（未用于查库）: "
                    f"{fallback_availability['unresolved_documents']}"
                )

            route_services = [
                svc.get("id") or svc.get("name", "")
                for svc in _sub_svc_map.get(execution_key, [])
                if isinstance(svc, dict)
            ]
            route_services = [s for s in dict.fromkeys(route_services) if s]
            primary_service = _primary_service_for_condition(
                sq,
                cond_a,
                allow_text_fallback=execution_spec.legacy_fallback_allowed,
            )
            if primary_service and primary_service in route_services:
                _sub_svc_map[execution_key] = [
                    svc for svc in _sub_svc_map.get(execution_key, [])
                    if (svc.get("id") or svc.get("name")) == primary_service
                ]
                route_services = [primary_service]
            elif primary_service and primary_service in services:
                svc = services.get(primary_service)
                if isinstance(svc, dict):
                    added_services = _ensure_services_for_query(
                        execution_key,
                        sq,
                        [{**svc, "id": primary_service}],
                        source="semantic_primary",
                    )
                    if added_services:
                        route_services = [primary_service]
            if primary_service:
                _sub_svc_map[execution_key] = [
                    svc for svc in _sub_svc_map.get(execution_key, [])
                    if (svc.get("id") or svc.get("name")) == primary_service
                ]
                if _sub_svc_map.get(execution_key):
                    route_services = [primary_service]

            anchor_docs, anchor_sections = _event_anchor_route(
                sq,
                temporal=execution_spec.temporal,
                allow_text_fallback=execution_spec.legacy_fallback_allowed,
            )
            if anchor_docs:
                sq_docs = list(dict.fromkeys(list(sq_docs or []) + anchor_docs))
            if anchor_sections:
                sq_sections = list(dict.fromkeys(list(sq_sections or []) + anchor_sections))
                for doc in anchor_docs or []:
                    sq_targets[doc] = list(dict.fromkeys(list(sq_targets.get(doc, [])) + anchor_sections))

            pre_prune_docs = list(sq_docs or [])
            pre_prune_sections = list(sq_sections or [])
            sq_docs, sq_sections, route_note = _prune_primary_service_route(
                sq,
                sq_docs,
                sq_sections,
                route_services,
                temporal=execution_spec.temporal,
                allow_text_fallback=execution_spec.legacy_fallback_allowed,
            )
            if sq_docs != pre_prune_docs or sq_sections != pre_prune_sections:
                sq_targets = {}
            if route_note:
                log(f"  [Step2-路由]   → {route_note}")

            # Build sq_route in the format _query_db expects
            targets = {
                doc: [sec for sec in (sq_targets.get(doc, []) or []) if sec]
                for doc in sq_docs
                if sq_targets.get(doc)
            }
            for doc in sq_docs:
                if doc not in targets:
                    targets[doc] = list(sq_sections)
            sq_route = {
                "user_query": sq,
                "targets": targets,
                "target_medical_doc": sq_docs,
                "target_sections": sq_sections,
                "target_xml_paths": [],
                "confidence": 0.9,
                "source": "understand_query",
                "_decomposed_keyword": sq_keyword if sq_keyword != sq else None,
                "_decomposed_modifiers": sq_modifiers if sq_modifiers else None,
                "_semantic_class": sq_semantic_class,
                "_entity_type": cond_a.get("entity_type", ""),
                "_canonical_entity": sq_primary_entity,
                "_entity_candidates": sq_entity_candidates,
                "_entity_aliases": list(cond_a.get("aliases") or []),
                "_entity_confidence": cond_a.get("entity_confidence"),
                "_normalization_source": cond_a.get("normalization_source", ""),
            }
            sq_xml = sq_route.get("target_xml_paths", [])
            cond_no = execution_spec.position
            log(
                "[完整路由][执行前] "
                + json.dumps(
                    {
                        "条件序号": cond_no,
                        "条件": sq,
                        "路由来源": route_source,
                        "执行输入来源": execution_spec.execution_source,
                        "IR": {
                            "keyword": cond_a.get("keyword", ""),
                            "entity": cond_a.get("entity", ""),
                            "canonical_entity": sq_primary_entity,
                            "aliases": list(cond_a.get("aliases") or []),
                            "entity_candidates": sq_entity_candidates,
                            "entity_confidence": cond_a.get("entity_confidence"),
                            "normalization_source": cond_a.get("normalization_source", ""),
                            "entity_type": cond_a.get("entity_type", ""),
                            "predicate": cond_a.get("predicate", ""),
                            "semantic_class": cond_a.get("semantic_class", ""),
                            "outcome_state": execution_spec.outcome_state,
                            "outcome_phase": execution_spec.outcome_phase,
                            "history_context": execution_spec.history_context,
                            "internal_negation": execution_spec.internal_negation,
                            "target_docs": ir_docs,
                            "target_sections": ir_sections,
                            "targets": ir_targets,
                            "target_skills": ir_services,
                            "evidence_plan_source_ids": cond_a.get("evidence_plan_source_ids", []),
                        },
                        "主证据服务": primary_service or "",
                        "事件锚点": {
                            "文档": anchor_docs,
                            "章节": anchor_sections,
                        },
                        "最终路由": {
                            "文档": sq_docs,
                            "章节": sq_sections,
                            "文档章节映射": targets,
                            "服务": route_services,
                        },
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
            log(
                f"  [Step2-执行] 条件{cond_no}: 文档={sq_docs or ['无']} | "
                f"章节={sq_sections[:4] or ['无']} | 服务={route_services or ['无']}"
            )

            sq_files = []

            # Query DB for THIS sub-condition's route
            relevant_files = query_db_for_route(sq_route)

            from microharness.medical.clinical_phase import (
                infer_document_source_phase,
            )
            for source in relevant_files:
                if not isinstance(source, dict) or source.get('service_id'):
                    continue
                source_name = str(source.get('file') or '')
                template_name = str(source.get('template') or '')
                document_name = next(
                    (
                        name
                        for name in query_document_catalog
                        if source_name.startswith(str(name)) or template_name == str(name)
                    ),
                    '',
                )
                document_metadata = query_document_catalog.get(document_name)
                if not isinstance(document_metadata, dict):
                    continue
                binding_sections = [
                    str(binding.get('html_field') or '').strip()
                    for binding in source.get('bindings', [])
                    if isinstance(binding, dict)
                    and str(binding.get('html_field') or '').strip()
                ]
                phase_profile = infer_document_source_phase(
                    document_metadata,
                    binding_sections,
                )
                semantic = dict(source.get('semantic') or {})
                semantic.update(phase_profile.to_dict())
                semantic['document_name'] = document_name
                source['semantic'] = semantic

            # Add pre-fetched external service results for this sub-condition
            for svc in _sub_svc_map.get(execution_key, []):
                sid = svc.get("id", svc.get("name", ""))
                results = _svc_results.get(sid)
                if results:
                    relevant_files.extend(results)
                    log(f"  [Step2-外部]   → {sid}: {_service_result_summary(results)}")
            if execution_spec.is_outcome_condition:
                from microharness.medical.clinical_phase import (
                    classify_outcome_source_role,
                    resolve_outcome_target_phase,
                    source_supports_outcome_state,
                )

                phase_profiles = [
                    source.get('semantic') or {}
                    for source in relevant_files
                    if isinstance(source, dict)
                    and not source.get('service_id')
                ]
                target_outcome_phase = resolve_outcome_target_phase(
                    execution_spec.outcome_phase,
                    phase_profiles,
                )
                for source in relevant_files:
                    if not isinstance(source, dict):
                        continue
                    semantic = dict(source.get('semantic') or {})
                    explicit_role = (
                        source.get('source_role')
                        or source.get('evidence_role')
                        or semantic.get('source_role')
                        or semantic.get('evidence_role')
                    )
                    source_kind = 'service' if source.get('service_id') else 'document'
                    role = classify_outcome_source_role(
                        semantic,
                        target_phase=target_outcome_phase,
                        source_kind=source_kind,
                        supports_outcome_state=source_supports_outcome_state(source),
                        explicit_role=explicit_role,
                    )
                    semantic['source_role'] = role
                    semantic['target_outcome_phase'] = target_outcome_phase
                    semantic['outcome_phase_policy'] = (
                        execution_spec.attributes.get('outcome_phase_policy')
                        or ('explicit' if execution_spec.outcome_phase else '')
                    )
                    source['semantic'] = semantic
                    source['source_role'] = role
                phase_label = target_outcome_phase or '未解析'
                policy_label = (
                    execution_spec.attributes.get('outcome_phase_policy')
                    or 'explicit'
                )
                log(
                    f'  [Step2-转归阶段] 目标阶段={phase_label} | '
                    f'策略={policy_label}'
                )

            from microharness.medical.time_window import resolve_time_window
            time_window = resolve_time_window(
                sq,
                _svc_results,
                relevant_files,
                temporal=execution_spec.temporal,
                allow_text_fallback=execution_spec.legacy_fallback_allowed,
            )
            if time_window and route_services and time_window.resolved:
                log(f"  [Step2-时间窗] {time_window.scope}: {time_window.describe()} ({time_window.source})")
            elif time_window and route_services and time_window.required:
                log(f"  [Step2-时间窗] {time_window.scope}: 未解析 ({time_window.reason})")

            matched_file_names = [ab.get("file", "") for ab in relevant_files if isinstance(ab, dict)]
            log(f"  [Step2-路由]   → 匹配文件: {matched_file_names}")
            log(
                "[完整路由][执行后] "
                + json.dumps(
                    {
                        "条件序号": cond_no,
                        "条件": sq,
                        "实际证据文件": matched_file_names,
                        "服务结果": {
                            sid: _service_result_summary(_svc_results.get(sid) or [])
                            for sid in route_services
                        },
                        "时间窗": {
                            "required": bool(time_window and time_window.required),
                            "resolved": bool(time_window and time_window.resolved),
                            "scope": getattr(time_window, "scope", "") if time_window else "",
                            "description": time_window.describe() if time_window and time_window.resolved else "",
                            "source": getattr(time_window, "source", "") if time_window else "",
                            "reason": getattr(time_window, "reason", "") if time_window else "",
                        },
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )

            # Check each relevant file in parallel
            def check_one_file(ab):
                if ab.get("service_error"):
                    fields = "\n".join(
                        f"  {b.get('html_field', '')}: {b.get('value', '')}"
                        for b in ab.get("bindings", [])
                    )
                    message = _compact_log_text(ab.get("error") or "未取得数据，当前无法判断", 180)
                    return {
                        "file": ab["file"],
                        "matched": False,
                        "status": "UNKNOWN",
                        "reason_code": "SOURCE_UNAVAILABLE",
                        "data_quality": "SOURCE_ERROR",
                        "reason": message,
                        "fields": fields,
                        "cot_response": "",
                    }

                from microharness.medical.domain_execution import (
                    ConditionSemanticType,
                    DomainExecutionRequest,
                    execute_document_domain,
                    execute_numeric_domain,
                    execute_recalled_document_domain,
                    execute_structured_domain,
                )

                domain_request = DomainExecutionRequest.from_execution_spec(
                    execution_spec,
                    ab.get("bindings", []),
                    time_window=time_window,
                    semantic=ab.get("semantic", {}),
                    temporal_semantics=ab.get("temporal_semantics", {}),
                )
                is_external = ab.get("template", "") not in ("AdmissionRecord","DischargeRecord",
                    "OutpatientAndEmergency","FirstMedicalRecord","DailyMedicalRecord","SurgeryRecord")

                semantic_source_truncated = False
                if is_external:
                    # ── Compact format: one line per record, | separated ──
                    _recs = {}  # prefix → list of "field: value"
                    _name_vals = {}  # key → [] for LLM grouping
                    for b in ab["bindings"]:
                        label = b.get("html_field", "")
                        val = str(b.get("value", ""))
                        val = ''.join(ch for ch in val if ch.isprintable() or ch in '\n\r\t')
                        if not val.strip():
                            continue
                        if label.startswith("[") and "] " in label:
                            br = label.index("] ")
                            prefix, field = label[:br+1], label[br+2:]  # "[诊断1]", "诊断名称"
                        else:
                            prefix, field = "", label
                        _recs.setdefault(prefix, []).append(f"{field}: {val}")
                        # Collect name-type fields for LLM grouping
                        if field.endswith("名称") or field == "过敏史":
                            _name_vals.setdefault(field, []).append(val)
                    sub_fields = [f"  {p} " + " | ".join(fs) if p else "  " + " | ".join(fs)
                                  for p, fs in _recs.items()]
                    sub_summary = "\n".join(sub_fields) if sub_fields else "(无匹配字段)"

                    # Slim for LLM: group name-type values with ；
                    # Also keep diagnosis type (diagTypeDesc) paired with name for context
                    if _name_vals:
                        # Build slim lines: "诊断名称: 背痛; 骨折术后" (grouped by field name)
                        _judge_fields = []
                        for k, vs in _name_vals.items():
                            unique = list(dict.fromkeys(vs))  # preserve order, remove dups
                            line = f"{k}: {'; '.join(unique)}"
                            if len(line) > 800:
                                semantic_source_truncated = True
                                line = line[:797] + "..."
                            _judge_fields.append(line)
                        # Also include diagnosis type info if present (pairs name with type)
                        # Format: "诊断名称(入院诊断): 背痛" so LLM knows which diagnosis type
                        _type_name_map = {}  # type → [names]
                        for b in ab["bindings"]:
                            label = b.get("html_field", "")
                            val = str(b.get("value", ""))
                            if not val.strip():
                                continue
                            if label.startswith("[") and "] " in label:
                                br = label.index("] ")
                                prefix, field = label[:br+1], label[br+2:]
                            else:
                                prefix, field = "", label
                            # Pair diagnosis name with its type within same record prefix
                            if field.endswith("名称"):
                                # Find the type for this same record (same prefix)
                                _type_val = ""
                                for b2 in ab["bindings"]:
                                    lbl2 = b2.get("html_field", "")
                                    if lbl2.startswith(prefix) and "诊断类型" in lbl2:
                                        _type_val = str(b2.get("value", ""))
                                        break
                                if _type_val:
                                    _type_name_map.setdefault(_type_val, [])
                                    if val not in _type_name_map[_type_val]:
                                        _type_name_map[_type_val].append(val)
                        # If we found type-name pairings, replace plain name list with typed list
                        if _type_name_map:
                            _judge_fields = []
                            for dtype, names in _type_name_map.items():
                                line = f"{dtype}诊断名称: {'; '.join(names)}"
                                if len(line) > 800:
                                    semantic_source_truncated = True
                                    line = line[:797] + "..."
                                _judge_fields.append(line)
                    else:
                        _judge_fields = sub_fields[:10]
                        semantic_source_truncated = len(sub_fields) > 10
                    judge_summary = "\n".join(_judge_fields)
                    debug_log(f"    [LLM入参] {ab['file']}: {len(_judge_fields)}行/{len(sub_fields)}记录行 →\n{judge_summary[:800]}")
                    domain_judge = execute_structured_domain(
                        domain_request,
                        on_error=lambda executor, exc: debug_log(
                            f"    [Step4-领域执行器异常] {executor}: {exc}"
                        ),
                    )
                    if domain_judge is not None:
                        domain_labels = {
                            "laboratory": "检验规则",
                            "medication": "用药规则",
                            "diagnosis": "诊断规则",
                        }
                        result = domain_judge.to_file_result(
                            ab["file"],
                            fallback_fields=sub_summary,
                        )
                        domain_status = result.get("status") or (
                            "MATCHED" if result["matched"] else "NOT_MATCHED"
                        )
                        missing_capabilities = result.get("missing_capabilities") or []
                        capability_suffix = (
                            f" | 缺失能力={missing_capabilities}"
                            if missing_capabilities else ""
                        )
                        log(
                            f"    [Step4-{domain_labels.get(domain_judge.domain, '领域规则')}] "
                            f"{ab['file']}: {domain_status} — {result['reason'][:80]}"
                            f"{capability_suffix}"
                        )
                        return result
                    if time_window and time_window.required and not time_window.resolved:
                        try:
                            from microharness.medical.structured_time import filter_bindings_by_time_window
                            semantic_time_judge = filter_bindings_by_time_window(
                                ab.get("bindings", []),
                                time_window,
                                condition=sq,
                                temporal_semantics=ab.get("temporal_semantics", {}),
                            )
                        except Exception:
                            semantic_time_judge = {"applicable": False}
                        if semantic_time_judge.get("applicable") and semantic_time_judge.get("matched"):
                            filtered_lines = semantic_time_judge.get("filtered_lines") or []
                            if filtered_lines:
                                sub_fields = filtered_lines
                                sub_summary = "\n".join(sub_fields)
                                judge_summary = sub_summary
                            log(
                                f"    [Step4-时间语义] {ab['file']}: "
                                f"{semantic_time_judge.get('reason', '结构化时间语义匹配')[:80]}"
                            )
                        else:
                            return {
                                "file": ab["file"],
                                "matched": False,
                                "reason": (
                                    f"找到结构化数据，但缺少{time_window.source or time_window.scope}时间锚点，"
                                    f"无法判断是否发生在{time_window.scope}"
                                ),
                                "fields": sub_summary[:4000],
                                "cot_response": "",
                            }
                else:
                    # Internal DB/upload results: per-field lines
                    sub_fields = []
                    for b in ab["bindings"]:
                        path = b.get("xml_path", ""); xml_tag = path.split("/")[-1] if path else ""
                        label = b.get("html_field") or xml_tag; val = b.get("html_value") or b.get("value", "")
                        include = any(ts in label or ts in path or label in ts or xml_tag in ts for ts in sq_sections)
                        if not include:
                            include = any(tp in path or xml_tag in tp or tp in xml_tag for tp in sq_xml)
                        if not sq_sections and not sq_xml: include = True
                        if include:
                            semantic_source_truncated = semantic_source_truncated or len(str(val)) > 500
                            sub_fields.append(f"  {label}: {str(val)[:500]}")
                    sub_summary = "\n".join(sub_fields) if sub_fields else "(无匹配字段)"
                    judge_summary = sub_summary
                    log(f"    [Step3-取值] {ab['file']}: 命中{len(sub_fields)}字段/{len(sub_summary)}字符")
                    debug_log(f"    [Step3-取值][debug] {ab['file']} →\n{sub_summary[:1000]}")

                # ═══════════════════════════════════════════════════════════
                # 预筛：字符重叠度扫描 → 减少 LLM 数据量
                #
                # 不用 str.contains（要求连续子串），改用字符覆盖度：
                #   "背痛" vs "背部疼痛" → 背✓ 痛✓ → 2/2 → 匹配
                #   "维生素B1" vs "维生素B1片" → 全部命中 → 匹配
                #   "糖尿病" vs "血糖偏高" → 糖✗ 尿✗ 病✗ → 0/3 → 不匹配
                #
                # 这样短词变体不会漏，长词不匹配可安全跳过 LLM。
                # ═══════════════════════════════════════════════════════════
                # 优先使用 LLM 语义拆解的关键字，更准确（如"背痛治好"→"背痛"）
                outcome_modifiers = (
                    list(execution_spec.modifiers)
                    if execution_spec.is_outcome_condition
                    else []
                )
                kw_pre = sq_execution_entity
                semantic_recall_required = False
                lexical_entity = kw_pre
                prefilter_candidates = []
                for candidate in [kw_pre, *(sq_route.get("_entity_candidates") or [])]:
                    candidate = str(candidate or "").strip()
                    if len(candidate) >= 2 and candidate not in prefilter_candidates:
                        prefilter_candidates.append(candidate)
                active_external_prefixes = None
                # 数值/日期条件不走预筛：关键字如"住院天数"不会字面出现在字段值中，
                # 这类条件由 IR 数值执行器和预计算 hints 确定性处理。
                if prefilter_candidates and not sq_numeric_required:
                    filtered_fields = []
                    filtered_prefixes = set()
                    matched_candidates = []
                    for line in sub_fields:
                        match_text = line
                        if is_external:
                            # External compact rows contain Chinese field labels such as
                            # "检测项目数量"; labels must not contribute characters to
                            # concept matching, otherwise "白细胞计数" can falsely match
                            # unrelated rows through label text.
                            match_text = re.sub(r'(^|\|)\s*[^:：|]{1,20}[:：]\s*', ' ', line)
                        line_candidates = [
                            candidate for candidate in prefilter_candidates
                            if _char_overlap_match(candidate, match_text)
                        ]
                        if line_candidates:
                            for candidate in line_candidates:
                                if candidate not in matched_candidates:
                                    matched_candidates.append(candidate)
                            if is_external:
                                _pm = re.match(r"\s*(\[[^\]]+\])", line)
                                if _pm:
                                    filtered_prefixes.add(_pm.group(1))
                            # 外部数据 slim 格式的特殊处理：字段值用 ; 分隔时，
                            # 只保留含关键字的个别值，进一步压缩
                            if is_external and ': ' in line:
                                fname, fvals = line.split(': ', 1)
                                if '; ' in fvals:
                                    matching_vals = [v.strip() for v in fvals.split('; ')
                                                     if any(_char_overlap_match(candidate, v)
                                                            for candidate in line_candidates)]
                                    if matching_vals:
                                        filtered_fields.append(f"{fname}: {'; '.join(matching_vals)}")
                                    continue
                            filtered_fields.append(line)
                    if filtered_fields:
                        lexical_entity = matched_candidates[0] if matched_candidates else kw_pre
                        if len(filtered_fields) < len(sub_fields):
                            log(
                                f"    [预筛] {ab['file']}: {len(sub_fields)}→{len(filtered_fields)}行 "
                                f"(候选实体'{lexical_entity}')"
                            )
                        sub_fields = filtered_fields
                        sub_summary = "\n".join(sub_fields)
                        judge_summary = sub_summary
                        active_external_prefixes = filtered_prefixes or None
                        # Rebuild hints from filtered data
                        hints = _precompute_hints(judge_summary)
                    else:
                        # 外部API数据（诊断、用药等）是结构化名称字段。
                        # 如果核心词在这些字段中没有任何字符证据，直接判不匹配；
                        # 不让小模型把无关药名/诊断名解释成目标概念。
                        if is_external:
                            log(f"    [预筛] {ab['file']}: 外部数据关键字'{kw_pre}'字符未覆盖 → 跳过LLM")
                            return {
                                "file": ab["file"],
                                "matched": False,
                                "status": "NOT_MENTIONED",
                                "reason_code": "NO_MATCHING_RECORD",
                                "data_quality": "COMPLETE",
                                "reason": f"关键字'{kw_pre}'未在结构化字段中出现",
                                "fields": sub_summary[:2000],
                                "cot_response": "",
                            }
                        else:
                            # 本地病历可能使用与查询无字符重叠的严格同义表达。
                            # 仅让 LLM 召回原文实体，最终仍由程序校验证据并判断语义。
                            semantic_recall_required = True
                            log(
                                f"    [预筛] {ab['file']}: 候选实体{prefilter_candidates}字符未覆盖 "
                                "→ 进入受约束语义召回"
                            )

                if not sub_fields:
                    log(f"    [Step3-取值] {ab['file']}: 无匹配字段 → 跳过")
                    return {"file": ab["file"], "matched": False, "reason": "无相关字段", "fields": "", "cot_response": ""}

                source_semantic = ab.get("semantic") if isinstance(ab.get("semantic"), dict) else {}
                temporal_filter_mode = str(
                    source_semantic.get("temporal_filter_mode") or "generic"
                ).strip().lower()
                if (
                    is_external
                    and time_window
                    and time_window.required
                    and time_window.resolved
                    and temporal_filter_mode == "generic"
                ):
                    time_bindings = ab.get("bindings", [])
                    if active_external_prefixes:
                        time_bindings = [
                            b for b in time_bindings
                            if any(str(b.get("html_field", "")).startswith(prefix) for prefix in active_external_prefixes)
                        ]
                    try:
                        from microharness.medical.structured_time import filter_bindings_by_time_window
                        temporal_judge = filter_bindings_by_time_window(
                            time_bindings,
                            time_window,
                            condition=sq,
                            temporal_semantics=ab.get("temporal_semantics", {}),
                        )
                    except Exception as _temporal_e:
                        temporal_judge = {"applicable": False, "error": str(_temporal_e)}
                    if temporal_judge.get("applicable"):
                        if not temporal_judge.get("matched"):
                            result = {
                                "file": ab["file"],
                                "matched": False,
                                "reason": temporal_judge.get("reason", f"记录时间不在{time_window.scope}"),
                                "fields": temporal_judge.get("fields", sub_summary)[:4000],
                                "cot_response": "",
                            }
                            if temporal_judge.get("candidate_records"):
                                result["候选记录"] = temporal_judge.get("candidate_records")
                                result["候选记录数"] = len(temporal_judge.get("candidate_records") or [])
                            log(
                                f"    [Step4-时间规则] {ab['file']}: ✗ 不符合 — "
                                f"{result['reason'][:80]}"
                            )
                            return result
                        filtered_lines = temporal_judge.get("filtered_lines") or []
                        if filtered_lines:
                            sub_fields = filtered_lines
                            sub_summary = "\n".join(sub_fields)
                            judge_summary = sub_summary
                            log(
                                f"    [Step4-时间规则] {ab['file']}: "
                                f"{len(sub_fields)}条候选记录在{time_window.scope}内"
                            )

                # Pre-compute date differences & numeric values (on judge_summary for speed)
                hints = _precompute_hints(judge_summary)

                numeric_domain_judge = execute_numeric_domain(
                    domain_request,
                    hints,
                    fields=sub_summary,
                )
                if numeric_domain_judge is not None:
                    result = numeric_domain_judge.to_file_result(
                        ab["file"],
                        fallback_fields=sub_summary,
                    )
                    log(
                        f"    [Step4-数值规则] {ab['file']}: {result['status']} — "
                        f"{result['reason'][:80]}"
                    )
                    return result

                document_domain_judge = None
                semantic_kw = lexical_entity or sq_execution_entity
                semantic_modifiers = sq_route.get("_decomposed_modifiers") or []
                has_internal_negation_modifier = execution_spec.internal_negation
                if (
                    not is_external
                    and semantic_kw
                    and not sq_numeric_required
                    and (
                        not has_internal_negation_modifier
                        or execution_spec.is_outcome_condition
                    )
                ):
                    from microharness.medical.structured_time import (
                        first_labeled_record_time_from_bindings,
                    )
                    document_request = replace(domain_request, entity=semantic_kw)
                    document_domain_judge = execute_document_domain(
                        document_request,
                        judge_summary,
                        record_time=first_labeled_record_time_from_bindings(
                            ab.get("bindings", [])
                        ),
                    )
                    if (
                        document_domain_judge is not None
                        and (
                            document_domain_judge.status.value != "MATCHED"
                            or document_domain_judge.semantic_type in {
                                ConditionSemanticType.HISTORY_DURATION,
                                ConditionSemanticType.OUTCOME_STATE,
                            }
                        )
                    ):
                        if (
                            semantic_recall_required
                            and document_domain_judge.status.value == "NOT_MENTIONED"
                        ):
                            log(
                                f"    [Step4-文档语义] {ab['file']}: 字面实体未提及 "
                                "→ 继续受约束语义召回"
                            )
                        else:
                            result = document_domain_judge.to_file_result(
                                ab["file"],
                                fallback_fields=sub_summary,
                            )
                            semantic_label = (
                                "无法判断" if result["status"] == "UNKNOWN"
                                else "未提及" if result["status"] == "NOT_MENTIONED"
                                else "不符合"
                            )
                            log(
                                f"    [Step4-文档语义] {ab['file']}: {semantic_label} — "
                                f"{document_domain_judge.reason[:80]}"
                            )
                            return result

                try:
                    from microharness.ollama import OllamaClient as JOC2
                    from microharness.ollama.model_profile import get_profile
                    from microharness.ollama.prompt_adapter import (
                        build_judge_prompt,
                        build_semantic_candidate_retry_prompt,
                        build_semantic_equivalence_prompt,
                        build_semantic_symptom_relation_prompt,
                    )
                    judge_profile = get_profile(judge_model)
                    judge_options = {"num_predict": judge_profile.num_predict}
                    if semantic_recall_required:
                        judge_options["seed"] = 0
                    j = JOC2(model=judge_model, timeout=120,
                             format_json=(
                                 semantic_recall_required
                                 or judge_profile.json_mode == "format_json"
                             ),
                             **judge_options)
                    # ── 注入预提取的关键词，避免LLM自己提词时被字段值带偏 ──
                    pre_kw = sq_execution_entity
                    kw_hint = ""
                    if pre_kw and pre_kw != sq:
                        kw_hint = f"\n核心关键词（已提取好，直接使用，不要自己重新提取）：{pre_kw}"

                    # ── 外部API数据：注入服务元数据（诊断类型说明等），指导LLM正确判断 ──
                    if is_external:
                        _svc_meta = _svc_by_label.get(ab.get("template", ""), {})
                        _svc_returns = _svc_meta.get("returns", "")
                        if _svc_returns:
                            kw_hint += f"\n\n数据类型说明（根据此说明理解字段含义，做语义匹配而非字面匹配）：\n{_svc_returns}"

                    prompt = build_judge_prompt(judge_profile, sq, kw_hint,
                                                judge_summary, hints,
                                                modifiers=sq_route.get("_decomposed_modifiers"),
                                                semantic_recall=semantic_recall_required,
                                                query_entity=(sq_route.get("_canonical_entity") or kw_pre or ""),
                                                entity_candidates=sq_route.get("_entity_candidates") or None,
                                                entity_type=sq_route.get("_entity_type", ""))
                    resp = j.chat(
                        [{"role": "user", "content": prompt}],
                        temperature=0.0 if semantic_recall_required else 0.1,
                    )
                    log(f"    [CoT响应] {ab['file']} ({len(resp)}字):\n{resp[:1000]}")
                    from microharness.medical.query_router import parse_llm_json
                    jd = parse_llm_json(resp, context=f"条件:{sq[:30]} 文件:{ab['file']}")
                    # ── 修复嵌套JSON: LLM偶尔把 matched/reason 包在 reasoning 字段里 ──
                    if isinstance(jd.get("reasoning"), dict):
                        jd = {**jd["reasoning"], **{k:v for k,v in jd.items() if k != "reasoning"}}
                    if semantic_recall_required:
                        from microharness.medical.semantic_entity_recall import (
                            aggregate_semantic_entity_decisions,
                            assess_semantic_entity_recall,
                            candidate_needing_equivalence,
                            parse_semantic_candidate_batch,
                            semantic_candidate_retry_required,
                            symptom_relation_review_required,
                        )
                        from microharness.medical.structured_time import (
                            first_labeled_record_time_from_bindings,
                        )
                        semantic_query_entity = (
                            sq_route.get("_canonical_entity") or kw_pre or ""
                        )
                        if 'candidates' in jd:
                            semantic_batch = parse_semantic_candidate_batch(jd)
                            candidate_decisions = []
                            review_blocks = []
                            semantic_file_name = ab.get('file', '')
                            if semantic_candidate_retry_required(
                                semantic_batch,
                                judge_summary,
                                source_complete=not semantic_source_truncated,
                            ):
                                log(
                                    f'    [语义候选完整性重试] {semantic_file_name}: '
                                    f'{semantic_batch.reason}'
                                )
                                try:
                                    retry_prompt = build_semantic_candidate_retry_prompt(
                                        sq,
                                        semantic_query_entity,
                                        judge_summary,
                                        semantic_batch.reason,
                                    )
                                    retry_response = j.chat(
                                        [{'role': 'user', 'content': retry_prompt}],
                                        temperature=0.0,
                                    )
                                    review_blocks.append(
                                        f'[语义候选完整性重试]\n{retry_response}'
                                    )
                                    log(
                                        f'    [语义候选完整性重试响应] '
                                        f'{semantic_file_name} ({len(retry_response)}字):\n'
                                        f'{retry_response[:1000]}'
                                    )
                                    retry_payload = parse_llm_json(
                                        retry_response,
                                        context=f'语义候选完整性重试:{sq[:30]}',
                                    )
                                    retry_batch = parse_semantic_candidate_batch(
                                        retry_payload
                                    )
                                    if retry_batch.valid and retry_batch.complete:
                                        jd = retry_payload
                                        semantic_batch = retry_batch
                                        log(
                                            f'    [语义候选完整性重试采用] '
                                            f'{semantic_file_name}: 候选数='
                                            f'{len(retry_batch.candidates)}'
                                        )
                                    else:
                                        log(
                                            f'    [语义候选完整性重试拒绝] '
                                            f'{semantic_file_name}: '
                                            f'{retry_batch.reason or "结果仍不完整或无效"}'
                                        )
                                except Exception as retry_error:
                                    log(
                                        f'    [语义候选完整性重试失败] '
                                        f'{semantic_file_name}: {retry_error}'
                                    )
                            record_time = first_labeled_record_time_from_bindings(
                                ab.get('bindings', [])
                            )
                            for candidate_index, candidate_payload in enumerate(
                                semantic_batch.candidates, start=1
                            ):
                                equivalence_payload = None
                                symptom_relation_payload = None
                                semantic_candidate = candidate_needing_equivalence(
                                    candidate_payload,
                                    query_entity=semantic_query_entity,
                                    entity_candidates=sq_route.get('_entity_candidates') or None,
                                    source_text=judge_summary,
                                )
                                if semantic_candidate:
                                    equivalence_prompt = build_semantic_equivalence_prompt(
                                        semantic_query_entity,
                                        semantic_candidate,
                                        sq_route.get('_entity_type', ''),
                                    )
                                    equivalence_response = j.chat(
                                        [{'role': 'user', 'content': equivalence_prompt}],
                                        temperature=0.0,
                                    )
                                    log(
                                        f'    [临床蕴含审核#{candidate_index}] '
                                        f'{semantic_file_name} ({len(equivalence_response)}字):\n'
                                        f'{equivalence_response[:1000]}'
                                    )
                                    review_blocks.append(
                                        f'[候选{candidate_index}严格等价审核]\n{equivalence_response}'
                                    )
                                    equivalence_payload = parse_llm_json(
                                        equivalence_response,
                                        context=(
                                            f'临床蕴含:{semantic_query_entity[:20]}'
                                            f'<-{semantic_candidate[:20]}'
                                        ),
                                    )
                                    if symptom_relation_review_required(equivalence_payload):
                                        symptom_prompt = build_semantic_symptom_relation_prompt(
                                            semantic_query_entity,
                                            semantic_candidate,
                                        )
                                        symptom_response = j.chat(
                                            [{'role': 'user', 'content': symptom_prompt}],
                                            temperature=0.0,
                                        )
                                        log(
                                            f'    [症状同一性复核#{candidate_index}] '
                                            f'{semantic_file_name} ({len(symptom_response)}字):\n'
                                            f'{symptom_response[:1000]}'
                                        )
                                        review_blocks.append(
                                            f'[候选{candidate_index}症状同一性复核]\n{symptom_response}'
                                        )
                                        symptom_relation_payload = parse_llm_json(
                                            symptom_response,
                                            context=(
                                                f'症状同一性:{semantic_query_entity[:20]}'
                                                f'<-{semantic_candidate[:20]}'
                                            ),
                                        )
                                candidate_decisions.append(
                                    assess_semantic_entity_recall(
                                        candidate_payload,
                                        query_entity=semantic_query_entity,
                                        entity_candidates=sq_route.get('_entity_candidates') or None,
                                        source_text=judge_summary,
                                        equivalence_payload=equivalence_payload,
                                        symptom_relation_payload=symptom_relation_payload,
                                        condition=sq,
                                        time_window=time_window,
                                        record_time=record_time,
                                    )
                                )
                            semantic_recall_decision = aggregate_semantic_entity_decisions(
                                candidate_decisions,
                                query_entity=semantic_query_entity,
                                batch=semantic_batch,
                            )
                            recalled_request = replace(
                                domain_request,
                                entity=semantic_query_entity,
                            )
                            recalled_domain_result = execute_recalled_document_domain(
                                recalled_request,
                                judge_summary,
                                semantic_recall_decision,
                                candidate_decisions=tuple(candidate_decisions),
                                candidates_complete=semantic_batch.complete,
                                record_time=record_time,
                            )
                            if recalled_domain_result is None:
                                raise RuntimeError("语义召回结果无法进入病历领域执行器")
                            result = recalled_domain_result.to_file_result(
                                semantic_file_name,
                                fallback_fields=sub_summary,
                            )
                            result['cot_response'] = '\n'.join(
                                [resp, *review_blocks]
                            )[:5000]
                            semantic_status = result['status']
                            semantic_label = {
                                'MATCHED': '符合',
                                'NOT_MATCHED': '不符合',
                                'NOT_MENTIONED': '未提及',
                                'UNKNOWN': '无法判断',
                            }.get(semantic_status, '无法判断')
                            log(
                                f'    [Step4-多候选语义召回] {semantic_file_name}: '
                                f'{semantic_label} — {result["reason"][:80]}'
                            )
                            return result
                        equivalence_payload = None
                        equivalence_response = ""
                        symptom_relation_payload = None
                        symptom_relation_response = ""
                        semantic_candidate = candidate_needing_equivalence(
                            jd,
                            query_entity=semantic_query_entity,
                            entity_candidates=sq_route.get("_entity_candidates") or None,
                            source_text=judge_summary,
                        )
                        if semantic_candidate:
                            equivalence_prompt = build_semantic_equivalence_prompt(
                                semantic_query_entity,
                                semantic_candidate,
                                sq_route.get("_entity_type", ""),
                            )
                            equivalence_response = j.chat(
                                [{"role": "user", "content": equivalence_prompt}],
                                temperature=0.0,
                            )
                            log(
                                f"    [临床蕴含审核] {ab['file']} ({len(equivalence_response)}字):\n"
                                f"{equivalence_response[:1000]}"
                            )
                            equivalence_payload = parse_llm_json(
                                equivalence_response,
                                context=(
                                    f"临床蕴含:{semantic_query_entity[:20]}"
                                    f"<-{semantic_candidate[:20]}"
                                ),
                            )
                            if symptom_relation_review_required(equivalence_payload):
                                symptom_relation_prompt = build_semantic_symptom_relation_prompt(
                                    semantic_query_entity,
                                    semantic_candidate,
                                )
                                symptom_relation_response = j.chat(
                                    [{"role": "user", "content": symptom_relation_prompt}],
                                    temperature=0.0,
                                )
                                log(
                                    f"    [症状同一性复核] {ab['file']} "
                                    f"({len(symptom_relation_response)}字):\n"
                                    f"{symptom_relation_response[:1000]}"
                                )
                                symptom_relation_payload = parse_llm_json(
                                    symptom_relation_response,
                                    context=(
                                        f"症状同一性:{semantic_query_entity[:20]}"
                                        f"<-{semantic_candidate[:20]}"
                                    ),
                                )
                        semantic_record_time = first_labeled_record_time_from_bindings(
                            ab.get("bindings", [])
                        )
                        semantic_recall_decision = assess_semantic_entity_recall(
                            jd,
                            query_entity=semantic_query_entity,
                            entity_candidates=sq_route.get("_entity_candidates") or None,
                            source_text=judge_summary,
                            equivalence_payload=equivalence_payload,
                            symptom_relation_payload=symptom_relation_payload,
                            condition=sq,
                            time_window=time_window,
                            record_time=semantic_record_time,
                        )
                        recalled_request = replace(
                            domain_request,
                            entity=semantic_query_entity,
                        )
                        recalled_domain_result = execute_recalled_document_domain(
                            recalled_request,
                            judge_summary,
                            semantic_recall_decision,
                            candidate_decisions=(semantic_recall_decision,),
                            candidates_complete=True,
                            record_time=semantic_record_time,
                        )
                        if recalled_domain_result is None:
                            raise RuntimeError("语义召回结果无法进入病历领域执行器")
                        result = recalled_domain_result.to_file_result(
                            ab["file"],
                            fallback_fields=sub_summary,
                        )
                        result["cot_response"] = (
                            resp
                            + (
                                "\n[严格等价审核]\n" + equivalence_response
                                if equivalence_response
                                else ""
                            )
                            + (
                                "\n[症状同一性复核]\n" + symptom_relation_response
                                if symptom_relation_response
                                else ""
                            )
                        )[:5000]
                        semantic_status = result["status"]
                        semantic_label = {
                            "MATCHED": "符合",
                            "NOT_MATCHED": "不符合",
                            "NOT_MENTIONED": "未提及",
                            "UNKNOWN": "无法判断",
                        }.get(semantic_status, "无法判断")
                        log(
                            f"    [Step4-语义召回] {ab['file']}: {semantic_label} — "
                            f"{result['reason'][:80]}"
                        )
                        return result
                    # ── reason 字段缺失时用 reasoning 兜底（native thinking 模型习惯不加reason）──
                    _reason = jd.get("reason", "")
                    if not _reason and jd.get("reasoning"):
                        # 取 reasoning 第一句作为用户理由（去掉过长的内部思考）
                        _reasoning = jd["reasoning"].replace('\n', ' ').strip()
                        # 截到第一个句号/分号，或最多60字
                        for sep in ('。', '；', ';', '.'):
                            if sep in _reasoning:
                                _reasoning = _reasoning.split(sep)[0] + '。'
                                break
                        _reason = _reasoning[:80]
                    if not _reason:
                        # 最后兜底：从 resp 中提取干净文本（跳过 markdown 代码块）
                        _clean = resp.replace("```json","").replace("```","").strip()
                        # 尝试从 JSON 内容中提取可读片段
                        _reason = _clean[:80]
                    # ── 清理：去掉 markdown、JSON 残留、换行 ──
                    _reason = _reason.replace('\n', ' ').replace('```', '').strip()
                    if _reason.startswith('{') or _reason.startswith('['):
                        _reason = "已匹配" if jd.get("matched") else "未匹配"

                    result = {"file": ab["file"], "matched": jd.get("matched",False),
                              "reason": _reason, "fields": sub_summary[:2000],
                              "cot_response": resp[:5000]}
                    if document_domain_judge is not None:
                        result["semantic_trace"] = list(
                            document_domain_judge.extra.get("semantic_trace", [])
                        )
                    # ═══════════════════════════════════════════════════════════
                    # 字符重叠安全网：LLM 漏判时 Python 兜底
                    #
                    # 小模型在长文本中注意力稀释，可能漏判明显匹配。
                    # 用字符覆盖度做最后一次校验——不要求连续子串，只要求
                    # 关键字的字符大部分在字段值中出现。
                    #
                    # 有修饰词时也触发，但标记为"关键字级匹配"。
                    # 修饰词的真伪由子条件级修饰词验证链路判断，
                    # 修饰词不满足时会反转子条件结果并更新per-file原因。
                    # ═══════════════════════════════════════════════════════════
                    if not result["matched"] and not sq_route.get("_decomposed_modifiers"):
                        kw = sq_execution_entity
                        semantic_allows_literal_match = (
                            is_external
                            or (
                                document_domain_judge is not None
                                and document_domain_judge.status.value == "MATCHED"
                            )
                        )
                        if kw and len(kw) >= 2 and semantic_allows_literal_match:
                            if _char_overlap_match(kw, judge_summary):
                                result["matched"] = True
                                if document_domain_judge is not None:
                                    result["reason"] = document_domain_judge.reason
                                    result["reason_code"] = document_domain_judge.reason_code
                                    log(
                                        f"    [Step4-判断] {ab['file']}: ✗→✓ 文档语义纠正 — "
                                        f"'{kw}'存在肯定性患者语境"
                                    )
                                else:
                                    result["reason"] = f"[字面纠正] LLM未识别，但关键字'{kw}'存在于字段值中"
                                    log(f"    [Step4-判断] {ab['file']}: ✗→✓ 字面纠正 — '{kw}'在字段值中")
                    # ── 清理原因：LLM未输出JSON时，原因可能是原始推理文本 ──
                    if not result["matched"] and not result["reason"].startswith("失败:") \
                       and not result["reason"].startswith("从CoT文本推断") \
                       and len(result["reason"]) > 60:
                        # 尝试从长文本中提取可读的第一句
                        short = result["reason"].replace('\n',' ').strip()
                        for s in ('。', '；'):
                            if s in short:
                                short = short.split(s)[0] + '。'
                                break
                        result["reason"] = short[:80] if len(short) < len(result["reason"]) else "未能判断"

                    log(f"    [Step4-判断] {ab['file']}: {'✓ 符合' if result['matched'] else '✗ 不符合'} — {result['reason'][:60]}")

                    # Strip internal pipeline markers from user-facing reason
                    for tag in ("[字面纠正] ", "[CoT推断] "):
                        result["reason"] = result["reason"].replace(tag, "")
                    return result
                except Exception as e:
                    return {"file": ab["file"], "matched": False, "reason": f"失败:{str(e)[:60]}", "fields": "", "cot_response": ""}

            with ThreadPoolExecutor(max_workers=min(3, max(1,len(relevant_files)))) as ex:
                futures = {ex.submit(check_one_file, ab): ab for ab in relevant_files}
                for f in as_completed(futures):
                    r = f.result()
                    from microharness.medical.evidence import (
                        annotate_evidence_source,
                        attach_native_evidence_records,
                    )
                    annotate_evidence_source(
                        r,
                        futures[f],
                        primary_source_id=primary_service,
                        time_source_id=getattr(time_window, "source", "") if time_window else "",
                        routed_documents=sq_docs,
                        anchor_documents=anchor_docs,
                        anchor_sections=anchor_sections,
                        time_window_required=bool(time_window and time_window.required),
                        time_window_resolved=bool(time_window and time_window.resolved),
                    )
                    attach_native_evidence_records(
                        r,
                        futures[f],
                        condition=sq,
                        entity=sq_execution_entity,
                        target_sections=sq_sections,
                        target_xml=sq_xml,
                        is_numeric=bool(sq_route.get("is_numeric")),
                    )
                    sq_files.append(r)

            # ═══════════════════════════════════════════════════════════
            # 清理 per-file 原因：去掉内部标记，换成用户能懂的描述
            # ═══════════════════════════════════════════════════════════
            kw_label = sq_execution_entity
            for sf in sq_files:
                raw = sf.get("reason", "")
                if raw.startswith("从CoT文本推断") or raw.startswith("LLM未识别") or raw.startswith("LLM未输出有效JSON"):
                    if sf.get("matched"):
                        sf["reason"] = f"关键字'{kw_label}'匹配"
                    else:
                        sf["reason"] = "未匹配"
                source_role = str(sf.get("source_role") or sf.get("evidence_role") or "CANDIDATE")
                role_labels = {
                    "PRIMARY": "主证据",
                    "SUPPORTING": "辅助证据",
                    "CONTEXT": "上下文",
                    "TIME_ANCHOR": "时间范围依据",
                    "CANDIDATE": "候选证据",
                }
                role_purposes = {
                    "PRIMARY": "用于直接判断当前条件",
                    "SUPPORTING": "用于补充或交叉验证当前条件",
                    "CONTEXT": "用于提供当前条件的上下文",
                    "CANDIDATE": "作为当前条件的候选证据",
                }
                sf["证据角色"] = role_labels.get(source_role, "候选证据")
                sf.setdefault("用途", role_purposes.get(source_role, "作为当前条件的候选证据"))
                if source_role == "TIME_ANCHOR" and time_window:
                    sf["用途"] = f"用于限定{time_window.scope}的起止时间"
                    if time_window.resolved:
                        sf["reason"] = f"{time_window.scope}范围：{time_window.describe()}"
                    elif time_window.reason:
                        sf["reason"] = f"{time_window.scope}范围未解析：{time_window.reason}"

            elapsed = round((time.time() - t0) * 1000)
            # Collect full evidence from matched files (read complete binding)
            evidence = {}
            for f in sq_files:
                fn = f["file"]
                if str(f.get("fields", "")).strip():
                    evidence[fn] = str(f.get("fields", ""))
                    continue
                ab = next((x for x in relevant_files if x["file"] == fn), None)
                if ab:
                    is_ext = ab.get("template", "") not in ("AdmissionRecord","DischargeRecord",
                        "OutpatientAndEmergency","FirstMedicalRecord","DailyMedicalRecord","SurgeryRecord")
                    if is_ext:
                        _recs = {}
                        for b in ab["bindings"]:
                            label = b.get("html_field", ""); val = str(b.get("html_value") or b.get("value", ""))
                            if not val.strip():
                                continue
                            if label.startswith("[") and "] " in label:
                                br = label.index("] ")
                                prefix, field = label[:br+1], label[br+2:]
                            else:
                                prefix, field = "", label
                            _recs.setdefault(prefix, []).append(f"{field}: {val}")
                        full_fields = [f"{p} " + " | ".join(fs) if p else " | ".join(fs)
                                       for p, fs in _recs.items()]
                    else:
                        full_fields = []
                        for b in ab["bindings"]:
                            path = b.get("xml_path", ""); xml_tag = path.split("/")[-1] if path else ""
                            label = b.get("html_field") or xml_tag; val = b.get("html_value") or b.get("value", "")
                            if any(ts in label or ts in path or label in ts or xml_tag in ts for ts in sq_sections):
                                full_fields.append(f"{label}: {str(val)}")
                    evidence[fn] = "\\n".join(full_fields) if full_fields else "(无匹配字段)"
                else:
                    evidence[fn] = "(未找到绑定数据)"
            from microharness.medical.evidence import (
                adjudicate_condition_result,
                assess_condition_confidence,
                build_evidence_items,
                sync_condition_result,
            )
            condition_result = {"condition": sq, "matched": False, "status": "UNKNOWN", "reason": "",
                                "files": sq_files, "docs": sq_docs, "sections": sq_sections,
                                "elapsed_ms": elapsed, "evidence": evidence,
                                "证据明细": build_evidence_items(sq_files)}
            if time_window:
                time_window_data = time_window.to_dict()
                time_window_data["source_label"] = _source_display_label(
                    time_window.source,
                    _service_catalog_for_evidence_plan,
                )
                condition_result["时间范围"] = time_window_data
            from microharness.medical.encounter_consistency import (
                assess_encounter_consistency,
                requires_encounter_consistency,
            )
            if requires_encounter_consistency(sq_files):
                encounter_consistency = assess_encounter_consistency(
                    sq_files,
                    relevant_files,
                ).to_dict()
                condition_result["encounter_consistency"] = encounter_consistency
                condition_result["\u5c31\u8bca\u4e00\u81f4\u6027"] = encounter_consistency
                log(
                    f"  [Step4-\u5c31\u8bca\u4e00\u81f4\u6027] \u6761\u4ef6{cond_no}: "
                    f"\u72b6\u6001={encounter_consistency['status']} | "
                    f"\u963b\u65ad={'\u662f' if encounter_consistency['blocks_adjudication'] else '\u5426'} | "
                    f"\u539f\u56e0={encounter_consistency['reason']}"
                )
            unified_condition = adjudicate_condition_result(condition_result, f"c{cond_no}")
            sync_condition_result(condition_result, unified_condition)
            if unified_condition.conflict_level.value != "NONE":
                log(
                    f"  [Step4-证据裁决] 条件{cond_no}: "
                    f"状态={unified_condition.status.value} | "
                    f"冲突={unified_condition.conflict_level.value}"
                )
            condition_result["置信评估"] = assess_condition_confidence(condition_result)
            condition_result.update(condition_result["置信评估"])
            return condition_result

        # Run sub-conditions in PARALLEL
        per_condition_results = {}
        _condition_text_counts = {
            text: sum(1 for spec in _execution_specs if spec.text == text)
            for text in sub_queries
        }
        _condition_execution_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=min(3, len(_execution_specs))) as cex:
            futures = {
                cex.submit(check_one_condition, spec): spec
                for spec in _execution_specs
            }
            for f in as_completed(futures):
                r = f.result()
                spec = futures[f]
                result_key = spec.text
                if _condition_text_counts.get(spec.text, 0) > 1:
                    result_key = f"{spec.text} [{spec.condition_id}@{spec.position}]"
                per_condition_results[result_key] = r
                log(
                    f"  [Step4-小结] {'✓' if r['matched'] else '✗'} "
                    f"子条件「{spec.text[:30]}」 {r['reason'][:100]}"
                )
        _record_stage("condition_execution_ms", _condition_execution_started)

        # ── Meta-judge: 确定性组合四态子条件 ──
        from microharness.medical.evidence import (
            EvidenceStatus,
            combine_condition_statuses,
            status_label,
        )
        use_and = _use_and
        sub_results = list(per_condition_results.values())
        overall_status = combine_condition_statuses(
            [v.get("status") or v.get("判断状态") for v in sub_results],
            use_and=use_and,
        )
        matched = overall_status == EvidenceStatus.MATCHED
        status_groups = {
            label: [v for v in sub_results if v.get("判断状态") == label]
            for label in ("符合", "不符合", "未提及", "无法判断")
        }
        if len(sub_results) == 1:
            reason = sub_results[0]["reason"]
        elif use_and:
            if overall_status == EvidenceStatus.MATCHED:
                conds = "、".join(f"「{v['condition'][:40]}」" for v in sub_results)
                reason = f"该患者满足全部筛选条件：{conds}"
            elif overall_status == EvidenceStatus.NOT_MATCHED:
                rejected = "、".join(
                    f"「{v['condition'][:40]}」" for v in status_groups["不符合"]
                )
                reason = f"AND条件存在明确不满足项：{rejected}"
            elif overall_status == EvidenceStatus.NOT_MENTIONED:
                missing = "、".join(
                    f"「{v['condition'][:40]}」" for v in status_groups["未提及"]
                )
                reason = f"AND条件中以下目标在已查询数据中未提及：{missing}"
            else:
                unknown = "、".join(
                    f"「{v['condition'][:40]}」" for v in status_groups["无法判断"]
                )
                reason = f"AND条件中以下项目证据不足，当前无法判断：{unknown}"
        else:
            if overall_status == EvidenceStatus.MATCHED:
                hit = "、".join(
                    f"「{v['condition'][:40]}」" for v in status_groups["符合"]
                )
                reason = f"该患者满足以下条件之一：{hit}"
            elif overall_status == EvidenceStatus.NOT_MATCHED:
                reason = "所有OR条件均有相关证据，但均明确不满足"
            elif overall_status == EvidenceStatus.NOT_MENTIONED:
                missing = "、".join(
                    f"「{v['condition'][:40]}」" for v in status_groups["未提及"]
                )
                reason = f"未发现满足的OR条件，且以下目标在已查询数据中未提及：{missing}"
            else:
                unknown = "、".join(
                    f"「{v['condition'][:40]}」" for v in status_groups["无法判断"]
                )
                reason = f"未发现满足的OR条件，且以下项目证据不足：{unknown}"

        op_word = "AND(全部满足)" if use_and else "OR(任一满足)"
        parts = [
            f"{v.get('判断状态', '无法判断')} {v['condition'][:40]}"
            for v in sub_results
        ]
        debug_reason = f"[{op_word}] " + " | ".join(parts)
        log(f"  [Step5-整合] 四态组合: {op_word} → {status_label(overall_status)}")
        log(f"  [Step5-整合] 用户原因: {reason[:120]}")

        execution_elapsed_ms = int((time.perf_counter() - _full_query_start) * 1000)
        # ── Negation flip ──
        # Only flip when negation is EXTERNAL (e.g. "不存在烧伤" = flip the result)
        # Do NOT flip when negation is INTERNAL and already handled by judge
        # (e.g. "术中没有输血" — modifiers=["没有"], judge already checked "输血" not found → matched=true)
        if _negate:
            _has_internal_negation = any(
                spec.internal_negation for spec in _execution_specs
            )
            if _has_internal_negation:
                log(f"  [Step5-取反] 跳过取反：否定已在judge阶段处理(modifiers含否定词)")
            else:
                if overall_status == EvidenceStatus.MATCHED:
                    overall_status = EvidenceStatus.NOT_MATCHED
                elif overall_status == EvidenceStatus.NOT_MATCHED:
                    overall_status = EvidenceStatus.MATCHED
                matched = overall_status == EvidenceStatus.MATCHED
                reason = f"[取反] {reason}"
                log(f"  [Step5-取反] 外部否定 → {status_label(overall_status)}")
        from microharness.medical.evidence import assess_patient_confidence
        patient_confidence = assess_patient_confidence(
            matched,
            reason,
            per_condition_results,
            use_and=use_and,
        )
        status = status_label(overall_status)
        conclusive = overall_status != EvidenceStatus.UNKNOWN
        patient_confidence["判断状态"] = status
        patient_confidence["可判定"] = conclusive
        if status == "无法判断":
            unknown_reasons = []
            for item in per_condition_results.values():
                if not isinstance(item, dict):
                    continue
                item_reason = str(item.get("reason", "") or "")
                item_status = item.get("判断状态")
                if item_status == "符合":
                    continue
                if item_status == "无法判断" or "无法判断" in item_reason:
                    unknown_reasons.append(f"{item.get('condition', '子条件')}：{item_reason}")
                    continue
                for file_result in item.get("files", []) or []:
                    if not isinstance(file_result, dict):
                        continue
                    file_reason = str(file_result.get("reason", "") or "")
                    if "无法判断" in file_reason or "失败" in file_reason or "不可用" in file_reason:
                        unknown_reasons.append(f"{item.get('condition', '子条件')}：{file_result.get('file', '数据源')} {file_reason}")
                        break
            if unknown_reasons:
                reason = "关键证据不足，无法判断：" + "；".join(dict.fromkeys(unknown_reasons))[:300]
            elif "无法判断" not in reason:
                reason = "关键证据不足，无法判断：" + reason

        if status == "符合":
            summary_label = "✓ 患者符合"
        elif status == "不符合":
            summary_label = "✗ 患者不符合"
        elif status == "未提及":
            summary_label = "- 病历未提及"
        else:
            summary_label = "? 无法判断"
        log(f"  [Step5-整合] {summary_label} | {reason[:120]}")
        log(f"  [执行阶段耗时] {execution_elapsed_ms}ms ({execution_elapsed_ms/1000:.1f}s)")

        results = [{
            "register_no": register_no,
            "matched": matched,
            "reason": reason,
            "判断状态": status,
            "可判定": conclusive,
            "置信度": patient_confidence["置信度"],
            "置信等级": patient_confidence["置信等级"],
            "依据等级": patient_confidence["依据等级"],
            "per_condition": per_condition_results,
            "all_files": list(set(f for r in per_condition_results.values() for f in [x["file"] for x in r.get("files",[])])),
        }]
        response_obj = {
            "condition": condition,
            "原始条件": original_condition,
            "规范条件": condition,
            "查询归一化": _normalization.to_dict() if _normalization is not None else {},
            "register_no": register_no,
            "route": analysis,
            "查询IR": _query_ir.to_dict(),
            "IR质量": _ir_quality.to_dict() | {"retried": _ir_retried},
            "证据计划": _evidence_plan.to_dict(),
            "results": results,
            "matched_count": 1 if matched else 0,
            "判断状态": status,
            "可判定": conclusive,
            "置信度": patient_confidence["置信度"],
            "置信等级": patient_confidence["置信等级"],
            "依据等级": patient_confidence["依据等级"],
            "total_ms": execution_elapsed_ms,
        }
        _enrichment_started = time.perf_counter()
        from microharness.medical.evidence import enrich_response_with_evidence_model
        response_obj = enrich_response_with_evidence_model(response_obj, _query_ir)
        _record_stage("evidence_enrichment_ms", _enrichment_started)
        _polish_started = time.perf_counter()
        try:
            from microharness.medical.reason_polisher import polish_response_explanations
            response_obj = polish_response_explanations(
                response_obj,
                model=router_model or judge_model or planner_model,
            )
        except Exception as exc:
            debug_log(f"[解释润色] 跳过: {exc}")
        finally:
            _record_stage("explanation_polish_ms", _polish_started)
        response_obj = _sanitize_response(response_obj)
        response_obj = _finalize_timing_response(response_obj)
        log(
            f"  [全链路总耗时] {response_obj['total_ms']}ms "
            f"({response_obj['total_ms']/1000:.1f}s) | 阶段={response_obj['timings']}"
        )
        log(f"{'='*60}\n")
        return response_obj

    # No patient specified — just return the analysis
    return _finalize_timing_response(_sanitize_response({
        "condition": condition,
        "原始条件": original_condition,
        "规范条件": condition,
        "查询归一化": _normalization.to_dict() if _normalization is not None else {},
        "route": analysis,
        "查询IR": _query_ir.to_dict(),
        "IR质量": _ir_quality.to_dict() | {"retried": _ir_retried},
        "证据计划": _evidence_plan.to_dict(),
    }))


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
async def test_db_config(type: str = ""):
    from microharness.database.db_client import IrisClient, MySQLClient, load_config
    cfg = load_config()
    active_type = (type or cfg.get("type") or "iris").lower()
    try:
        if active_type == "iris":
            c = cfg.get("iris", {})
            client = IrisClient(c.get("base_url",""), c.get("namespace","HDCV2DEV"), c.get("username",""), c.get("password",""))
        else:
            c = cfg.get("mysql", {})
            client = MySQLClient(c.get("host","127.0.0.1"), c.get("port",3306), c.get("database",""), c.get("user",""), c.get("password",""))
        ok = client.test()
        return {"ok": ok, "type": active_type}
    except Exception as e:
        return {"ok": False, "type": active_type, "error": str(e)[:100]}


# ═══════════════════════════════════════════════════════════════
# External Services Config (base_url for diagnosis/drug/encounter APIs)
# ═══════════════════════════════════════════════════════════════

@app.get("/api/external-services/config")
async def get_external_services_config():
    """Return external services config (base_url + service list)."""
    from microharness.services.service_catalog import load_services, _load_base_url
    from pathlib import Path as _P
    _cfg_path = _P(__file__).parent.parent / "configs" / "external_services.json"
    base_url = _load_base_url()
    # Also return the skills list for the frontend
    services = load_services()
    svc_list = {}
    for sid, svc in services.items():
        if sid == "base_url" or not isinstance(svc, dict):
            continue
        svc_list[sid] = {
            "name": svc.get("name", sid),
            "label": svc.get("label", svc.get("name", sid)),
            "url": svc.get("url", ""),
            "description": svc.get("description", ""),
            "triggers": svc.get("triggers", []),
        }
    return {"base_url": base_url, "services": svc_list}


@app.post("/api/external-services/config")
async def save_external_services_config(request: Request):
    """Save external services config."""
    data = await request.json()
    from pathlib import Path as _P
    _cfg_path = _P(__file__).parent.parent / "configs" / "external_services.json"
    existing = {}
    if _cfg_path.exists():
        import json as _j
        existing = _j.loads(_cfg_path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "base_url" in data:
        existing["base_url"] = data["base_url"]
    if isinstance(data, dict) and isinstance(data.get("services"), dict):
        existing["services"] = data["services"]
    _cfg_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "saved",
        "base_url": existing.get("base_url", ""),
        "services": existing.get("services", {}),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
