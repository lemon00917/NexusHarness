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
import sys
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, Request, UploadFile
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
from microharness.agent.tools import TOOLS as TOOLS

# Ensure utf-8 output
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

app = FastAPI(title="NexusHarness", version="0.1.0")

# Load RAG index at startup
rag.load_index()

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
# RAG API
# ──────────────────────────────────────────────────

@app.post("/api/rag/upload")
async def upload_document(file: UploadFile, description: str = ""):
    """Upload a document to the knowledge base."""
    from microharness.rag.rag import rag
    from microharness.rag.document_parser import parse_document

    # Read file content
    content = await file.read()

    # Parse document based on extension (handles HTML, PDF, MD, TXT, JSON)
    text = parse_document(content, file.filename)

    # Add to RAG index
    metadata = {"description": description, "original_filename": file.filename}
    doc_id = rag.add_document(text, file.filename, metadata)
    rag.save_index()

    return {"doc_id": doc_id, "filename": file.filename, "status": "success"}


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
    for key in ["chunk_mode", "chunk_size", "chunk_overlap", "search_mode", "vector_weight", "bm25_weight"]:
        if key in data and hasattr(config, key):
            setattr(config, key, data[key])

    save_config(config)
    return {"status": "success", "config": config.to_dict()}


@app.get("/api/rag/preview_chunk")
async def preview_chunk(text: str = "", filename: str = ""):
    """Preview how text would be chunked with current config."""
    from microharness.rag.rag import rag
    from microharness.rag.document_parser import parse_document
    if not text:
        return {"chunks": []}
    # Parse HTML if filename indicates HTML content
    if filename.endswith('.html') or filename.endswith('.htm'):
        text = parse_document(text.encode('utf-8'), filename)
    chunks = rag.preview_chunking(text)
    return {"chunks": chunks}


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)