"""
Prompt Management Module
========================
Harness 的提示管理层 —— 负责在每次启动时给模型注入正确的"入职手册"。

v2 更新：启动时自动读取长期记忆并注入到系统提示，
         让模型感知上次会话做了什么。
v3 更新：移除重复代码，RAG 搜索改为延迟加载。
"""

from microharness.memory.memory import format_memories_for_prompt, load_memories

# 基础系统提示模板（工具列表由 build_tool_block 动态注入）
SYSTEM_PROMPT_TEMPLATE = """You are a helpful AI assistant inside a safe harness.

Available tools:
{tool_block}

Rules:
- You may write files and run Python code inside the sandbox
- You MUST NOT run shell commands that delete, move, or overwrite files outside the workspace
- Always explain what you are about to do before doing it
- If a task is unclear, ask for clarification instead of guessing
- Keep code clean, readable, and well-commented

Workspace: /tmp/sandbox/"""


def build_tool_block(tools: list = None) -> str:
    """
    动态构建工具列表字符串。

    Args:
        tools: 可选，指定工具列表（用于 web 模式过滤 disabled_skills）

    Returns:
        格式化的工具列表字符串
    """
    from microharness.agent.tools import TOOLS

    if tools is None:
        try:
            from web.app import disabled_skills
            tools = [t for t in TOOLS if t.name not in disabled_skills]
        except Exception:
            tools = TOOLS

    lines = []
    for t in tools:
        # 基础文件操作工具 - 显示完整签名
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
            lines.append(f"- {sig}")
        else:
            # 其他工具只显示名称（技能工具等）
            lines.append(f"- {t.name}")

    return "\n".join(lines)


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


def _get_rag_results(task: str, timeout: float = 0.5) -> str:
    """
    尝试获取 RAG 结果，带超时保护避免阻塞。

    Args:
        task: 搜索关键词
        timeout: 超时秒数，默认 0.5s

    Returns:
        格式化的 RAG 结果字符串，空字符串表示无结果
    """
    if not task:
        return ""

    try:
        import signal

        def timeout_handler(signum, frame):
            raise TimeoutError("RAG search timed out")

        # 设置超时
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(int(timeout))

        try:
            from microharness.rag.rag import rag
            from microharness.rag.rag_config import load_config

            config = load_config()
            vector_weight = config.vector_weight if config.search_mode == "hybrid" else 1.0
            bm25_weight = config.bm25_weight if config.search_mode == "hybrid" else 0.0

            query = task[:100] if len(task) > 100 else task
            results = rag.similarity_search(query, top_k=2, vector_weight=vector_weight, bm25_weight=bm25_weight)

            signal.alarm(0)  # 取消超时
            return _format_rag_results(results) if results else ""
        except TimeoutError:
            signal.alarm(0)
            return ""  # 超时则返回空
        except Exception:
            signal.alarm(0)
            return ""
    except AttributeError:
        # Windows 不支持 signal.SIGALRM，回退到不带超时的版本
        try:
            from microharness.rag.rag import rag
            from microharness.rag.rag_config import load_config

            config = load_config()
            vector_weight = config.vector_weight if config.search_mode == "hybrid" else 1.0
            bm25_weight = config.bm25_weight if config.search_mode == "hybrid" else 0.0

            query = task[:100] if len(task) > 100 else task
            results = rag.similarity_search(query, top_k=2, vector_weight=vector_weight, bm25_weight=bm25_weight)
            return _format_rag_results(results) if results else ""
        except Exception:
            return ""


def get_system_prompt(task: str = None, tools: list = None) -> str:
    """
    组装系统提示：基础规则 + 动态工具列表 + RAG结果 + 长期记忆。

    Args:
        task: 当前任务描述（用于 RAG 搜索，带超时保护）
        tools: 可选，指定工具列表（用于 web 模式过滤 disabled_skills）
    """
    tool_block = build_tool_block(tools)
    base = SYSTEM_PROMPT_TEMPLATE.format(tool_block=tool_block)

    # RAG 结果带超时保护获取
    if task:
        rag_block = _get_rag_results(task)
        if rag_block:
            base += rag_block

    # 加载长期记忆
    memories = load_memories()
    memory_block = format_memories_for_prompt(memories)

    if memory_block:
        base += f"\n\n{memory_block}"
        print(f"[HARNESS] Memory loaded: {len(memories)} record(s) injected into prompt.")

    return base