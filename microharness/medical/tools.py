"""
Medical Knowledge Lookup Tool
===============================

Agent tool for searching the medical knowledge base.
"""

from typing import Optional
from langchain_core.tools import tool


@tool
def medical_lookup(query: str, category: Optional[str] = None) -> str:
    """
    Search the medical knowledge base for relevant information.

    Use this tool when you need to look up:
    - Drug information (usage, dosage, contraindications, interactions)
    - Clinical guidelines and recommendations
    - Laboratory test interpretation (reference values, clinical significance)
    - Surgical procedures and protocols

    Args:
        query: Search query (e.g., drug name, lab indicator, disease)
        category: Optional filter by category:
            - "药品": Drug information
            - "指南": Clinical guidelines
            - "检验": Laboratory tests
            - "手术": Surgical procedures

    Returns:
        Formatted search results from the medical knowledge base.
        Returns a message if no results found.
    """
    from microharness.medical import medical_kb

    # Perform search
    results = medical_kb.similarity_search(query, top_k=3, filter_type=category)

    if not results:
        return "未找到相关医学知识。请尝试：\n- 简化查询关键词\n- 检查类别筛选是否过严\n- 确认医学文档已正确导入知识库"

    # Format results
    lines = ["## 医学知识检索结果\n"]

    for i, doc in enumerate(results, 1):
        medical_type = doc.metadata.get("medical_type", "未知")
        doc_type_display = {
            "药品": "药品",
            "指南": "临床指南",
            "检验": "检验指标",
            "手术": "手术操作",
            "general": "通用",
        }.get(medical_type, medical_type)

        lines.append(f"【{i}】[{doc_type_display}] {doc.filename}")

        # Truncate long content
        preview = doc.content[:500] + "..." if len(doc.content) > 500 else doc.content
        lines.append(f"内容: {preview}\n")

    return "\n".join(lines)


@tool
def list_medical_documents() -> str:
    """
    List all documents in the medical knowledge base.

    Returns:
        Formatted list of all indexed medical documents with their types.
    """
    from microharness.medical import medical_kb

    docs = medical_kb.list_documents()

    if not docs:
        return "医学知识库为空。请先上传医学文档。"

    lines = ["## 医学知识库文档列表\n"]
    lines.append(f"共 {len(docs)} 个文档:\n")

    # Group by type
    by_type = {}
    for doc in docs:
        mtype = doc.get("metadata", {}).get("medical_type", "general")
        if mtype not in by_type:
            by_type[mtype] = []
        by_type[mtype].append(doc.get("filename", "unknown"))

    type_names = {
        "药品": "药品",
        "指南": "临床指南",
        "检验": "检验指标",
        "手术": "手术操作",
        "general": "通用",
    }

    for mtype, filenames in by_type.items():
        type_name = type_names.get(mtype, mtype)
        lines.append(f"\n### {type_name} ({len(filenames)}个)")
        for fname in filenames:
            lines.append(f"- {fname}")

    return "\n".join(lines)


@tool
def get_medical_stats() -> str:
    """
    Get statistics about the medical knowledge base.

    Returns:
        Formatted statistics including total document count and breakdown by type.
    """
    from microharness.medical import medical_kb

    stats = medical_kb.get_stats()

    lines = ["## 医学知识库统计\n"]
    lines.append(f"- 文档总数: {stats['total_documents']}")
    lines.append(f"- 索引目录: {stats['index_dir']}")

    if stats['by_type']:
        lines.append("\n### 按类型分布:")
        type_names = {
            "药品": "药品",
            "指南": "临床指南",
            "检验": "检验指标",
            "手术": "手术操作",
            "general": "通用",
        }
        for mtype, count in stats['by_type'].items():
            type_name = type_names.get(mtype, mtype)
            lines.append(f"- {type_name}: {count}个")

    return "\n".join(lines)