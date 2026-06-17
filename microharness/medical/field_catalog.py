"""
Medical Field Catalog
=====================
Parse XML clinical document templates and build a field index.

Maps fixed HTML filenames to XML templates, extracts all leaf-node
xml_paths with Chinese descriptions, and defines derived fields
(e.g., 住院天数 = dischargeDateTime - admissionDateTime).

Usage:
    from microharness.medical.field_catalog import get_catalog, match_template

    catalog = get_catalog()
    matched = match_template("出院记录.html")
    # → {"filename": "2.出院记录基本数据集.xml", "docType": "DischargeRecord", ...}
"""

import xml.etree.ElementTree as ET
import re
from pathlib import Path
from typing import Dict, List, Optional

# ── Paths ──────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).parent.parent.parent
# Docker-safe path: templates_xml survives volume mounts
_xml1 = _PROJECT_ROOT / "templates_xml"
_xml2 = _PROJECT_ROOT / "data" / "临床文档模板"
_XML_DIR = _xml1 if (_xml1.exists() and list(_xml1.glob("*.xml"))) else _xml2

# ── Filename → template mapping ────────────────────────────────────
# Fixed HTML filenames the user uploads, mapped to XML template filenames

FILENAME_TO_TEMPLATE: Dict[str, str] = {
    "入院记录.html":     "1.入院记录基本数据集.xml",
    "出院记录.html":     "2.出院记录基本数据集.xml",
    "门急诊病历.html":   "3.门急诊病历基本数据集.xml",
    "首次病程记录.html": "4.首次病程记录 .xml",
    "日常病程记录.html": "5.日常病程记录.xml",
    "手术记录.html":     "6.手术记录基本数据集.xml",
}

# ── Derived fields ─────────────────────────────────────────────────

DERIVED_FIELDS: Dict[str, dict] = {
    "住院天数": {
        "description": "住院天数（出院日期 - 入院日期）",
        "formula": "dischargeDateTime - admissionDateTime",
        "unit": "天",
        "source_template": "2.出院记录基本数据集.xml",
        "source_fields": ["encounter/admissionDateTime", "encounter/dischargeDateTime"],
    }
}

# ── Catalog cache ──────────────────────────────────────────────────

_catalog: Optional[Dict] = None


# ── XML Parsing ────────────────────────────────────────────────────

def _parse_xml_template(filepath: Path) -> dict:
    """
    Parse a single XML template and extract its structure.

    Returns:
        {
            "filename": "2.出院记录基本数据集.xml",
            "docType": "DischargeRecord",
            "title": "出院记录",
            "fields": [
                {"path": "encounter/admissionDateTime", "desc": "入院日期时间", "type": "datetime"},
                ...
            ]
        }
    """
    tree = ET.parse(str(filepath))
    root = tree.getroot()

    doc_type = ""
    title = ""

    # Extract docType from docHeader
    doc_header = root.find("docHeader")
    if doc_header is not None:
        dt = doc_header.find("docType")
        if dt is not None:
            doc_type = (dt.text or "").strip()

    # Extract title from docBody
    doc_body = root.find("docBody")
    if doc_body is not None:
        t = doc_body.find("title")
        if t is not None:
            title = (t.text or "").strip()

    # Extract all leaf-node fields with their comments
    fields = _extract_fields(root, parent_path="")

    return {
        "filename": filepath.name,
        "docType": doc_type,
        "title": title,
        "fields": fields,
    }


def _extract_fields(element: ET.Element, parent_path: str = "") -> List[dict]:
    """
    Recursively extract leaf-node fields with xpath and Chinese descriptions.
    """
    fields = []
    tag = _strip_ns(element.tag)
    current_path = f"{parent_path}/{tag}" if parent_path else tag

    desc = _TAG_TO_DESC.get(tag, tag)

    # Separate child elements from text content
    children = [c for c in element]
    text_content = (element.text or "").strip()

    # Also check for <text> child — common pattern in these templates
    has_text_child = any(_strip_ns(c.tag) == "text" for c in children)

    if not children or (len(children) == 1 and has_text_child):
        # Leaf node — has field value
        field_type = _infer_field_type(desc, text_content)
        fields.append({
            "path": current_path,
            "desc": desc,
            "type": field_type,
        })
    else:
        # Recurse into children
        for child in children:
            fields.extend(_extract_fields(child, current_path))

    # If node has both text and non-text children, also record as a field
    if children and text_content and not has_text_child:
        field_type = _infer_field_type(desc, text_content)
        fields.append({
            "path": current_path,
            "desc": desc,
            "type": field_type,
        })

    return fields


def _infer_field_type(desc: str, sample: str = "") -> str:
    """Infer field type from description and sample value."""
    desc_lower = desc.lower()
    if any(kw in desc_lower for kw in ["日期", "时间", "datetime", "date"]):
        return "datetime"
    if any(kw in desc_lower for kw in ["年龄", "天数", "数", "量", "值"]):
        return "number"
    return "text"


def _strip_ns(tag: str) -> str:
    """Strip XML namespace from tag."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


# ── Tag-to-description fallback mapping ──────────────────────────
# Built from the XML comments in the templates

_TAG_TO_DESC: Dict[str, str] = {
    "ClinicalDocument": "临床文档",
    "docHeader": "文档头",
    "documentId": "源文档ID",
    "version": "模板版本号",
    "hospital": "文档保管机构",
    "docType": "文档类型",
    "docBody": "文档正文",
    "title": "文档标题",
    "recordDatetime": "记录日期时间",
    "physician": "医师签名",
    "attendingPhysicianSign": "上级医师签名",
    "surgeonSign": "术者签名",
    "patient": "患者信息",
    "medicalNo": "病案号",
    "registerNo": "登记号",
    "name": "姓名",
    "gender": "性别",
    "age": "年龄",
    "birthDate": "出生日期",
    "maritalStatus": "婚姻状况",
    "nation": "民族",
    "occupation": "职业",
    "birthAddress": "出生地",
    "address": "住址",
    "company": "工作单位",
    "encounter": "就诊信息",
    "visitNumber": "就诊号",
    "visitDateTime": "就诊日期时间",
    "admissionDateTime": "入院日期时间",
    "admissionDepartment": "入院科室",
    "dischargeDateTime": "出院日期时间",
    "dischargeDepartment": "出院科室",
    "department": "科室",
    "hospitalBed": "病床",
    "chiefComplaint": "主诉",
    "presentHistory": "现病史",
    "pastHistory": "既往史",
    "socialHistory": "个人史",
    "maritalandobstetricHistory": "婚育史",
    "menstrualHistory": "月经史",
    "familyHistory": "家族史",
    "physicalExamination": "体格检查",
    "specificFindings": "专科情况",
    "tcmFourFindings": "中医四诊观察结果",
    "investigations": "辅助检查",
    "preliminaryDiagnosis": "初步诊断",
    "admissionCondition": "入院情况",
    "admissionDiagnosis": "入院诊断",
    "dischargeDiagnosis": "出院诊断",
    "treatmentProcess": "诊疗经过",
    "dischargeCondition": "出院情况",
    "dischargeOrder": "出院医嘱",
    "caseCharacteristics": "病历特点",
    "diagnosticBasis": "诊断依据",
    "differentialDiagnosis": "鉴别诊断",
    "treatment": "治疗意见/诊疗计划",
    "allergy": "过敏史",
    "diagnosis": "诊断",
    "progressNote": "住院病程",
    "surgery": "手术信息",
    "surgeon": "手术医师",
    "surgicalAssistant": "手术助手",
    "surgicalName": "手术名称",
    "anesthesiaMethod": "麻醉方法",
    "anesthesiologist": "麻醉医生",
    "surgeryDate": "手术日期",
    "preoperativeDiagnosis": "术前诊断",
    "intraoperativeDiagnosis": "术中诊断",
    "operativeProcedure": "手术经过",
    "intraoperativeFindingsAndManagement": "术中出现情况及处理",
    "note": "备注",
    "text": "文本内容",
}


# ── Public API ─────────────────────────────────────────────────────

def build_catalog() -> dict:
    """
    Build the complete field catalog from all XML templates.

    Returns:
        {
            "templates": {
                "DischargeRecord": {
                    "title": "出院记录",
                    "filename": "2.出院记录基本数据集.xml",
                    "html_file": "出院记录.html",
                    "fields": [
                        {"path": "patient/name", "desc": "姓名", "type": "text"},
                        ...
                    ]
                },
                ...
            },
            "html_file_map": {
                "出院记录.html": "DischargeRecord",
                ...
            },
            "derived_fields": {
                "住院天数": {...}
            }
        }
    """
    if not _XML_DIR.exists():
        print(f"[FieldCatalog] XML template dir not found: {_XML_DIR}")
        return {"templates": {}, "html_file_map": {}, "derived_fields": DERIVED_FIELDS}

    templates = {}
    html_file_map = {}

    # Build reverse mapping: template filename → html filename
    template_to_html = {v: k for k, v in FILENAME_TO_TEMPLATE.items()}

    for xml_file in sorted(_XML_DIR.glob("*.xml")):
        try:
            info = _parse_xml_template(xml_file)
            doc_type = info["docType"]
            if not doc_type:
                doc_type = xml_file.stem

            html_file = template_to_html.get(xml_file.name, "")

            templates[doc_type] = {
                "title": info["title"],
                "filename": info["filename"],
                "html_file": html_file,
                "fields": info["fields"],
            }

            if html_file:
                html_file_map[html_file] = doc_type

        except Exception as e:
            print(f"[FieldCatalog] Failed to parse {xml_file.name}: {e}")

    return {
        "templates": templates,
        "html_file_map": html_file_map,
        "derived_fields": DERIVED_FIELDS,
    }


def get_catalog(force_rebuild: bool = False) -> dict:
    """
    Get the field catalog. Cached after first build.
    """
    global _catalog
    if _catalog is None or force_rebuild:
        _catalog = build_catalog()
    return _catalog


def match_template(html_filename: str) -> Optional[dict]:
    """
    Match an HTML filename to its XML template info.

    Args:
        html_filename: e.g. "出院记录.html"

    Returns:
        Template info dict or None if no match
    """
    catalog = get_catalog()
    html_file_map = catalog.get("html_file_map", {})
    doc_type = html_file_map.get(html_filename)
    if doc_type:
        return catalog["templates"].get(doc_type)
    return None


def get_template_filename(html_filename: str) -> Optional[str]:
    """Get the XML template filename for a given HTML filename."""
    return FILENAME_TO_TEMPLATE.get(html_filename)


def list_document_types() -> List[dict]:
    """
    List all document types with their HTML filenames and fields.

    Returns:
        [
            {"html_file": "出院记录.html", "docType": "DischargeRecord",
             "title": "出院记录", "template": "2.出院记录基本数据集.xml",
             "field_count": 15},
            ...
        ]
    """
    catalog = get_catalog()
    result = []
    for doc_type, info in catalog.get("templates", {}).items():
        result.append({
            "html_file": info.get("html_file", ""),
            "docType": doc_type,
            "title": info.get("title", ""),
            "template": info.get("filename", ""),
            "field_count": len(info.get("fields", [])),
        })
    return result
