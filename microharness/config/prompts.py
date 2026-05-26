"""
Prompt Management Module
========================
Harness 的提示管理层 —— 负责在每次启动时给模型注入正确的"入职手册"。

支持动态提示词配置：
- 从 config/prompts.json 加载模板
- 意图识别自动选择对应场景提示
- RAG 结果自动注入

v4 更新：支持意图驱动的动态提示词模板（从 JSON 配置加载）
"""

from microharness.memory.memory import format_memories_for_prompt, load_memories
from microharness.config.prompt_config import (
    load_prompt_config,
    get_intent_by_query,
    format_template,
)

# 基础系统提示模板（工具列表由 build_tool_block 动态注入）
SYSTEM_PROMPT_TEMPLATE = """You are a helpful AI assistant inside a safe harness.

Available tools:
{tool_block}

Rules:
{rule_block}

Workspace: /tmp/sandbox/"""

# 默认安全规则
DEFAULT_RULE_BLOCK = """- You may write files and run Python code inside the sandbox
- You MUST NOT run shell commands that delete, move, or overwrite files outside the workspace
- Always explain what you are about to do before doing it
- If a task is unclear, ask for clarification instead of guessing
- Keep code clean, readable, and well-commented"""


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


def _format_rag_results(results) -> str:
    """Format RAG search results for injection into prompt."""
    if not results:
        return ""
    lines = ["\n\n## 知识库检索结果"]
    for i, result in enumerate(results, 1):
        # Handle both Document (old) and SearchResult (new) formats
        doc = result.document if hasattr(result, 'document') else result
        preview = doc.content[:300] + "..." if len(doc.content) > 300 else doc.content
        lines.append(f"\n【文档 {i}】{doc.filename}")
        lines.append(f"内容: {preview}")
    return "\n".join(lines)


def _get_rag_results(task: str, top_k: int = 2, timeout: float = 0.5) -> str:
    """
    尝试获取 RAG 结果，带超时保护避免阻塞。

    Args:
        task: 搜索关键词
        top_k: 返回结果数量
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
            results = rag.search(query, top_k=top_k, vector_weight=vector_weight, bm25_weight=bm25_weight)

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
            results = rag.search(query, top_k=top_k, vector_weight=vector_weight, bm25_weight=bm25_weight)
            return _format_rag_results(results) if results else ""
        except Exception:
            return ""


def _get_medical_rag_results(task: str, top_k: int = 2, filter_type: str = None) -> str:
    """
    获取医学知识库 RAG 结果。

    Args:
        task: 搜索关键词
        top_k: 返回结果数量
        filter_type: 过滤类型

    Returns:
        格式化的医学 RAG 结果
    """
    if not task:
        return ""

    try:
        from microharness.medical import medical_kb
        results = medical_kb.similarity_search(task, top_k=top_k, filter_type=filter_type)
        return _format_rag_results(results) if results else ""
    except Exception:
        return ""


def get_system_prompt(task: str = None, tools: list = None) -> str:
    """
    组装系统提示：基础规则 + 动态工具列表 + 意图匹配的场景提示 + RAG结果 + 长期记忆。

    Args:
        task: 当前任务描述（用于意图识别和 RAG 搜索）
        tools: 可选，指定工具列表（用于 web 模式过滤 disabled_skills）
    """
    config = load_prompt_config()

    # 构建基础组件
    tool_block = build_tool_block(tools)
    rule_block = config.get("system_prompt", {}).get("variables", {}).get(
        "rule_block", DEFAULT_RULE_BLOCK
    )

    # 检测意图
    intent_name, intent_config = get_intent_by_query(task, config)

    # 尝试意图匹配的场景提示
    if intent_name and intent_config:
        intent_template = intent_config.get("template", "")
        if intent_template:
            # 获取意图对应的 RAG 结果
            rag_block = ""
            if intent_config.get("rag", {}).get("enabled"):
                rag_config = intent_config.get("rag", {})
                top_k = rag_config.get("top_k", 2)
                filter_type = rag_config.get("filter_type")

                # 优先使用医学知识库
                if filter_type in ("药品", "指南", "检验", "手术"):
                    rag_block = _get_medical_rag_results(task, top_k, filter_type)
                else:
                    rag_block = _get_rag_results(task, top_k)

            # 填充场景模板
            variables = {
                "query": task or "",
                "medical_knowledge": rag_block,
            }

            # 构建基础提示
            base = SYSTEM_PROMPT_TEMPLATE.format(
                tool_block=tool_block,
                rule_block=rule_block
            )

            scene_prompt = format_template(intent_template, variables)
            base = base + "\n\n" + scene_prompt

            # 通用 RAG（非医学）
            if not rag_block:
                general_rag = _get_rag_results(task)
                if general_rag:
                    base += general_rag

            # 记忆注入
            memory_config = config.get("memory", {})
            if memory_config.get("enabled", True):
                memories = load_memories()
                max_records = memory_config.get("max_records", 5)
                memory_block = format_memories_for_prompt(memories[:max_records])
                if memory_block:
                    base += f"\n\n{memory_block}"
                    print(f"[HARNESS] Memory loaded: {len(memories)} record(s) injected into prompt.")

            return base

    # 无意图匹配，使用默认提示
    base = SYSTEM_PROMPT_TEMPLATE.format(
        tool_block=tool_block,
        rule_block=rule_block
    )

    # RAG 结果带超时保护获取
    if task:
        rag_config = config.get("rag", {})
        if rag_config.get("auto_inject", True):
            top_k = rag_config.get("top_k", 2)
            rag_block = _get_rag_results(task, top_k)
            if rag_block:
                base += rag_block

    # 记忆注入
    memory_config = config.get("memory", {})
    if memory_config.get("enabled", True):
        memories = load_memories()
        max_records = memory_config.get("max_records", 5)
        memory_block = format_memories_for_prompt(memories[:max_records])
        if memory_block:
            base += f"\n\n{memory_block}"
            print(f"[HARNESS] Memory loaded: {len(memories)} record(s) injected into prompt.")

    return base