"""
Prompt Management Module
========================
Harness 的提示管理层 —— 负责在每次启动时给模型注入正确的"入职手册"。

v2 更新：启动时自动读取长期记忆并注入到系统提示，
         让模型感知上次会话做了什么。
"""

from microharness.memory.memory import format_memories_for_prompt, load_memories

def _build_tool_list() -> str:
    """Build the available tools section dynamically based on active tools."""
    # Import here to avoid circular import
    from microharness.agent.tools import TOOLS
    # Note: disabled_skills lives in web.app, which may not be loaded yet
    # So we use a fallback approach - check if we can import it
    try:
        from web.app import disabled_skills
        active = [t for t in TOOLS if t.name not in disabled_skills]
    except Exception:
        active = TOOLS

    lines = ["Available tools:"]
    for t in active:
        if t.name in ("list_files", "read_file", "get_file_info", "write_file", "delete_file", "run_python"):
            sig = f"{t.name}"
            if t.name in ("read_file", "get_file_info"):
                sig += "(filename)"
            elif t.name == "write_file":
                sig += "(filename, content)"
            elif t.name == "run_python":
                sig += "(filename)"
            lines.append(f"- {sig}")

    lines.append("- And other skills as needed")
    return "\n".join(lines)


SYSTEM_PROMPT_BASE = f"""
You are a helpful AI assistant inside a safe harness.

{_build_tool_list()}

Rules:
- You may write files and run Python code inside the sandbox
- You MUST NOT run shell commands that delete, move, or overwrite files outside the workspace
- Always explain what you are about to do before doing it
- If a task is unclear, ask for clarification instead of guessing
- Keep code clean, readable, and well-commented

Workspace: /tmp/sandbox/
"""


def _format_rag_results(docs) -> str:
    """Format RAG search results for injection into prompt."""
    if not docs:
        return ""
    lines = ["\n\n## 知识库检索结果"]
    for i, doc in enumerate(docs, 1):
        preview = doc.content[:300] + "..." if len(doc.content) > 300 else doc.content
        lines.append(f"\n【文档 {i}】{doc.filename}")
        lines.append(f"内容: {preview}")
    return "\n".join(lines)


def _search_rag_if_possible(task: str = None):
    """Try to search RAG based on task keywords."""
    if not task:
        return ""
    try:
        from microharness.rag.rag import rag
        from microharness.rag.rag_config import load_config

        config = load_config()
        vector_weight = config.vector_weight if config.search_mode == "hybrid" else 1.0
        bm25_weight = config.bm25_weight if config.search_mode == "hybrid" else 0.0

        query = task[:100] if len(task) > 100 else task
        results = rag.similarity_search(query, top_k=2, vector_weight=vector_weight, bm25_weight=bm25_weight)
        if results:
            return _format_rag_results(results)
    except Exception:
        pass
    return ""


def get_system_prompt(task: str = None) -> str:
    """
    组装系统提示：基础规则 + 知识库检索结果 + 长期记忆（如果有）。
    """
    from microharness.agent.tools import TOOLS
    try:
        from web.app import disabled_skills
        active = [t for t in TOOLS if t.name not in disabled_skills]
    except Exception:
        active = TOOLS

    tool_lines = []
    for t in active:
        if t.name in ("list_files", "read_file", "get_file_info", "write_file", "delete_file", "run_python"):
            if t.name == "list_files":
                sig = "list_files"
            elif t.name in ("read_file", "get_file_info"):
                sig = f"{t.name}(filename)"
            elif t.name == "write_file":
                sig = f"{t.name}(filename, content)"
            elif t.name == "delete_file":
                sig = f"{t.name}(filename)"
            elif t.name == "run_python":
                sig = f"{t.name}(filename)"
            tool_lines.append(f"- {sig}")

    tool_lines.append("- And other skills as needed")

    tool_block = "\n".join(tool_lines)

    base = f"""You are a helpful AI assistant inside a safe harness.

Available tools:
{tool_block}

Rules:
- You may write files and run Python code inside the sandbox
- You MUST NOT run shell commands that delete, move, or overwrite files outside the workspace
- Always explain what you are about to do before doing it
- If a task is unclear, ask for clarification instead of guessing
- Keep code clean, readable, and well-commented

Workspace: /tmp/sandbox/
"""

    # Inject RAG results if task is provided
    rag_block = _search_rag_if_possible(task)
    if rag_block:
        base += rag_block

    memories = load_memories()
    memory_block = format_memories_for_prompt(memories)

    if memory_block:
        full_prompt = f"{base}\n\n{memory_block}"
        print(f"[HARNESS] Memory loaded: {len(memories)} record(s) injected into prompt.")
        return full_prompt

    return base
