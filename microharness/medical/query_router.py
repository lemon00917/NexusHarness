"""
Medical Disease-Section Router
==============================
Lightweight routing skill: disease query → target documents + sections.

Combines:
- Configurable concept→document→section mapping (reliable, fast)
- LLM generalization (handles unmapped queries via few-shot reasoning)
- Integrated with XML field catalog for precise field path resolution

Usable as:
1. Direct Python call:  router.route("糖尿病患者")
2. LangChain tool:    medical_disease_section_router("找出糖尿病患者")
"""

import copy
import json
import os
import re
import threading
from typing import Optional, Dict, List

from microharness.medical.field_catalog import get_catalog, FILENAME_TO_TEMPLATE
from microharness.ollama import OllamaClient


def _router_debug_enabled() -> bool:
    return str(os.environ.get("MEDICAL_QUERY_DEBUG", "")).lower() in {"1", "true", "yes", "on"}


def _router_debug(message: str) -> None:
    if _router_debug_enabled():
        print(message, flush=True)

# ═══════════════════════════════════════════════════════════════
# Section Purpose Catalog (章节用途元数据)
# ─────────────────────────────────────────────────
# Each section has a "purpose" describing its clinical role.
# LLM uses these to match ANY query (disease, symptom, condition)
# to the right sections — no need for exhaustive disease mapping.
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# Document → Section Hierarchy
# ———————————————————————————
# First describe what each document is for, then what each section
# within it records. LLM routes: query → document type → sections.
# ═══════════════════════════════════════════════════════════════

DOCUMENT_CATALOG: Dict[str, dict] = {
    "入院记录": {
        "purpose": "患者入院时建立的首份完整病历，记录入院时已存在的症状、既往病史、入院查体和初步诊断。注意：入院记录不含入院日期字段，入院日期需从就诊信息接口(encounter-info)的encStartDate获取",
        "used_for": ["初次就诊记录", "症状录入", "病史采集", "入院评估", "基础查体", "入院前症状查询"],
        "sections": [
            {"name": "主诉", "purpose": "患者入院时自述的核心主观症状及持续时间（入院前已存在的症状），如'头痛3天''发热伴咳嗽1周'，是判断入院前已有哪些症状的直接证据", "info_type": "主观症状"},
            {"name": "现病史", "purpose": "本次发病全过程：起病时间、诱因、症状演变、伴随症状、外院诊疗经过", "info_type": "发病经过"},
            {"name": "既往史", "purpose": "既往疾病史、手术史、慢性病史（如糖尿病/高血压/乙肝）、过敏史", "info_type": "既往病史"},
            {"name": "个人史", "purpose": "吸烟史、饮酒史、职业暴露、居住地、疫区接触史", "info_type": "生活习惯"},
            {"name": "婚育史", "purpose": "婚姻状况、生育次数和方式（顺产/剖宫产）", "info_type": "婚姻生育"},
            {"name": "月经史", "purpose": "女性月经初潮年龄、周期、经量、痛经情况", "info_type": "女性生理"},
            {"name": "家族史", "purpose": "直系亲属遗传病史、家族聚集性疾病", "info_type": "家族遗传"},
            {"name": "体格检查", "purpose": "体温/脉搏/呼吸/血压等生命体征，各系统查体发现（心肺听诊/腹部触诊/神经查体）", "info_type": "客观体征"},
            {"name": "专科情况", "purpose": "专科查体发现：骨科脊柱四肢、神经科病理征、眼科眼底等", "info_type": "专科查体"},
            {"name": "辅助检查", "purpose": "入院前完成的实验室检查（血常规/生化/血糖/糖化）和影像学检查（CT/MR/X线/超声/心电图）结果", "info_type": "检查检验"},
            {"name": "初步诊断", "purpose": "入院时的初步诊断列表（对应诊断接口的diagTypeDesc=入院诊断），列出所有入院时诊断的疾病", "info_type": "初步诊断"},
            {"name": "中医四诊观察结果", "purpose": "中医望闻问切：舌象、脉象、面色", "info_type": "中医诊察"},
        ]
    },
    "出院记录": {
        "purpose": "患者出院时撰写的总结性病历，记录住院期间诊疗全过程的结论，是查住院天数、最终诊断、治疗结果的核心文档",
        "used_for": ["出院总结", "住院天数查询", "最终诊断", "治疗结果", "出院用药"],
        "sections": [
            {"name": "入院情况", "purpose": "入院时主要症状和体征的简要概述", "info_type": "入院概况"},
            {"name": "入院诊断", "purpose": "入院时经检查确认的诊断", "info_type": "入院诊断"},
            {"name": "诊疗经过", "purpose": "住院期间检查、用药（药名/剂量）、手术、治疗反应的全过程", "info_type": "治疗过程"},
            {"name": "出院诊断", "purpose": "出院时最终诊断结论（主要诊断+次要诊断+合并症），是确诊疾病的权威来源", "info_type": "最终诊断"},
            {"name": "出院情况", "purpose": "出院时症状改善程度、转归、生命体征、出院时状态", "info_type": "出院转归"},
            {"name": "出院医嘱", "purpose": "出院后用药方案、复诊时间、康复指导、饮食建议", "info_type": "出院指导"},
            {"name": "入院日期", "purpose": "本次住院的入院日期时间", "info_type": "时间节点"},
            {"name": "出院日期", "purpose": "本次住院的出院日期时间，与入院日期的差值即为住院天数", "info_type": "时间节点"},
        ]
    },
    "门急诊病历": {
        "purpose": "门诊或急诊就诊记录，记录单次就诊的主诉、查体、诊断和处理意见，不同于住院病历",
        "used_for": ["门诊就诊", "急诊处理", "初次接诊", "过敏史查询"],
        "sections": [
            {"name": "主诉", "purpose": "本次就诊的主要症状和持续时间", "info_type": "主观症状"},
            {"name": "现病史", "purpose": "本次发病经过", "info_type": "发病经过"},
            {"name": "既往史", "purpose": "既往病史", "info_type": "既往病史"},
            {"name": "体格检查", "purpose": "生命体征和查体发现", "info_type": "客观体征"},
            {"name": "辅助检查", "purpose": "相关检查检验结果", "info_type": "检查检验"},
            {"name": "诊断", "purpose": "门急诊诊断结论", "info_type": "诊断"},
            {"name": "治疗意见", "purpose": "门急诊处理措施和治疗建议", "info_type": "治疗意见"},
            {"name": "过敏史", "purpose": "药物、食物等过敏记录", "info_type": "过敏"},
        ]
    },
    "首次病程记录": {
        "purpose": "患者入院后首次病程记录，归纳病情特点、列出诊断依据和鉴别诊断、制定诊疗计划",
        "used_for": ["入院初评", "诊断依据", "鉴别诊断", "诊疗计划"],
        "sections": [
            {"name": "病历特点", "purpose": "患者病情的特征性要点归纳", "info_type": "病情摘要"},
            {"name": "诊断依据", "purpose": "支持诊断的临床表现和检查依据", "info_type": "诊断证据"},
            {"name": "初步诊断", "purpose": "初步诊断列表", "info_type": "诊断"},
            {"name": "鉴别诊断", "purpose": "需要排除的其他可能诊断", "info_type": "鉴别诊断"},
            {"name": "诊疗计划", "purpose": "后续检查和治疗的安排", "info_type": "治疗计划"},
        ]
    },
    "日常病程记录": {
        "purpose": "住院期间每日病程记录，记录每日病情变化、查体发现、治疗反应和并发症",
        "used_for": ["住院期间动态", "病情变化", "用药调整", "并发症观察"],
        "sections": [
            {"name": "住院病程", "purpose": "每日病情变化、查体发现、治疗反应、并发症观察", "info_type": "住院动态"},
            {"name": "科室", "purpose": "患者当前所在科室", "info_type": "科室"},
        ]
    },
    "手术记录": {
        "purpose": "手术过程完整记录，涵盖术前诊断、术中操作、术后恢复、术中量化指标（出血量/输血量/输液量/尿量）、植入物、麻醉、术中异常及处置。'术后''手术'均匹配此文档",
        "used_for": ["手术查询", "术式确认", "麻醉记录", "术中情况", "术后恢复", "术中量化指标查询"],
        "sections": [
            {"name": "手术名称", "purpose": "本次手术的具体术式名称，如阑尾切除术、冠脉搭桥术、剖宫产术", "info_type": "手术术式"},
            {"name": "麻醉方法", "purpose": "所用麻醉方式：全麻/椎管内/局麻等", "info_type": "麻醉方式"},
            {"name": "手术日期", "purpose": "手术开始与结束时间范围，常见格式如“2026年06月10日 15:29--2026年06月10日 16:15”或单个开始时间。术前、术中、术后、手术前、手术中、手术后等相对时间判断均以该字段作为手术时间锚点：术前取开始时间之前，术中取开始至结束之间，术后取结束时间之后；若只有单个时间，则同时作为开始/结束锚点。", "info_type": "手术时间范围", "anchor_field": True, "anchor_aliases": ["手术", "术"], "time_role": "range"},
            {"name": "术前诊断", "purpose": "手术前确认的诊断", "info_type": "术前诊断"},
            {"name": "术中诊断", "purpose": "手术过程中新发现的诊断", "info_type": "术中诊断"},
            {"name": "手术经过", "purpose": "手术全过程详细记录：操作步骤、切除范围、出血量/输血量/输液量/尿量等术中量化指标、植入物信息、术中生命体征、术后即时状态", "info_type": "手术过程"},
            {"name": "术中出现情况及处理", "purpose": "术中量化指标汇总：出血量/输血量/输液量/尿量、术中生命体征、术后即时血压、植入物信息、术中异常事件及处置", "info_type": "术中并发症"},
        ]
    },
}

_DEFAULT_DOCUMENT_CATALOG = copy.deepcopy(DOCUMENT_CATALOG)
SECTION_PURPOSE_LOOKUP: Dict[str, dict] = {}
CATALOG_SOURCE_STATUS: dict = {}
CATALOG_RELOAD_LOCK = threading.RLock()


def format_catalog_source_log(prefix: str = "[病历元数据]", status: dict | None = None) -> str:
    """Build one explicit log line describing the configured and effective source."""
    current = status if status is not None else CATALOG_SOURCE_STATUS
    configured = current.get("configured_source", "local")
    effective = current.get("effective_source", configured)
    source_labels = {"local": "本地配置", "external": "外部接口"}
    parts = [
        prefix,
        f"配置来源={source_labels.get(configured, configured)}",
        f"实际来源={source_labels.get(effective, effective)}",
        f"文档数={current.get('document_count', len(DOCUMENT_CATALOG))}",
        f"是否回退={'是' if current.get('fallback') else '否'}",
    ]
    if configured == "external":
        parts.append(f"外部URL={current.get('external_url') or '-'}")
    if current.get("error"):
        parts.append(f"原因={current['error']}")
    return " | ".join(parts)


def reload_document_catalog(
    log_prefix: str = "[病历元数据配置刷新]",
    emit_log: bool = True,
) -> dict:
    """Reload the active metadata source and rebuild compatibility indexes."""
    global DOCUMENT_CATALOG, SECTION_PURPOSE_LOOKUP, CATALOG_SOURCE_STATUS
    from microharness.medical.catalog_source import load_effective_catalog

    with CATALOG_RELOAD_LOCK:
        DOCUMENT_CATALOG, CATALOG_SOURCE_STATUS = load_effective_catalog(
            _DEFAULT_DOCUMENT_CATALOG
        )
        SECTION_PURPOSE_LOOKUP = {
            section["name"]: section
            for doc_info in DOCUMENT_CATALOG.values()
            for section in doc_info.get("sections", [])
            if isinstance(section, dict) and section.get("name")
        }
        if emit_log:
            print(format_catalog_source_log(log_prefix), flush=True)
        return dict(CATALOG_SOURCE_STATUS)


def reload_document_catalog_snapshot() -> tuple[dict, dict]:
    """Reload metadata and return an isolated catalog snapshot for one query."""
    with CATALOG_RELOAD_LOCK:
        status = reload_document_catalog(emit_log=False)
        return copy.deepcopy(DOCUMENT_CATALOG), status


reload_document_catalog(log_prefix="[病历元数据初始化]")


def _diagnosis_evidence_targets(document_catalog: dict | None = None) -> dict[str, list[str]]:
    """Select disease/symptom evidence sections from catalog metadata."""
    catalog = document_catalog if document_catalog is not None else DOCUMENT_CATALOG
    target_roles = {"disease_symptom_evidence", "diagnosis_evidence", "symptom_evidence"}
    targets: dict[str, list[str]] = {}
    for doc_name, doc_info in catalog.items():
        sections = []
        for sec in doc_info.get("sections", []) or []:
            name = str(sec.get("name") or "")
            roles = sec.get("evidence_roles") or []
            if isinstance(roles, str):
                roles = [roles]
            if name and set(str(role) for role in roles) & target_roles:
                sections.append(name)
        if sections:
            targets[doc_name] = list(dict.fromkeys(sections))
    return targets

# ═══════════════════════════════════════════════════════════════
# Disease → Document → Section Mapping (fast keyword path)
# ═══════════════════════════════════════════════════════════════

_DEFAULT_DISEASE_SECTION_MAP: Dict[str, dict] = {
    "糖尿病": {
        "docs": ["入院记录", "出院记录", "日常病程记录"],
        "sections": ["既往史", "出院诊断", "入院诊断", "初步诊断", "现病史", "主诉"],
        "xml_paths": ["pastHistory", "dischargeDiagnosis", "admissionDiagnosis",
                      "preliminaryDiagnosis", "presentHistory", "chiefComplaint"],
        "note": "糖尿病属慢性基础病，记录于既往史和诊断章节；病程记录含用药调整"
    },
    "高血压": {
        "docs": ["入院记录", "出院记录", "日常病程记录"],
        "sections": ["既往史", "出院诊断", "入院诊断", "体格检查"],
        "xml_paths": ["pastHistory", "dischargeDiagnosis", "admissionDiagnosis",
                      "physicalExamination"],
        "note": "高血压常见于既往史，体格检查含血压值"
    },
    "冠心病": {
        "docs": ["入院记录", "出院记录", "手术记录"],
        "sections": ["既往史", "出院诊断", "入院诊断", "手术名称"],
        "xml_paths": ["pastHistory", "dischargeDiagnosis", "admissionDiagnosis",
                      "surgicalName"],
        "note": "冠心病如需手术，手术记录含冠脉搭桥/支架信息"
    },
    "肺炎": {
        "docs": ["入院记录", "出院记录"],
        "sections": ["现病史", "出院诊断", "入院诊断", "主诉", "体格检查"],
        "xml_paths": ["presentHistory", "dischargeDiagnosis", "admissionDiagnosis",
                      "chiefComplaint", "physicalExamination"],
        "note": "肺炎属急性感染，主诉和现病史含呼吸道症状"
    },
    "骨折": {
        "docs": ["入院记录", "出院记录", "手术记录", "日常病程记录"],
        "sections": ["主诉", "现病史", "出院诊断", "手术名称", "体格检查"],
        "xml_paths": ["chiefComplaint", "presentHistory", "dischargeDiagnosis",
                      "surgicalName", "physicalExamination"],
        "note": "骨折可能涉及手术，手术记录含术式信息"
    },
    "乳腺": {
        "docs": ["入院记录", "出院记录", "手术记录", "门急诊病历", "日常病程记录"],
        "sections": ["主诉", "现病史", "既往史", "入院诊断", "出院诊断", "初步诊断",
                      "术前诊断", "术中诊断", "手术名称", "诊断"],
        "xml_paths": ["chiefComplaint", "presentHistory", "pastHistory",
                      "admissionDiagnosis", "dischargeDiagnosis", "preliminaryDiagnosis",
                      "preoperativeDiagnosis", "intraoperativeDiagnosis", "surgicalName", "diagnosis"],
        "note": "乳腺肿块/肿瘤可能在主诉、诊断、手术记录（切除手术）、病理等多个章节出现"
    },
    "肿块": {
        "docs": ["入院记录", "出院记录", "手术记录", "门急诊病历"],
        "sections": ["主诉", "现病史", "入院诊断", "出院诊断", "初步诊断",
                      "术前诊断", "术中诊断", "手术名称", "诊断"],
        "xml_paths": ["chiefComplaint", "presentHistory", "admissionDiagnosis",
                      "dischargeDiagnosis", "preliminaryDiagnosis",
                      "preoperativeDiagnosis", "intraoperativeDiagnosis", "surgicalName", "diagnosis"],
        "note": "肿块可在主诉、诊断、手术记录中出现"
    },
    "肿瘤": {
        "docs": ["入院记录", "出院记录", "手术记录", "日常病程记录"],
        "sections": ["主诉", "现病史", "既往史", "出院诊断", "入院诊断", "手术名称"],
        "xml_paths": ["chiefComplaint", "presentHistory", "pastHistory",
                      "dischargeDiagnosis", "admissionDiagnosis", "surgicalName"],
        "note": "肿瘤相关可能出现在多个诊断章节和手术记录"
    },
    "手术": {
        "docs": ["手术记录", "出院记录", "入院记录"],
        "sections": ["手术名称", "麻醉方法", "手术日期", "术前诊断", "术中诊断", "手术经过"],
        "xml_paths": ["surgicalName", "anesthesiaMethod", "surgeryDate",
                      "preoperativeDiagnosis", "intraoperativeDiagnosis", "operativeProcedure"],
        "note": "手术信息集中在手术记录"
    },
    "过敏": {
        "docs": ["入院记录", "门急诊病历"],
        "sections": ["过敏史", "既往史"],
        "xml_paths": ["allergy", "pastHistory"],
        "note": "过敏史在门急诊病历和入院记录既往史中"
    },
    "住院天数": {
        "docs": ["出院记录"],
        "sections": ["入院日期", "出院日期"],
        "xml_paths": ["admissionDateTime", "dischargeDateTime"],
        "note": "住院天数 = 出院日期 - 入院日期，只有出院记录同时包含两者"
    },
    "住院": {
        "docs": ["出院记录"],
        "sections": ["入院日期", "出院日期", "出院情况"],
        "xml_paths": ["admissionDateTime", "dischargeDateTime", "dischargeCondition"],
        "note": "住院相关信息集中在出院记录"
    },
    "长期住院": {
        "docs": ["出院记录"],
        "sections": ["入院日期", "出院日期"],
        "xml_paths": ["admissionDateTime", "dischargeDateTime"],
        "note": "住院时长需要出院记录的入院/出院日期计算"
    },
    "诊断": {
        "docs": ["出院记录", "入院记录", "门急诊病历", "首次病程记录"],
        "sections": ["出院诊断", "入院诊断", "初步诊断", "诊断"],
        "xml_paths": ["dischargeDiagnosis", "admissionDiagnosis", "preliminaryDiagnosis", "diagnosis"],
        "note": "诊断出现在多种病历中"
    },
    "用药": {
        "docs": ["出院记录", "日常病程记录", "门急诊病历"],
        "sections": ["出院医嘱", "诊疗经过", "治疗意见"],
        "xml_paths": ["dischargeOrder", "treatmentProcess", "treatment"],
        "note": "用药信息主要在出院医嘱和诊疗经过"
    },
    "检查": {
        "docs": ["入院记录", "门急诊病历"],
        "sections": ["辅助检查", "体格检查"],
        "xml_paths": ["investigations", "physicalExamination"],
        "note": "辅助检查和体格检查含检查结果"
    },
    "年龄": {
        "docs": ["入院记录", "出院记录", "门急诊病历", "手术记录", "首次病程记录", "日常病程记录"],
        "sections": ["年龄"],
        "xml_paths": ["age"],
        "note": "年龄在所有病历的患者信息中都有"
    },
    "性别": {
        "docs": ["入院记录", "出院记录", "门急诊病历", "手术记录", "首次病程记录", "日常病程记录"],
        "sections": ["性别"],
        "xml_paths": ["gender"],
        "note": "性别在所有病历中都有"
    },
    # ── 症状/体征类 ────────────────────────────
    "头痛": {
        "docs": ["入院记录", "出院记录", "门急诊病历", "首次病程记录"],
        "sections": ["主诉", "现病史", "入院诊断", "出院诊断", "初步诊断", "体格检查"],
        "xml_paths": ["chiefComplaint", "presentHistory", "admissionDiagnosis",
                      "dischargeDiagnosis", "preliminaryDiagnosis", "physicalExamination"],
        "note": "头痛属常见症状，可能存在主诉、诊断、体格检查等多个章节"
    },
    "发热": {
        "docs": ["入院记录", "出院记录", "门急诊病历", "日常病程记录"],
        "sections": ["主诉", "现病史", "入院诊断", "出院诊断", "体格检查"],
        "xml_paths": ["chiefComplaint", "presentHistory", "admissionDiagnosis",
                      "dischargeDiagnosis", "physicalExamination"],
        "note": "发热属常见症状，主诉和体格检查常见体温记录"
    },
    "胸痛": {
        "docs": ["入院记录", "门急诊病历", "出院记录"],
        "sections": ["主诉", "现病史", "入院诊断", "出院诊断", "体格检查"],
        "xml_paths": ["chiefComplaint", "presentHistory", "admissionDiagnosis",
                      "dischargeDiagnosis", "physicalExamination"],
        "note": "胸痛可能涉及心血管，主诉和诊断章节多见"
    },
    "背痛": {
        "docs": ["入院记录", "出院记录", "门急诊病历", "日常病程记录"],
        "sections": ["主诉", "现病史", "体格检查", "专科情况", "入院诊断", "出院诊断"],
        "xml_paths": ["chiefComplaint", "presentHistory", "physicalExamination",
                      "specificFindings", "admissionDiagnosis", "dischargeDiagnosis"],
        "note": "背痛属脊柱/肌肉骨骼症状，主诉和专科查体多见，病程记录含日常变化"
    },
    "腹痛": {
        "docs": ["入院记录", "门急诊病历", "出院记录"],
        "sections": ["主诉", "现病史", "入院诊断", "出院诊断", "体格检查"],
        "xml_paths": ["chiefComplaint", "presentHistory", "admissionDiagnosis",
                      "dischargeDiagnosis", "physicalExamination"],
        "note": "腹痛属消化系统常见主诉，体格检查也有腹部查体"
    },
    "咳嗽": {
        "docs": ["入院记录", "门急诊病历", "出院记录"],
        "sections": ["主诉", "现病史", "入院诊断", "出院诊断"],
        "xml_paths": ["chiefComplaint", "presentHistory", "admissionDiagnosis", "dischargeDiagnosis"],
        "note": "咳嗽属呼吸道症状，主诉和诊断章节常见"
    },
    "恶心": {
        "docs": ["入院记录", "门急诊病历"],
        "sections": ["主诉", "现病史", "体格检查"],
        "xml_paths": ["chiefComplaint", "presentHistory", "physicalExamination"],
        "note": "恶心多为伴随症状，主诉和现病史中记载"
    },
    "呕吐": {
        "docs": ["入院记录", "门急诊病历"],
        "sections": ["主诉", "现病史"],
        "xml_paths": ["chiefComplaint", "presentHistory"],
        "note": "呕吐常与恶心一同出现于主诉"
    },
    "贫血": {
        "docs": ["入院记录", "出院记录"],
        "sections": ["既往史", "入院诊断", "出院诊断", "体格检查"],
        "xml_paths": ["pastHistory", "admissionDiagnosis", "dischargeDiagnosis", "physicalExamination"],
        "note": "贫血可在既往史、诊断、查体中发现"
    },
    "黄疸": {
        "docs": ["入院记录", "出院记录"],
        "sections": ["主诉", "现病史", "体格检查", "入院诊断", "出院诊断"],
        "xml_paths": ["chiefComplaint", "presentHistory", "physicalExamination",
                      "admissionDiagnosis", "dischargeDiagnosis"],
        "note": "黄疸属肝胆系统体征，查体和诊断均有记录"
    },
    # ── 生活习惯/环境 ──────────────────────
    "吸烟": {
        "docs": ["入院记录"],
        "sections": ["个人史"],
        "xml_paths": ["socialHistory"],
        "note": "吸烟史记录在个人史"
    },
    "饮酒": {
        "docs": ["入院记录"],
        "sections": ["个人史"],
        "xml_paths": ["socialHistory"],
        "note": "饮酒史记录在个人史"
    },
    "家族遗传": {
        "docs": ["入院记录"],
        "sections": ["家族史"],
        "xml_paths": ["familyHistory"],
        "note": "家族遗传病史记录在家族史"
    },
    # ── 生育相关 ──────────────────────────
    "剖宫产": {
        "docs": ["入院记录", "手术记录"],
        "sections": ["婚育史", "手术名称", "手术经过"],
        "xml_paths": ["maritalandobstetricHistory", "surgicalName", "operativeProcedure"],
        "note": "剖宫产在婚育史（生育方式）和手术记录均有记载"
    },
    "顺产": {
        "docs": ["入院记录"],
        "sections": ["婚育史"],
        "xml_paths": ["maritalandobstetricHistory"],
        "note": "顺产记录在婚育史"
    },
    # ── 检查相关 ──────────────────────────
    "CT": {
        "docs": ["入院记录", "门急诊病历"],
        "sections": ["辅助检查"],
        "xml_paths": ["investigations"],
        "note": "CT检查结果记录在辅助检查"
    },
    "MRI": {
        "docs": ["入院记录", "门急诊病历"],
        "sections": ["辅助检查"],
        "xml_paths": ["investigations"],
        "note": "MRI/磁共振结果记录在辅助检查"
    },
    "磁共振": {
        "docs": ["入院记录", "门急诊病历"],
        "sections": ["辅助检查"],
        "xml_paths": ["investigations"],
        "note": "磁共振结果记录在辅助检查"
    },
    "心电图": {
        "docs": ["入院记录", "门急诊病历"],
        "sections": ["辅助检查"],
        "xml_paths": ["investigations"],
        "note": "心电图结果记录在辅助检查"
    },
    "超声": {
        "docs": ["入院记录", "门急诊病历"],
        "sections": ["辅助检查"],
        "xml_paths": ["investigations"],
        "note": "超声检查结果记录在辅助检查"
    },
    "X线": {
        "docs": ["入院记录", "门急诊病历"],
        "sections": ["辅助检查"],
        "xml_paths": ["investigations"],
        "note": "X线检查结果记录在辅助检查"
    },
}


def _load_disease_section_map(default_map: Dict[str, dict]) -> Dict[str, dict]:
    """Load project-specific concept routing metadata.

    The built-in map is retained only as a compatibility fallback. Projects can
    put routing metadata in configs/medical_routing_map.json:

    {
      "replace_default": false,
      "routes": {
        "概念": {"docs": [...], "sections": [...], "xml_paths": [...], "note": "..."}
      }
    }

    A plain JSON object of routes is also accepted.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    candidates = [
        root / "configs" / "medical_routing_map.json",
        root / "configs" / "disease_section_map.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            _router_debug(f"[路由配置] 读取失败 {path.name}: {exc}")
            continue
        if not isinstance(data, dict):
            _router_debug(f"[路由配置] {path.name} 不是JSON对象，忽略")
            continue
        replace_default = bool(data.get("replace_default"))
        routes = data.get("routes") if isinstance(data.get("routes"), dict) else data
        if not isinstance(routes, dict):
            _router_debug(f"[路由配置] {path.name} 缺少routes对象，忽略")
            continue
        merged = dict(routes) if replace_default else {**default_map, **routes}
        _router_debug(
            f"[路由配置] 使用 {path.name}: routes={len(routes)}, replace_default={replace_default}"
        )
        return merged
    return dict(default_map)


DISEASE_SECTION_MAP: Dict[str, dict] = _load_disease_section_map(_DEFAULT_DISEASE_SECTION_MAP)

# ═══════════════════════════════════════════════════════════════
# Shared Utility
# ═══════════════════════════════════════════════════════════════

def parse_llm_json(text: str, context: str = "") -> dict:
    """Extract and parse JSON from LLM response. Handles CoT output with JSON at end.

    Also validates CoT step format if present (第1步…第4步), logging any
    structural issues instead of silently returning {{}}.
    """
    cleaned = text.strip()

    # ── CoT step validation (non-breaking: only logs issues) ──
    _validate_cot_steps(cleaned, context)

    # 1) Try markdown fences
    for fence in ("```json", "```"):
        if fence in cleaned:
            parts = cleaned.split(fence)
            if len(parts) >= 2:
                inner = parts[1].split("```")[0] if "```" in parts[1] else parts[1]
                cleaned = inner.strip()
                break
    # 2) Try to parse directly
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass
    # 3) CoT output: find the LAST {...} JSON object in the text
    brace_depth = 0
    json_start = -1
    json_end = -1
    for i in range(len(cleaned) - 1, -1, -1):
        if cleaned[i] == '}':
            if brace_depth == 0:
                json_end = i
            brace_depth += 1
        elif cleaned[i] == '{':
            brace_depth -= 1
            if brace_depth == 0:
                json_start = i
                break
    if json_start >= 0:
        try:
            return json.loads(cleaned[json_start:json_end + 1])
        except (json.JSONDecodeError, ValueError) as e:
            _router_debug(f"[CoT解析] JSON提取失败({context}): {str(e)[:80]}")
            _router_debug(f"[CoT解析] 原始响应: {text[:400]}")
    else:
        _router_debug(f"[CoT解析] 未找到JSON对象({context})")
        _router_debug(f"[CoT解析] 原始响应: {text[:400]}")
    # ── Final fallback: infer verdict from CoT text ──
    return _infer_verdict_from_cot(text, context)


def _infer_verdict_from_cot(text: str, context: str = "") -> dict:
    """When JSON parsing fails, try to infer matched=true/false from CoT reasoning text.

    Looks for explicit verdict phrases in Chinese that the model may have written
    before forgetting to output the JSON.
    """
    import re as _ire
    t = text
    # Check negative FIRST (不匹配 contains 匹配, so order matters)
    neg = _ire.search(r'(?:不匹配|不符合|不满足|未找到|不存在|无匹配|没有匹配)', t)
    pos = _ire.search(r'(?:[^不]匹配|符合|满足|找到|存在|有匹配)', t)
    if neg and not pos:
        _router_debug(f"[CoT推断] {context}文本推断→matched=false (不匹配/不符合/未找到)")
        return {"matched": False, "reason": "从CoT文本推断:不匹配"}
    if pos and not neg:
        _router_debug(f"[CoT推断] {context}文本推断→matched=true (匹配/符合/满足)")
        return {"matched": True, "reason": "从CoT文本推断:匹配"}
    if neg and pos:
        # Both present: model is reasoning bidirectionally, can't infer reliably
        # Long text means the model is thinking out loud, not concluding
        if len(t) > 400:
            _router_debug(f"[CoT推断] {context}文本过长({len(t)}字)且正反标记并存 → 放弃推断")
            return {}
        # Short text: count occurrences to break tie
        nc = len(_ire.findall(r'不匹配|不符合|不满足|未找到|不存在|无匹配', t))
        pc = len(_ire.findall(r'匹配|符合|满足|找到|存在', t)) - nc
        # Both present: model is reasoning bidirectionally, likely confused
        # Don't guess — return {} and let the mechanical safety net decide
        if nc > 0 and pc > 0:
            _router_debug(f"[CoT推断] {context}正反标记并存，放弃推断 (neg={nc} pos={pc})")
            return {}
        if nc > pc:
            _router_debug(f"[CoT推断] {context}计数推断→matched=false (neg={nc} pos={pc})")
            return {"matched": False, "reason": "从CoT文本推断:不匹配占多"}
        elif pc > nc:
            _router_debug(f"[CoT推断] {context}计数推断→matched=true (neg={nc} pos={pc})")
            return {"matched": True, "reason": "从CoT文本推断:匹配占多"}
    _router_debug(f"[CoT推断] {context}无法推断(歧义或无语义标记)")
    return {}


def _validate_cot_steps(text: str, context: str = "") -> None:
    """Check if CoT output contains expected 4-step structure. Logs issues only.

    Expected format:
        第1步 关键词：...
        第2步 候选：...
        第3步 判断：...
        第4步 JSON：...
    """
    import re as _vre
    steps_found = []
    for i in range(1, 5):
        pat = f"第{i}步"
        if pat in text:
            steps_found.append(i)
    missing = [i for i in range(1, 5) if i not in steps_found]
    if missing and len(steps_found) > 0:
        # Only warn if the model started CoT but didn't finish
        _router_debug(f"[CoT校验] {context}缺少步骤: 第{missing}步 (已有: 第{steps_found}步)")
    elif not steps_found and text.strip():
        # Model didn't follow CoT format at all — note but don't fail
        has_json = bool(_vre.search(r'\{[^}]+\}', text))
        if has_json:
            pass  # JSON present without CoT wrapper, acceptable fallback
        else:
            truncated = text[:200].replace('\n', '\\n')
            _router_debug(f"[CoT校验] {context}模型未按CoT格式输出，无步骤标记 | 内容: {truncated}")


def _normalize_route_label(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^[《<「『【\[]+|[》>」』】\]]+$", "", text)
    text = re.sub(r"\s+", "", text)
    return text


def _normalize_catalog_name(value: str, valid_names: set[str]) -> tuple[str, str]:
    """Map minor LLM label variants back to catalog names."""
    raw = str(value or "")
    cleaned = _normalize_route_label(raw)
    if cleaned in valid_names:
        return cleaned, "" if cleaned == raw else f"{raw}->{cleaned}"
    candidates = [cleaned]
    for suffix in ("文档", "病历", "记录单", "表单", "表"):
        if cleaned.endswith(suffix):
            candidates.append(cleaned[: -len(suffix)])
    for candidate in candidates:
        if candidate in valid_names:
            return candidate, f"{raw}->{candidate}"
    matches = [name for name in valid_names if name in cleaned or cleaned in name]
    if len(matches) == 1:
        return matches[0], f"{raw}->{matches[0]}"
    return "", ""


# ═══════════════════════════════════════════════════════════════
# Router System Prompt
# ═══════════════════════════════════════════════════════════════

ROUTER_SYSTEM = """你是病历查询路由专家。查询→文档→章节。

## 选择方法
1. 理解查询核心意图，判断属于哪个医疗场景（手术/住院/门诊/病程）
2. 对比文档用途(purpose)，选最匹配的1个文档
3. 在该文档的章节中，找出所有可能包含相关信息的章节

## 关键规则
- 同一查询的答案可能分散在多个互补章节中（如操作记录章+量化汇总章），必须全选，不能只选一个
- 每选一个章节自问：该章节的用途描述是否与查询意图语义相关
- 章节名必须逐字复制目录中的名称，禁止编造
- 不确定时宁可多选，不要漏选

## 输出JSON
只允许使用上方目录中真实存在的文档名和章节名；不要输出“文档名”“章节1”这类占位符。
示例：
{"targets":{"入院记录":["主诉","现病史"]},"match_reason":{"入院记录.主诉":"查询入院症状"}}"""


# ═══════════════════════════════════════════════════════════════
# QueryRouter
# ═══════════════════════════════════════════════════════════════

class QueryRouter:
    """Disease-aware medical query router."""

    def __init__(self, model: str = None, timeout: int = 120,
                 document_catalog: dict | None = None):
        if model is None:
            model = "medaibase/medgemma1.5:4b"
        self.model = model
        self.timeout = timeout
        self.catalog = get_catalog()
        self.document_catalog = copy.deepcopy(
            document_catalog if document_catalog is not None else DOCUMENT_CATALOG
        )
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = OllamaClient(model=self.model, timeout=self.timeout)
        return self._client

    # ── Keyword matching (fast path, no LLM) ──────────────────

    def _services_for_sections(self, sections: list[str]) -> list[str]:
        diagnosis_sections = {
            "既往史", "现病史", "主诉", "初步诊断", "入院诊断", "出院诊断",
            "诊断", "术前诊断", "术中诊断", "术后诊断",
        }
        if set(sections or []) & diagnosis_sections:
            return ["diagnosis-query"]
        return []

    def match_keywords(self, condition: str) -> Optional[dict]:
        """Check if condition matches any known disease in the map.
        For multi-keyword queries, merge all matching entries.
        Keywords must be 2+ chars to avoid false positives (e.g., '诊断' in '鉴别诊断')."""
        matches = []
        for keyword, mapping in DISEASE_SECTION_MAP.items():
            if keyword in condition:
                # For short keywords (<=3 chars), require word-boundary match
                if len(keyword) <= 3:
                    # Check the keyword is a standalone word, not part of a longer word
                    idx = condition.find(keyword)
                    if idx >= 0:
                        before_ok = idx == 0 or condition[idx-1] in '，,、。；; '
                        after_end = idx + len(keyword)
                        after_ok = after_end >= len(condition) or condition[after_end] in '，,、。；; 的患者大小高低于等'
                        if not (before_ok and after_ok):
                            continue
                matches.append((keyword, mapping))

        if not matches:
            return None

        # Merge all matches → per-document targets
        targets = {}
        all_xml = []
        keywords = []
        notes = []
        doc_catalog = self.document_catalog  # per-query routing catalog snapshot
        for kw, mp in matches:
            keywords.append(kw)
            notes.append(mp.get("note", ""))
            for d in mp.get("docs", []):
                doc_info = doc_catalog.get(d, {})
                doc_sections = doc_info.get("sections", [])
                doc_sec_names = {s["name"] for s in doc_sections} if doc_sections else set()
                # Only include sections that exist in this document
                valid = [s for s in mp.get("sections", []) if s in doc_sec_names]
                if valid:
                    targets.setdefault(d, [])
                    for s in valid:
                        if s not in targets[d]:
                            targets[d].append(s)
            for x in mp.get("xml_paths", []):
                if x not in all_xml:
                    all_xml.append(x)
        # If no per-doc targets built (e.g. sections not in any doc), fall back
        if not targets:
            for kw, mp in matches:
                for d in mp.get("docs", []):
                    targets.setdefault(d, [])

        all_docs = list(targets.keys())
        all_sections = list({s for secs in targets.values() for s in secs})
        target_services = self._services_for_sections(all_sections)

        return {
            "user_query": condition,
            "target_medical_doc": all_docs,
            "target_sections": all_sections,
            "targets": targets,
            "target_services": target_services,
            "target_xml_paths": all_xml,
            "confidence": 0.92 if len(matches) > 1 else 0.95,
            "judge_reason": f"关键词匹配「{' + '.join(keywords)}」→ {'; '.join(notes)}",
            "matched_keywords": keywords,
            "source": "keyword_match",
        }

    # ── LLM routing (generalization path) ─────────────────────

    def _route_llm(self, condition: str, kw_result: Optional[dict]) -> dict:
        """Use LLM with hierarchical document catalog to reason about query routing."""
        # Build compact catalog: strip info_type, shorten descriptions
        compact = {}
        for doc, info in self.document_catalog.items():
            compact[doc] = {
                "用途": info["purpose"],
                "章节": {s["name"]: s["purpose"] for s in info["sections"]}
            }
        user_prompt = f"""## 示例（展示推理模式）
1. 查询"术中大出血的患者"
   思路：涉及手术中情况→手术记录。"手术经过"描述操作过程，"术中出现情况及处理"汇总量化指标→互补章节都选。
2. 查询"入院时发热的患者"
   思路：涉及入院症状→入院记录。"主诉"记录自述症状，"体格检查"记录体征→互补章节都选。

## 病历文档及章节
{json.dumps(compact, ensure_ascii=False)}

## 查询
{condition}

请按示例的推理模式，选择合适的文档和章节，输出JSON："""

        try:
            resp = self.client.chat(
                messages=[
                    {"role": "system", "content": ROUTER_SYSTEM},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.1
            )
            parsed = self._parse_json(resp)
            # Ensure required fields + normalize
            # Normalize targets: accept both new {"入院记录":["主诉"]} and old flat lists
            if "targets" in parsed and isinstance(parsed["targets"], dict):
                # New per-document format
                raw_targets = {doc: secs for doc, secs in parsed["targets"].items() if isinstance(secs, list)}
            else:
                # Old flat format → distribute sections to matching docs
                docs = parsed.get("target_medical_doc", kw_result["target_medical_doc"] if kw_result else [])
                secs = parsed.get("target_sections", kw_result["target_sections"] if kw_result else [])
                raw_targets = {doc: list(secs) for doc in docs} if docs else {}
            # Validate route targets. Invalid LLM choices are preserved as
            # diagnostics so the evidence chain explains why fallback happened.
            targets = {}
            invalid_targets = []
            route_warnings = []
            route_repairs = []
            valid_docs = set(self.document_catalog.keys())
            for doc, secs in raw_targets.items():
                normalized_doc, doc_repair = _normalize_catalog_name(str(doc), valid_docs)
                if not normalized_doc:
                    item = {"doc": doc, "sections": list(secs or []), "reason": "未知文档"}
                    invalid_targets.append(item)
                    warning = f"LLM返回无效文档名「{doc}」，未用于查库"
                    route_warnings.append(warning)
                    print(f"[路由] ⚠️ {warning}，将保留诊断并尝试兜底", flush=True)
                    continue
                if doc_repair:
                    route_repairs.append({"from": str(doc), "to": normalized_doc, "type": "document"})
                    print(f"[路由] ℹ️ 文档名已归一化：{doc_repair}", flush=True)
                known = {s["name"] for s in self.document_catalog.get(normalized_doc, {}).get("sections", [])}
                valid = []
                unknown = []
                for sec in secs or []:
                    normalized_sec, sec_repair = _normalize_catalog_name(str(sec), known)
                    if normalized_sec:
                        valid.append(normalized_sec)
                        if sec_repair:
                            route_repairs.append({
                                "doc": normalized_doc,
                                "from": str(sec),
                                "to": normalized_sec,
                                "type": "section",
                            })
                            print(f"[路由] ℹ️ 章节名已归一化：{normalized_doc} {sec_repair}", flush=True)
                    else:
                        unknown.append(sec)
                valid = list(dict.fromkeys(valid))
                if unknown and len(secs or []) > 0:
                    invalid_targets.append({"doc": normalized_doc, "raw_doc": doc, "sections": unknown, "reason": "未知章节"})
                    warning = f"{normalized_doc}: LLM返回无效章节{unknown}，未用于查库"
                    route_warnings.append(warning)
                    print(f"[路由] ⚠️ {warning}。可用章节: {known}", flush=True)
                if valid:
                    targets[normalized_doc] = valid
            if not targets and kw_result:
                fallback = dict(kw_result)
                fallback["source"] = f"{fallback.get('source', 'keyword')}+llm_invalid_route_fallback"
                fallback["llm_invalid_targets"] = invalid_targets
                fallback["route_warnings"] = route_warnings
                fallback["route_repairs"] = route_repairs
                fallback["raw_response"] = resp
                fallback["judge_reason"] = (
                    (fallback.get("judge_reason") or "")
                    + ("；" if fallback.get("judge_reason") and route_warnings else "")
                    + "；".join(route_warnings)
                )[:300]
                return fallback
            parsed["targets"] = targets
            parsed["llm_invalid_targets"] = invalid_targets
            parsed["route_warnings"] = route_warnings
            parsed["route_repairs"] = route_repairs

            # Backward compat: derive flat lists from targets
            parsed["target_medical_doc"] = list(targets.keys())
            parsed["target_sections"] = list({s for secs in targets.values() for s in secs})
            parsed.setdefault("target_xml_paths", kw_result["target_xml_paths"] if kw_result else [])
            parsed.setdefault("match_reason", kw_result.get("match_reason", {}) if kw_result else {})
            parsed.setdefault("confidence", 0.7)
            parsed.setdefault("user_query", condition)
            if isinstance(parsed.get("match_reason"), dict):
                parsed["judge_reason"] = "；".join(f"{k}:{v}" for k,v in parsed["match_reason"].items())[:100]
            if not targets and route_warnings:
                parsed["confidence"] = 0
                parsed["judge_reason"] = "LLM路由未给出有效文档/章节：" + "；".join(route_warnings)
            elif route_warnings:
                parsed["judge_reason"] = (
                    str(parsed.get("judge_reason") or "")
                    + "；路由诊断：" + "；".join(route_warnings)
                )[:300]
            parsed["source"] = "llm"
            parsed["raw_response"] = resp
            return parsed
        except Exception as e:
            # Fallback to keyword result or empty
            if kw_result:
                return kw_result
            return {
                "user_query": condition,
                "target_medical_doc": [],
                "target_sections": [],
                "target_xml_paths": [],
                "confidence": 0,
                "judge_reason": f"路由失败: {str(e)[:60]}",
                "source": "fallback",
            }

    # ── Main entry point ─────────────────────────────────────

    def route(self, condition: str) -> dict:
        """Route a query to target documents and sections.

        For compound queries (X 并且/且/和 Y), split and merge.
        Keyword-first for known patterns, LLM fallback for the rest.
        """
        # ── Decompose compound queries ──────────────────────
        sub_queries = self._split_compound(condition)
        if len(sub_queries) > 1:
            merged = None
            for sq in sub_queries:
                r = self._route_single(sq)
                if merged is None:
                    merged = r
                    merged.setdefault("match_reason", {})
                else:
                    # Merge targets (per-document sections)
                    for doc, secs in r.get("targets", {}).items():
                        if doc not in merged.setdefault("targets", {}):
                            merged["targets"][doc] = []
                        for s in secs:
                            if s not in merged["targets"][doc]:
                                merged["targets"][doc].append(s)
                    # Backward compat flat lists
                    for d in r.get("target_medical_doc", []):
                        if d not in merged.setdefault("target_medical_doc", []):
                            merged["target_medical_doc"].append(d)
                    for s in r.get("target_sections", []):
                        if s not in merged.setdefault("target_sections", []):
                            merged["target_sections"].append(s)
                    match_reason = r.get("match_reason", {})
                    if isinstance(match_reason, dict):
                        merged["match_reason"].update(match_reason)
                    elif match_reason:
                        merged["match_reason"][sq] = str(match_reason)[:120]
                    merged["confidence"] = min(merged["confidence"], r.get("confidence", 0.9))
            merged["judge_reason"] = "；".join(f"{k}:{v}" for k,v in merged.get("match_reason",{}).items())[:120]
            merged["source"] = "compound"
            merged["sub_queries"] = sub_queries
            return merged

        return self._route_single(condition)

    def _split_compound(self, condition: str) -> list:
        """Use LLM to detect and split compound queries.

        Splits on AND (并且/且/和/与) and OR (或者/或/还是) keywords.
        Mixed AND+OR without parentheses is treated as a single condition.
        """
        # Quick pre-check: if it's very short, don't bother
        if len(condition) < 8:
            return [condition]

        # Regex fallback for obvious conjunctions (fast, no LLM needed)
        import re
        _and_kw = r'并且|且|和|与'
        _or_kw = r'或者|或|还是'

        # Detect which type of conjunction is present
        has_and = bool(re.search(_and_kw, condition))
        has_or = bool(re.search(_or_kw, condition))

        # Mixed AND+OR without parentheses → bail, can't resolve precedence
        if has_and and has_or:
            return [condition]

        # Split on AND keywords
        if has_and:
            parts = re.split(rf'(?:{_and_kw})[ ]*', condition)
            parts = [p.strip() for p in parts if len(p.strip()) >= 2]
            if len(parts) > 1:
                return parts

        # Split on OR keywords
        if has_or:
            parts = re.split(rf'(?:{_or_kw})[ ]*', condition)
            parts = [p.strip() for p in parts if len(p.strip()) >= 2]
            if len(parts) > 1:
                return parts

        # Implicit compound: "住院少于5天背痛" → split at unit+keyword boundary
        # Pattern: ...数字+单位 后面紧接中文关键词 → 两个独立条件
        # ⚠️ 单位前必须有数字(\d)，避免误切"住院天数"中的"天"（"天数"是词不是单位）
        implicit_parts = re.split(r'(?<=\d[天岁个次分度%月年])\s*(?=[一-鿿㐀-䶿]{2,})', condition)
        # Filter out generic suffixes (的患者, 的病人, 的情况 etc.)
        _generic_suffix = re.compile(r'^(的|患者|病人|情况|记录|数据|信息)')
        implicit_parts = [p.strip() for p in implicit_parts
                          if len(p.strip()) >= 3 and not _generic_suffix.match(p.strip())]
        # Don't split if first part looks like a temporal reference (not a standalone condition)
        # Structural check: has number+unit but no comparison operator → probably temporal anchor
        # e.g. "术后3天开了葡萄糖" → "术后3天" has number+unit but can't stand alone
        # e.g. "住院少于5天背痛" → "住院少于5天" has comparison, CAN stand alone → split is ok
        if len(implicit_parts) > 1:
            _has_time_unit = bool(re.search(r'\d+\s*(小时|天|日|分钟|周|月)', implicit_parts[0]))
            _has_comparison = bool(re.search(r'(少于|大于|小于|超过|不低于|不高于|等于|以上|以下)', implicit_parts[0]))
            if not (_has_time_unit and not _has_comparison):
                return implicit_parts

        # Short queries without explicit or implicit conjunctions → not compound
        _all_kw = ["并且","且","和","与","或者","或","还是",",","，"]
        if len(condition) <= 18 and not any(kw in condition for kw in _all_kw):
            return [condition]

        # Let LLM decide for longer queries
        try:
            prompt = f"""判断以下查询是否包含多个独立筛选条件。是则拆分为子条件列表。

查询：{condition}

规则：
- "住院小于5天"、"血糖大于7" → 单一条件 → ["无"]
- "住院小于5天并且背痛" → ["住院小于5天", "背痛"]（AND拆分）
- "烧伤或者烫伤" → ["烧伤", "烫伤"]（OR拆分）
- "糖尿病患者"、"有手术记录" → ["无"]
- AND和OR混用不拆 → ["无"]
只输出JSON数组："""

            resp = self.client.chat(
                messages=[{"role":"user","content":prompt}],
                temperature=0.1
            )
            cleaned = resp.strip()
            for fence in ("```json","```"):
                if fence in cleaned:
                    p = cleaned.split(fence)
                    if len(p)>=2: cleaned = p[1].split("```")[0] if "```" in p[1] else p[1]; cleaned = cleaned.strip(); break
            result = json.loads(cleaned)
            if isinstance(result, list) and len(result) > 1 and result[0] != "无":
                parts = [p.strip() for p in result if len(p.strip()) >= 2]
                if len(parts) > 1:
                    return parts
        except Exception:
            pass

        # Time prefix fallback (regex, fast)
        implicit = re.match(r'(.+?)(之前|之后|以内|以上|以下)(.{4,})', condition)
        if implicit:
            p1 = implicit.group(1).strip()
            p3 = implicit.group(3).strip()
            p2 = re.sub(r'^(之前|之后|以内|以上|以下)', '', p3).strip()
            if len(p1) >= 3 and len(p2) >= 3:
                return [p1, p2]

        return [condition]

    def _extract_concepts(self, condition: str) -> list:
        """LLM extracts core concepts from query. Simple task, 3B handles reliably.

        Returns list of concept strings, e.g. "术中没有输血的患者" → ["手术", "输血"]
        """
        prompt = f"""从查询中提取核心概念词，只输出JSON数组。

查询：{condition}

只输出JSON数组："""

        import json as _json, re as _re
        try:
            client = OllamaClient(model=self.model, timeout=15,
                                 num_predict=80, format_json=False)
            resp = client.chat([{"role": "user", "content": prompt}], temperature=0.1)
            _router_debug(f"[路由] 概念提取原始响应: {resp[:150]}")

            # Strip markdown fences
            for fence in ("```json", "```"):
                if fence in resp:
                    parts = resp.split(fence)
                    if len(parts) >= 2:
                        resp = parts[1].split("```")[0] if "```" in parts[1] else parts[1]
                        resp = resp.strip()
                        break

            parsed = parse_llm_json(resp, context=f"概念提取:{condition[:30]}")
            raw_concepts = []
            if isinstance(parsed, list):
                raw_concepts = [str(c).strip() for c in parsed if str(c).strip()]
            elif isinstance(parsed, dict):
                for k, v in parsed.items():
                    for item in ([k] if k not in ("true", "false", "null") else []):
                        raw_concepts.append(str(item).strip())
                    if isinstance(v, list):
                        raw_concepts.extend([str(x).strip() for x in v if str(x).strip()])
                    elif isinstance(v, str) and v.strip():
                        raw_concepts.append(v.strip())

            # Clean 3B garbled output
            concepts = []
            for c in raw_concepts:
                # Flatten stringified inner arrays: "['手术']" → "手术"
                if (c.startswith('[') and c.endswith(']')):
                    try:
                        inner = _json.loads(c.replace("'", '"'))
                        if isinstance(inner, list):
                            for item in inner:
                                item_s = str(item).strip()
                                if item_s and len(item_s) >= 2:
                                    concepts.append(item_s)
                            continue
                    except Exception:
                        pass
                if len(c) >= 2 and c not in ("true", "false", "null", "[]", "{}"):
                    concepts.append(c)

            if concepts:
                _router_debug(f"[路由] LLM概念: {condition[:30]} → {concepts}")
                return concepts
        except Exception as e:
            _router_debug(f"[路由] 概念提取失败: {e}")
        return []

    def _match_services(self, concepts: list, condition: str = "") -> list:
        """Match concepts to services. Reads SKILL.md metadata — source of truth.

        Primary: deterministic — concept appears in service description/triggers.
        Fallback: LLM semantic match (only when deterministic result is ambiguous).
        No hardcoded rules. Metadata IS the rule.
        """
        if not concepts:
            return []

        try:
            from microharness.services.service_catalog import load_services
            services = load_services()
        except Exception:
            return []

        valid_ids = []
        svc_meta = {}  # sid → concatenated metadata text
        for sid, svc in services.items():
            if sid == "base_url" or not isinstance(svc, dict) or not svc.get("url"):
                continue
            meta_text = svc.get("description", "") + " " + " ".join(svc.get("triggers", [])) + " " + svc.get("returns", "")
            svc_meta[sid] = meta_text
            valid_ids.append(sid)

        if not valid_ids:
            return []

        # ── Deterministic: concept ∈ service metadata ──
        # Only use 3+ char concepts for service matching. Short concepts (1-2 chars
        # like "手术", "天") are inherently ambiguous across all domains — they
        # appear as substrings everywhere. This is a general NLP principle, not
        # medical knowledge: shorter tokens → lower discriminative power.
        _svc_concepts = [c for c in concepts if len(c) >= 3]
        scored = []  # [(sid, hit_count)]
        for sid in valid_ids:
            meta = svc_meta[sid]
            hits = sum(1 for c in _svc_concepts if c in meta)
            if hits > 0:
                scored.append((sid, hits))

        if len(scored) == 1:
            print(f"[路由] 服务元数据命中(唯一): concepts={_svc_concepts} → {scored[0][0]}", flush=True)
            return [scored[0][0]]

        if len(scored) > 1:
            print(f"[路由] 服务元数据命中(模糊{len(scored)}): {[(s[0],s[1]) for s in scored]}, LLM裁决", flush=True)
        else:
            print(f"[路由] 服务元数据命中(无): concepts={_svc_concepts}, LLM判断", flush=True)

        # ── LLM fallback ──
        svc_lines = []
        for sid in valid_ids:
            svc = services.get(sid, {})
            desc = svc.get("description", "")
            triggers = "、".join(svc.get("triggers", [])[:10])
            svc_lines.append(f"[{sid}] {desc}  触发词: {triggers}")
        svc_menu = "\n".join(svc_lines)
        concepts_str = "、".join(concepts)

        prompt = f"""可用服务：
{svc_menu}

查询概念：{concepts_str}
原始查询：{condition}

根据每个服务的描述，判断哪些服务能查到这些概念对应的数据。只输出服务ID的JSON数组："""

        import re as _svc_re
        try:
            client = OllamaClient(model=self.model, timeout=15, num_predict=100, format_json=False)
            resp = client.chat([{"role": "user", "content": prompt}], temperature=0.0)
            _router_debug(f"[路由] 服务LLM匹配响应 ({len(resp)}字): {resp[:200]}")
            result = parse_llm_json(resp, context=f"服务匹配:{concepts_str}")
            if not isinstance(result, list):
                arr_m = _svc_re.search(r'\[([^\]]+)\]', resp)
                if arr_m:
                    result = [s.strip().strip('"').strip("'") for s in arr_m.group(1).split(',') if s.strip()]
                else:
                    for vid in valid_ids:
                        if vid in resp:
                            result = [vid]
                            break
            if isinstance(result, list):
                matched = [s for s in result if s in valid_ids]
                if matched:
                    print(f"[路由] 服务LLM匹配: concepts={concepts} → {matched}", flush=True)
                else:
                    print(f"[路由] 服务LLM匹配: concepts={concepts} → 无匹配", flush=True)
                return matched
        except Exception as e:
            print(f"[路由] 服务匹配LLM失败: {e}", flush=True)

        return []

    def _match_catalog(self, condition: str) -> Optional[dict]:
        """Match query against catalog.

        1. LLM extracts core concepts (simple task, 1 call)
        2. Concepts → catalog section purposes (deterministic substring match)
        3. Concepts → services via metadata lookup + LLM fallback

        LLM does semantic understanding. Code does deterministic lookup.
        No hardcoded domain words in code.
        """
        import re as _cre

        # Step 1: LLM extracts concepts
        concepts = self._extract_concepts(condition)
        if not concepts:
            _clean = _cre.sub(r'^(不存在|没有|无|非)\s*', '', condition)
            _clean = _cre.sub(r'(的患者|的病人|的情况|的记录|的$)', '', _clean).strip()
            concepts = [_clean]

        # Step 2: Match concepts against catalog (deterministic)
        best_doc = None
        best_sections = []
        best_doc_score = 0

        for doc_name, doc_info in self.document_catalog.items():
            doc_sections = []
            doc_total = 0

            doc_purpose = doc_info.get("purpose", "")
            doc_hits = sum(1 for c in concepts if c in doc_purpose)
            if doc_hits == 0:
                continue

            for sec in doc_info.get("sections", []):
                sec_name = sec.get("name", "")
                sec_purpose = sec.get("purpose", "")
                if not sec_purpose:
                    continue
                hits = sum(1 for c in concepts if c in sec_purpose)
                if hits > 0:
                    doc_sections.append((sec_name, hits))
                    doc_total += hits

            if doc_sections:
                doc_total += doc_hits
                if doc_total > best_doc_score:
                    best_doc = doc_name
                    best_doc_score = doc_total
                    best_sections = doc_sections

        # Step 3: Match concepts to services (deterministic + LLM fallback).
        # Service-only routes are valid: structured services can be the primary
        # evidence even when the document catalog has no phrase-level hit.
        target_services = self._match_services(concepts, condition)

        if not best_doc or not best_sections:
            if target_services:
                evidence_targets = _diagnosis_evidence_targets(self.document_catalog) if "diagnosis-query" in target_services else {}
                flat_sections = list(dict.fromkeys(
                    section for sections in evidence_targets.values() for section in sections
                ))
                print(f"[路由] 概念匹配: concepts={concepts} → 服务{target_services}", flush=True)
                return {
                    "user_query": condition,
                    "targets": evidence_targets,
                    "target_medical_doc": list(evidence_targets.keys()),
                    "target_sections": flat_sections,
                    "target_xml_paths": [],
                    "target_services": target_services,
                    "confidence": 0.85,
                    "match_reason": {
                        **{svc: "概念命中服务目录" for svc in target_services},
                        **{
                            f"{doc}.{section}": "目录角色提示为疾病/症状证据章节"
                            for doc, sections in evidence_targets.items()
                            for section in sections
                        },
                    },
                    "judge_reason": "；".join(f"{svc}:概念命中服务目录" for svc in target_services)[:120],
                    "source": "concept_service_match",
                }
            return None

        best_sections.sort(key=lambda x: x[1], reverse=True)
        targets = {best_doc: [s[0] for s in best_sections]}

        simple_reason = {}
        for s in best_sections:
            sec_purpose = ""
            for sec in self.document_catalog.get(best_doc, {}).get("sections", []):
                if sec["name"] == s[0]:
                    sec_purpose = sec.get("purpose", "")
                    break
            matched_c = [c for c in concepts if c in sec_purpose]
            simple_reason[f"{best_doc}.{s[0]}"] = f"概念{matched_c}命中"

        if "lab-results" in target_services:
            print(
                f"[路由] 概念匹配: concepts={concepts} → 主证据服务 lab-results"
                f"（文档候选{best_doc}{[s[0] for s in best_sections]}已降级为辅助）",
                flush=True,
            )
        else:
            print(f"[路由] 概念匹配: concepts={concepts} → {best_doc}{[s[0] for s in best_sections]}", flush=True)

        return {
            "user_query": condition,
            "targets": targets,
            "target_medical_doc": [best_doc],
            "target_sections": [s[0] for s in best_sections],
            "target_xml_paths": [],
            "target_services": target_services,
            "confidence": 0.9,
            "match_reason": simple_reason,
            "judge_reason": "；".join(f"{k}:{v}" for k, v in simple_reason.items())[:120],
            "source": "concept_match",
        }

    def _route_single(self, condition: str) -> dict:
        """Route a single (non-compound) query.

        Priority: keyword map → LLM概念+catalog匹配 → LLM全路由兜底.
        LLM 只做语义理解（提取概念），不定章节选择（代码做匹配）。
        """
        # Fast path 1: keyword matching (disease→section map)
        kw_result = self.match_keywords(condition)
        if kw_result and kw_result["confidence"] >= 0.9:
            return kw_result

        # Fast path 2: catalog character-overlap matching (deterministic, 0ms)
        catalog_result = self._match_catalog(condition)
        if catalog_result:
            return catalog_result

        # LLM fallback: only when catalog has zero overlap (novel expression)
        result = self._route_llm(condition, kw_result)
        if result.get("confidence", 0) >= 0.5:
            return result

        if kw_result:
            kw_result["source"] = "keyword_fallback"
            return kw_result
        return result

    # ── Helpers ──────────────────────────────────────────────

    def _parse_json(self, text: str) -> dict:
        """Robust JSON extraction from LLM response. Handles single-quoted JSON."""
        cleaned = text.strip()
        for fence in ("```json", "```"):
            if fence in cleaned:
                parts = cleaned.split(fence)
                if len(parts) >= 2:
                    inner = parts[1].split("```")[0] if "```" in parts[1] else parts[1]
                    cleaned = inner.strip()
                    break
        # ── Try standard JSON ──
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        # ── Try { } block extraction ──
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            block = cleaned[start:end+1]
            try:
                return json.loads(block)
            except json.JSONDecodeError:
                pass
            # ── Fix single-quoted JSON (LLMs often output Python-style dicts) ──
            try:
                import re as _pjre
                # Replace single quotes around keys: 'key': → "key":
                fixed = _pjre.sub(r"'([^']*)'(?=\s*:)", r'"\1"', block)
                # Replace single quotes around simple string values: : 'value' → : "value"
                fixed = _pjre.sub(r":\s*'([^']*)'", r': "\1"', fixed)
                return json.loads(fixed)
            except (json.JSONDecodeError, ValueError):
                pass
            # ── Try ast.literal_eval for Python dict literals ──
            try:
                import ast as _past
                result = _past.literal_eval(block)
                if isinstance(result, dict):
                    return result
            except (ValueError, SyntaxError):
                pass
        raise json.JSONDecodeError("All JSON parsing strategies failed", cleaned, 0)


# ═══════════════════════════════════════════════════════════════
# LangChain Tool (optional — for Agent integration)
# ═══════════════════════════════════════════════════════════════

def create_router_tool():
    """Create a LangChain-compatible tool from the router."""
    try:
        from langchain_core.tools import tool

        @tool("medical-disease-section-router")
        def medical_disease_section_router(user_query: str) -> dict:
            """
            一体化病历路由工具：输入疾病/症状/人群查询，
            返回需要调取的病历文档类型 + 文档对应的章节 + XML字段路径。

            Args:
                user_query: 自然语言，如"找出所有糖尿病患者"
            Returns:
                JSON: {target_medical_doc, target_sections, target_xml_paths, confidence, judge_reason}
            """
            router = QueryRouter()
            return router.route(user_query)

        return medical_disease_section_router
    except ImportError:
        return None


# ═══════════════════════════════════════════════════════════════
# Convenience
# ═══════════════════════════════════════════════════════════════

_router: Optional[QueryRouter] = None

def get_router() -> QueryRouter:
    global _router
    if _router is None:
        _router = QueryRouter()
    return _router
