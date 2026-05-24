"""
Medical Document Parser
========================

Extends base document_parser with medical-specific type detection.

Supports:
- Auto-detection of medical document type (drug/guideline/lab/surgery)
- Medical section markers for structured parsing
- All formats supported by base parser (PDF, MD, TXT, HTML)
"""

from typing import Tuple, Optional

# Medical section markers for type detection
MEDICAL_SECTION_MARKERS = {
    "药品": [
        "【药品名称】", "【通用名称】",
        "【适应症】", "【功能主治】",
        "【用法用量】", "【剂量】",
        "【禁忌】", "【注意事项】",
        "【不良反应】", "【副作用】",
        "【药物相互作用】", "【相互作用】",
        "【药理作用】", "【药代动力学】",
        "【储存条件】", "【包装】",
    ],
    "指南": [
        "【指南】", "【临床指南】",
        "【推荐意见】", "【推荐】",
        "【证据级别】", "【证据水平】",
        "【强度推荐】", "【推荐强度】",
        "【诊断标准】", "【治疗原则】",
    ],
    "检验": [
        "【参考值】", "【正常范围】",
        "【临床意义】", "【意义】",
        "【标本采集】", "【采集方法】",
        "【影响因素】", "【干扰因素】",
        "【注意事项】",
    ],
    "手术": [
        "【术前准备】", "【术前评估】",
        "【麻醉方式】", "【麻醉】",
        "【手术步骤】", "【操作步骤】",
        "【术后处理】", "【术后护理】",
        "【并发症】", "【注意事项】",
    ],
}

# Fallback markers (simpler patterns)
FALLBACK_MARKERS = {
    "药品": ["药品名称", "适应症", "用法用量", "禁忌", "不良反应", "副作用"],
    "指南": ["指南", "推荐意见", "证据级别", "推荐强度"],
    "检验": ["参考值", "临床意义", "标本采集", "正常范围"],
    "手术": ["术前准备", "手术步骤", "术后处理", "麻醉方式"],
}


def detect_medical_type(content: str) -> str:
    """
    Auto-detect the medical document type based on content markers.

    Args:
        content: Document text content

    Returns:
        Medical type string: "药品" | "指南" | "检验" | "手术" | "general"
    """
    if not content:
        return "general"

    # Check primary markers (with 【】 brackets - more specific)
    for doc_type, markers in MEDICAL_SECTION_MARKERS.items():
        for marker in markers:
            if marker in content:
                return doc_type

    # Check fallback markers (simpler text)
    for doc_type, markers in FALLBACK_MARKERS.items():
        for marker in markers:
            if marker in content:
                return doc_type

    return "general"


def get_medical_type_display_name(medical_type: str) -> str:
    """Get display name for medical type."""
    names = {
        "药品": "药品",
        "指南": "临床指南",
        "检验": "检验指标",
        "手术": "手术操作",
        "general": "通用文档",
    }
    return names.get(medical_type, medical_type)


def parse_medical_doc(content: bytes, filename: str) -> Tuple[str, str]:
    """
    Parse a medical document and detect its type.

    Args:
        content: Raw file content as bytes
        filename: Original filename (used for format detection)

    Returns:
        Tuple of (parsed_text, medical_type)
    """
    from microharness.rag.document_parser import parse_document

    # Use base parser to extract text
    text = parse_document(content, filename)

    # Detect medical type
    medical_type = detect_medical_type(text)

    return text, medical_type


def split_by_medical_sections(content: str, medical_type: str) -> list:
    """
    Split medical document by section markers.

    Args:
        content: Document text
        medical_type: Detected medical type

    Returns:
        List of section texts
    """
    if medical_type == "general":
        return [content]

    # Use primary markers for splitting
    markers = MEDICAL_SECTION_MARKERS.get(medical_type, [])

    if not markers:
        return [content]

    sections = []
    current_lines = []
    current_section_start = None

    lines = content.split('\n')
    for i, line in enumerate(lines):
        # Check if line starts with a marker
        is_marker = False
        for marker in markers:
            stripped = line.strip()
            if stripped.startswith(marker) or stripped.startswith(f"【{marker[1:]}"):
                is_marker = True
                break

        if is_marker and current_lines:
            # Save current section
            section_text = '\n'.join(current_lines).strip()
            if section_text:
                sections.append(section_text)
            current_lines = []
            current_section_start = i

        current_lines.append(line)

    # Don't forget last section
    if current_lines:
        section_text = '\n'.join(current_lines).strip()
        if section_text:
            sections.append(section_text)

    return sections if sections else [content]


# For backward compatibility with existing code
SUPPORTED_FORMATS = [".pdf", ".md", ".txt", ".html", ".json"]