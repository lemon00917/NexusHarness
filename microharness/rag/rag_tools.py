"""
RAG Tools for NexusHarness
===========================
LangChain tools providing RAG operations to agents.
"""

from typing import Optional

from langchain_core.tools import tool

from microharness.agent.tools import read_file
from microharness.rag.rag import rag, SearchResult


# ──────────────────────── Constants ────────────────────────

# Display limits
CONTENT_PREVIEW_LENGTH = 200
DATE_DISPLAY_LENGTH = 10  # YYYY-MM-DD

# ──────────────────────── Output Formatter ────────────────────────

class OutputFormatter:
    """
    Configurable output formatter for search results and status messages.

    Default uses emoji formatting. Set formatter=None to use plain text.
    """

    def __init__(
        self,
        icons: Optional[dict] = None,
        template: Optional[dict] = None
    ):
        """
        Initialize formatter with custom icons/templates.

        Args:
            icons: Custom icon mappings, e.g. {"success": "✓", "error": "✗"}
            template: Custom format strings for each message type
        """
        self._icons = icons or {}
        self._templates = template or {}

    def icon(self, key: str, default: str = "") -> str:
        """Get icon for a key."""
        return self._icons.get(key, default)

    def format(self, msg_type: str, *args, **kwargs) -> str:
        """Format a message by type, calling the template method."""
        method = getattr(self, f"_format_{msg_type}", None)
        if method:
            return method(*args, **kwargs)
        return str(args[0]) if args else ""

    def _format_search_header(self, query: str, count: int) -> str:
        tpl = self._templates.get("search_header",
            "{icon} 知识库检索结果\n查询: \"{query}\"\n找到 {count} 个相关文档:")
        icon = self.icon("docs", "📚")
        return tpl.format(icon=icon, query=query, count=count)

    def _format_result_header(self, index: int) -> str:
        tpl = self._templates.get("result_header", "{icon} 结果 {index}")
        icon = self.icon("result", "【结果】")
        return tpl.format(icon=icon, index=index)

    def _format_document_list_header(self, count: int) -> str:
        tpl = self._templates.get("document_list_header",
            "{icon} 已索引文档 (共 {count} 个)")
        icon = self.icon("folder", "📁")
        return tpl.format(icon=icon, count=count)

    def _format_document_item(self, index: int, filename: str, doc_id: str,
                              date_str: str) -> str:
        tpl = self._templates.get("document_item",
            "  {index}. {filename}\n     ID: {doc_id} | 添加: {date_str}")
        return tpl.format(index=index, filename=filename,
                         doc_id=doc_id, date_str=date_str)

    def _format_empty_knowledge(self) -> str:
        tpl = self._templates.get("empty_knowledge",
            "{icon} 未找到相关文档。\n\n查询: \"{query}\"\n建议: 尝试使用不同的关键词或更宽泛的描述。")
        icon = self.icon("empty", "📭")
        return tpl.format(icon=icon, query=getattr(self, '_last_query', ''))

    def _format_empty_list(self) -> str:
        tpl = self._templates.get("empty_list",
            "{icon} 知识库为空\n\n当前没有已索引的文档。\n使用 index_document_tool 来添加文档。")
        icon = self.icon("empty", "📭")
        return tpl.format(icon=icon)

    def _format_success(self, filename: str, doc_id: str, count: int) -> str:
        tpl = self._templates.get("success",
            "{icon} 文档索引成功\n   文件: {filename}\n   ID: {doc_id}\n   知识库现有 {count} 个文档")
        icon = self.icon("success", "✅")
        return tpl.format(icon=icon, filename=filename, doc_id=doc_id, count=count)

    def _format_error(self, msg: str) -> str:
        tpl = self._templates.get("error", "{icon} {msg}")
        icon = self.icon("error", "❌")
        return tpl.format(icon=icon, msg=msg)

    def _format_warning(self, msg: str) -> str:
        tpl = self._templates.get("warning", "{icon} {msg}")
        icon = self.icon("warning", "⚠️")
        return tpl.format(icon=icon, msg=msg)

    def _format_health(self, status: str, doc_count: int, searchable: str) -> str:
        tpl = self._templates.get("health",
            "{icon} 知识库健康检查\n  状态: {status}\n  文档数: {doc_count}\n  可搜索: {searchable}")
        icon = self.icon("health", "🏥")
        return tpl.format(icon=icon, status=status,
                         doc_count=doc_count, searchable=searchable)

    def _format_doc_count(self, count: int) -> str:
        tpl = self._templates.get("doc_count",
            "{icon} 知识库中有 {count} 个文档。")
        icon = self.icon("folder", "📁")
        return tpl.format(icon=icon, count=count)


# Global formatter instance (configurable)
_formatter: Optional[OutputFormatter] = None


def get_formatter() -> OutputFormatter:
    """Get or create the global formatter instance."""
    global _formatter
    if _formatter is None:
        _formatter = OutputFormatter()
    return _formatter


def configure_formatter(icons: Optional[dict] = None,
                       templates: Optional[dict] = None) -> OutputFormatter:
    """
    Configure the global output formatter.

    Args:
        icons: Custom icons, e.g. {"success": "OK", "error": "ERROR"}
        templates: Custom templates for message types

    Returns:
        The configured formatter
    """
    global _formatter
    _formatter = OutputFormatter(icons=icons, template=templates)
    return _formatter


# ──────────────────────── Tool: Search Knowledge Base ────────────────────────

@tool
def search_knowledge(query: str, top_k: int = 3) -> str:
    """
    Search the knowledge base for documents relevant to the query.

    Use this tool when you need to find information from indexed documents.
    The search uses semantic understanding, not just keyword matching.

    Args:
        query: Natural language search query (be specific)
        top_k: Maximum number of results to return (1-10, default: 3)

    Returns:
        Formatted search results with document previews

    Examples:
        search_knowledge("What is the authentication flow?")
        search_knowledge("error handling patterns", top_k=5)
    """
    # Validate input
    if not query or not query.strip():
        fmt = get_formatter()
        return fmt.format("warning", "查询不能为空，请提供搜索关键词。")

    # Clamp top_k to reasonable range
    top_k = max(1, min(top_k, 10))

    try:
        results = rag.search(query, top_k=top_k)
    except Exception as e:
        fmt = get_formatter()
        return fmt.format("error", f"搜索失败: {type(e).__name__}: {e}")

    if not results:
        fmt = get_formatter()
        fmt._last_query = query  # Store for _format_empty_knowledge
        return fmt.format("empty_knowledge", query)

    return _format_search_results(query, results)


def _format_search_results(query: str, results: list) -> str:
    """
    Format search results into a readable string.

    Args:
        query: Original search query
        results: List of SearchResult or Document objects

    Returns:
        Formatted results string
    """
    fmt = get_formatter()
    lines = [
        fmt.format("search_header", query, len(results)),
        "",
    ]

    for i, result in enumerate(results, 1):
        lines.extend(_format_single_result(i, result, fmt))

    return "\n".join(lines)


def _format_single_result(index: int, result, fmt: OutputFormatter = None) -> list:
    """
    Format a single search result.

    Args:
        index: Result number (1-based)
        result: SearchResult or Document object
        fmt: OutputFormatter instance

    Returns:
        List of formatted lines
    """
    if fmt is None:
        fmt = get_formatter()

    # Handle both SearchResult and raw Document objects
    if hasattr(result, 'document'):
        doc = result.document
        score = getattr(result, 'score', None)
        matched_chunk = getattr(result, 'matched_chunk', None)
    else:
        doc = result
        score = None
        matched_chunk = None

    # Get content preview
    content = doc.content if hasattr(doc, 'content') else str(doc)
    preview = _truncate_text(content, CONTENT_PREVIEW_LENGTH)

    # Build result lines
    lines = [
        fmt.format("result_header", index),
        f"  文件: {doc.filename}",
    ]

    # Add relevance score if available
    if score is not None:
        lines.append(f"  相关度: {score:.3f}")

    # Add matched chunk indicator
    if matched_chunk:
        lines.append(f"  匹配片段: {_truncate_text(matched_chunk, 100)}")

    lines.extend([
        f"  预览: {preview}",
        "",
    ])

    return lines


# ──────────────────────── Tool: List Documents ────────────────────────

@tool
def list_indexed_documents() -> str:
    """
    List all documents currently in the knowledge base.

    Shows document names, IDs, and creation dates.
    Use this to check what information is available before searching.

    Returns:
        Formatted list of indexed documents
    """
    try:
        docs = rag.list_documents()
    except Exception as e:
        fmt = get_formatter()
        return fmt.format("error", f"获取文档列表失败: {e}")

    if not docs:
        fmt = get_formatter()
        return fmt.format("empty_list")

    return _format_document_list(docs)


def _format_document_list(docs: list) -> str:
    """
    Format document list for display.

    Args:
        docs: List of document dictionaries

    Returns:
        Formatted document list string
    """
    fmt = get_formatter()
    lines = [
        fmt.format("document_list_header", len(docs)),
        "─" * 40,
    ]

    for i, doc in enumerate(docs, 1):
        filename = doc.get('filename', 'unknown')
        doc_id = doc.get('doc_id', 'unknown')
        created_at = doc.get('created_at', '')

        # Truncate date to YYYY-MM-DD
        date_str = created_at[:DATE_DISPLAY_LENGTH] if created_at else 'unknown'

        lines.append(fmt.format("document_item", i, filename, doc_id, date_str))

    lines.append("─" * 40)
    lines.append(f"总计: {len(docs)} 个文档")

    return "\n".join(lines)


# ──────────────────────── Tool: Index Document ────────────────────────

@tool
def index_document_tool(filename: str, visit_id: str) -> str:
    """
    Index a file from the sandbox into the knowledge base.

    Reads the file, adds it to the RAG index, and saves the index.
    Supports text files, markdown, JSON, and other readable formats.

    Args:
        filename: Name of the file to index (must exist in sandbox)
        visit_id: Visit/patient ID to bind to this document

    Returns:
        Success or failure message with document ID

    Examples:
        index_document_tool("documentation.md", "V001")
        index_document_tool("api_reference.txt", "V002")
    """
    if not filename or not filename.strip():
        fmt = get_formatter()
        return fmt.format("warning", "请提供要索引的文件名。")

    # Step 1: Read the file
    read_result = _safe_read_file(filename)
    if read_result.startswith("❌"):
        return read_result

    # Step 2: Add to RAG index
    # auto_save=True by default, no manual save needed
    try:
        doc_id = rag.add_document(
            content=read_result,
            filename=filename,
            visit_id=visit_id
        )
    except ValueError as e:
        fmt = get_formatter()
        return fmt.format("error", f"文档内容无效: {e}")
    except Exception as e:
        fmt = get_formatter()
        return fmt.format("error", f"索引过程出错: {type(e).__name__}: {e}")

    # Return success message
    fmt = get_formatter()
    return fmt.format("success", filename, doc_id, rag.document_count)


def _safe_read_file(filename: str) -> str:
    """
    Safely read a file, returning error message on failure.

    Args:
        filename: File to read

    Returns:
        File content or error message string (never None)
    """
    try:
        return read_file.invoke({"filename": filename})
    except Exception as e:
        return f"❌ 读取文件失败: {e}"


# ──────────────────────── Utility Functions ────────────────────────

def _truncate_text(text: str, max_length: int = CONTENT_PREVIEW_LENGTH) -> str:
    """
    Truncate text to a maximum length with ellipsis.

    Args:
        text: Text to truncate
        max_length: Maximum characters

    Returns:
        Truncated text with "..." if shortened
    """
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."


# ──────────────────────── Additional Convenience Tools ────────────────────────

@tool
def get_document_count() -> str:
    """
    Get the total count of documents in the knowledge base.

    Returns:
        Document count message
    """
    count = rag.document_count
    fmt = get_formatter()

    if count == 0:
        return fmt.format("empty_knowledge")
    return fmt.format("doc_count", count)


@tool
def check_knowledge_health() -> str:
    """
    Check the health status of the knowledge base.

    Returns:
        Health status message with statistics
    """
    try:
        docs = rag.list_documents()
        doc_count = len(docs)

        # Check if search works
        if doc_count > 0:
            test_result = rag.search("test", top_k=1)
            search_ok = len(test_result) >= 0  # Empty is valid
        else:
            search_ok = True  # No docs to search is valid

        fmt = get_formatter()
        status_text = "正常" if search_ok else "异常"
        searchable_text = "是" if search_ok else "否"
        return fmt.format("health", status_text, doc_count, searchable_text)

    except Exception as e:
        fmt = get_formatter()
        return fmt.format("error", f"健康检查失败: {type(e).__name__}: {e}")