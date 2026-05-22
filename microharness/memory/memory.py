"""
Long-Term Memory Module
=======================
Harness 的长期记忆层 —— 跨会话持久化存储关键信息。

工作流程：
  会话结束 → 模型提炼本次要点 → 写入 memory.json
  下次启动 → 读取 memory.json → 注入系统提示
"""

import json
import os
from datetime import datetime

from langchain_core.messages import HumanMessage

from microharness.config.config import get_config

# Memory file at project root (parent of microharness package)
MEMORY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "memory.json")
MAX_MEMORIES = 20


def load_memories() -> list[dict]:
    """读取持久化记忆，文件不存在时返回空列表"""
    if not os.path.exists(MEMORY_FILE):
        return []
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def save_memories(memories: list[dict]) -> None:
    """写入持久化记忆，超过上限时裁剪最旧的条目"""
    memories = memories[-MAX_MEMORIES:]
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memories, f, ensure_ascii=False, indent=2)


def format_memories_for_prompt(memories: list[dict]) -> str:
    """把记忆列表格式化为可注入提示的字符串，只取最近 5 条"""
    if not memories:
        return ""
    lines = ["## Long-Term Memory (from previous sessions)\n"]
    for m in memories[-5:]:
        lines.append(f"- [{m['date']}] {m['summary']}")
    return "\n".join(lines)


def extract_and_save_memory(messages: list, task: str) -> str:
    """
    会话结束后，调用模型提炼本次会话的关键信息，写入持久化存储。
    使用 config.MEMORY_MODEL，通过 config.get_llm() 支持所有 provider。
    """
    history_text = []
    for m in messages:
        if isinstance(m, dict):
            role = m.get("type", "unknown")
            content = m.get("content", "")
        else:
            role = getattr(m, "type", "unknown")
            content = m.content if hasattr(m, "content") else ""
        content = str(content) if not isinstance(content, str) else content
        if content.strip():
            history_text.append(f"[{role}]: {content[:500]}")

    history_str = "\n".join(history_text[-20:])

    extract_prompt = f"""You are a memory extraction assistant.

Given this agent session, extract ONE concise summary sentence (max 80 words) capturing:
- What task was completed
- Key files created or modified
- Any important outcomes or errors

Task: {task}

Session history:
{history_str}

Respond with ONLY the summary sentence, nothing else."""

    from microharness.config.config import get_config, MEMORY_MODEL
    llm = get_config().get_llm(MEMORY_MODEL)
    response = llm.invoke([HumanMessage(content=extract_prompt)])
    summary = response.content.strip()

    memories = load_memories()

    # Skip if the same task was already saved in the last 3 records (dedup)
    recent_tasks = [m.get("task", "") for m in memories[-3:]]
    if task[:100] in recent_tasks:
        return summary  # Skip saving duplicate

    memories.append({
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "task": task[:100],
        "summary": summary,
    })
    save_memories(memories)

    return summary
