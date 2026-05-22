"""
RAG Tools for NexusHarness
===========================
LangChain tools for RAG operations.
"""

from langchain_core.tools import tool
from microharness.rag.rag import rag


@tool
def search_knowledge(query: str, top_k: int = 3) -> str:
    """
    Search the knowledge base for relevant documents.

    Args:
        query: The search query
        top_k: Number of top results to return (default: 3)
    """
    results = rag.similarity_search(query, top_k)

    if not results:
        return "No relevant documents found."

    output = "=== 知识库检索结果 ===\n\n"
    for i, doc in enumerate(results, 1):
        content_preview = doc.content[:200] + "..." if len(doc.content) > 200 else doc.content
        output += f"【文档 {i}】 {doc.filename}\n"
        output += f"内容: {content_preview}\n\n"

    return output


@tool
def list_indexed_documents() -> str:
    """List all documents in the knowledge base."""
    docs = rag.list_documents()

    if not docs:
        return "知识库为空，暂无索引文档。"

    output = "=== 已索引文档 ===\n\n"
    for doc in docs:
        output += f"• {doc['filename']} (ID: {doc['doc_id']}, 添加于 {doc['created_at'][:10]})\n"

    output += f"\n共 {len(docs)} 个文档"
    return output


@tool
def index_document_tool(filename: str) -> str:
    """
    Index a file from the sandbox into the knowledge base.

    Args:
        filename: Name of the file to index
    """
    from microharness.agent.tools import read_file

    try:
        content = read_file.invoke({"filename": filename})
        if content.startswith("❌"):
            return content

        doc_id = rag.add_document(content, filename)
        rag.save_index()
        return f"✅ 已索引文档: {filename} (ID: {doc_id})"
    except Exception as e:
        return f"❌ 索引失败: {e}"