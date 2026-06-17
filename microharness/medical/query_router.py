"""
Medical Disease-Section Router
==============================
Lightweight routing skill: disease query → target documents + sections.

Combines:
- Hardcoded disease→document→section mapping (reliable, fast)
- LLM generalization (handles unmapped queries via few-shot reasoning)
- Integrated with XML field catalog for precise field path resolution

Usable as:
1. Direct Python call:  router.route("糖尿病患者")
2. LangChain tool:    medical_disease_section_router("找出糖尿病患者")
"""

import json
import re
from typing import Optional, Dict, List

from microharness.medical.field_catalog import get_catalog, FILENAME_TO_TEMPLATE
from microharness.ollama import OllamaClient

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
        "purpose": "患者入院时建立的首份完整病历，记录患者基本信息、本次发病情况、既往病史、入院查体和初步诊断",
        "used_for": ["初次就诊记录", "症状录入", "病史采集", "入院评估", "基础查体"],
        "sections": [
            {"name": "主诉", "purpose": "患者本次就诊最核心的主观症状和持续时间，如'头痛3天''发热伴咳嗽1周'", "info_type": "主观症状"},
            {"name": "现病史", "purpose": "本次发病全过程：起病时间、诱因、症状演变、伴随症状、外院诊疗经过", "info_type": "发病经过"},
            {"name": "既往史", "purpose": "既往疾病史、手术史、慢性病史（如糖尿病/高血压/乙肝）、过敏史", "info_type": "既往病史"},
            {"name": "个人史", "purpose": "吸烟史、饮酒史、职业暴露、居住地、疫区接触史", "info_type": "生活习惯"},
            {"name": "婚育史", "purpose": "婚姻状况、生育次数和方式（顺产/剖宫产）", "info_type": "婚姻生育"},
            {"name": "月经史", "purpose": "女性月经初潮年龄、周期、经量、痛经情况", "info_type": "女性生理"},
            {"name": "家族史", "purpose": "直系亲属遗传病史、家族聚集性疾病", "info_type": "家族遗传"},
            {"name": "体格检查", "purpose": "体温/脉搏/呼吸/血压等生命体征，各系统查体发现（心肺听诊/腹部触诊/神经查体）", "info_type": "客观体征"},
            {"name": "专科情况", "purpose": "专科查体发现：骨科脊柱四肢、神经科病理征、眼科眼底等", "info_type": "专科查体"},
            {"name": "辅助检查", "purpose": "入院前完成的实验室检查（血常规/生化/血糖/糖化）和影像学检查（CT/MR/X线/超声/心电图）结果", "info_type": "检查检验"},
            {"name": "初步诊断", "purpose": "入院时的初步诊断列表，列出所有怀疑的疾病", "info_type": "初步诊断"},
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
        "purpose": "手术过程记录文档，记录手术名称、方式、麻醉、术中发现和手术经过",
        "used_for": ["手术查询", "术式确认", "麻醉记录", "术中情况"],
        "sections": [
            {"name": "手术名称", "purpose": "本次手术的具体术式名称，如阑尾切除术、冠脉搭桥术、剖宫产术", "info_type": "手术术式"},
            {"name": "麻醉方法", "purpose": "所用麻醉方式：全麻/椎管内/局麻等", "info_type": "麻醉方式"},
            {"name": "手术日期", "purpose": "手术日期", "info_type": "时间节点"},
            {"name": "术前诊断", "purpose": "手术前确认的诊断", "info_type": "术前诊断"},
            {"name": "术中诊断", "purpose": "手术过程中新发现的诊断", "info_type": "术中诊断"},
            {"name": "手术经过", "purpose": "手术过程的详细描述：操作步骤、切除范围、植入物", "info_type": "手术过程"},
            {"name": "术中出现情况及处理", "purpose": "术中意外（出血/血压波动/过敏等）及处置", "info_type": "术中并发症"},
        ]
    },
}

# Backward-compat lookup
SECTION_PURPOSE_LOOKUP = {}
for doc_name, doc_info in DOCUMENT_CATALOG.items():
    for s in doc_info.get("sections", []):
        SECTION_PURPOSE_LOOKUP[s["name"]] = s

# Load persisted config if exists (allows user edits via UI)
try:
    from pathlib import Path as _Path
    _config_path = _Path(__file__).parent.parent.parent / "configs" / "medical_catalog.json"
    if _config_path.exists():
        import json as _json
        _saved = _json.loads(_config_path.read_text(encoding="utf-8"))
        if isinstance(_saved, dict) and len(_saved) > 0:
            DOCUMENT_CATALOG = _saved
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════
# Disease → Document → Section Mapping (fast keyword path)
# ═══════════════════════════════════════════════════════════════

DISEASE_SECTION_MAP: Dict[str, dict] = {
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

# ═══════════════════════════════════════════════════════════════
# Router System Prompt
# ═══════════════════════════════════════════════════════════════

ROUTER_SYSTEM = """你是病历查询路由专家。两步推理：1)判断查询需要哪种病历文档 2)在该文档中定位相关章节。

## 推理步骤
1. 分析用户查询的核心意图：症状？诊断？手术？检查？时间数值？生活习惯？
2. 对比各类病历文档的整体用途（purpose字段），选出最匹配的文档类型
3. 在选中文档内，逐条匹配各章节的临床用途，选出相关章节
4. 按相关度排序，输出 JSON

## 输出格式
{
  "user_query": "原始问句",
  "target_medical_doc": ["入院记录", "出院记录"],
  "target_sections": ["主诉", "出院诊断"],
  "confidence": 0.95,
  "match_reason": {"主诉":"记录核心主观不适", "出院诊断":"记录最终确诊疾病"}
}
"""


# ═══════════════════════════════════════════════════════════════
# QueryRouter
# ═══════════════════════════════════════════════════════════════

class QueryRouter:
    """Disease-aware medical query router."""

    def __init__(self, model: str = "qwen2.5:3b", timeout: int = 60):
        self.model = model
        self.timeout = timeout
        self.catalog = get_catalog()
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = OllamaClient(model=self.model, timeout=self.timeout)
        return self._client

    # ── Keyword matching (fast path, no LLM) ──────────────────

    def match_keywords(self, condition: str) -> Optional[dict]:
        """Check if condition matches any known disease in the map.
        For multi-keyword queries, merge all matching entries."""
        matches = []
        for keyword, mapping in DISEASE_SECTION_MAP.items():
            if keyword in condition:
                matches.append((keyword, mapping))

        if not matches:
            return None

        # Merge all matches
        all_docs = []
        all_sections = []
        all_xml = []
        keywords = []
        notes = []
        for kw, mp in matches:
            keywords.append(kw)
            notes.append(mp.get("note", ""))
            for d in mp["docs"]:
                if d not in all_docs:
                    all_docs.append(d)
            for s in mp["sections"]:
                if s not in all_sections:
                    all_sections.append(s)
            for x in mp["xml_paths"]:
                if x not in all_xml:
                    all_xml.append(x)

        return {
            "user_query": condition,
            "target_medical_doc": all_docs,
            "target_sections": all_sections,
            "target_xml_paths": all_xml,
            "confidence": 0.92 if len(matches) > 1 else 0.95,
            "judge_reason": f"关键词匹配「{' + '.join(keywords)}」→ {'; '.join(notes)}",
            "matched_keywords": keywords,
            "source": "keyword_match",
        }

    # ── LLM routing (generalization path) ─────────────────────

    def _route_llm(self, condition: str, kw_result: Optional[dict]) -> dict:
        """Use LLM with hierarchical document catalog to reason about query routing."""
        user_prompt = f"""## 病历文档类型及章节元数据
{json.dumps(DOCUMENT_CATALOG, ensure_ascii=False, indent=2)}

## 推理示例
用户问题：找出头痛患者
意图：主观急性症状 → 需要记录症状和诊断的文档
第一步-选文档：
  - 入院记录（用途：症状录入、病史采集、入院评估）→ 匹配
  - 出院记录（用途：最终诊断、出院总结）→ 匹配（确诊疾病在此）
  - 门急诊病历（用途：门诊就诊、初次接诊）→ 中度匹配
第二步-选章节：
  - 入院记录.主诉：记录核心主观症状 → 高度匹配
  - 入院记录.现病史：记录发病全过程 → 高度匹配
  - 入院记录.体格检查：记录相关神经查体 → 中度匹配
  - 出院记录.出院诊断：记录确诊疾病 → 高度匹配
输出：
{{"user_query":"找出头痛患者","target_medical_doc":["入院记录","出院记录"],"target_sections":["主诉","现病史","体格检查","出院诊断"],"confidence":0.95,"match_reason":{{"主诉":"记录核心主观不适","现病史":"记录发病过程","体格检查":"记录神经查体","出院诊断":"记录确诊疾病"}}}}

用户问题：{condition}
请按两步推理分析，输出JSON："""

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
            parsed.setdefault("target_medical_doc", kw_result["target_medical_doc"] if kw_result else [])
            parsed.setdefault("target_sections", kw_result["target_sections"] if kw_result else [])
            parsed.setdefault("target_xml_paths", kw_result["target_xml_paths"] if kw_result else [])
            parsed.setdefault("match_reason", kw_result.get("match_reason", {}) if kw_result else {})
            parsed.setdefault("confidence", 0.7)
            parsed.setdefault("user_query", condition)
            # Flatten match_reason for backward compat
            if isinstance(parsed.get("match_reason"), dict):
                parsed["judge_reason"] = "；".join(f"{k}:{v}" for k,v in parsed["match_reason"].items())[:100]
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
                    # Merge: union docs and sections
                    for d in r.get("target_medical_doc", []):
                        if d not in merged["target_medical_doc"]:
                            merged["target_medical_doc"].append(d)
                    for s in r.get("target_sections", []):
                        if s not in merged["target_sections"]:
                            merged["target_sections"].append(s)
                    for x in r.get("target_xml_paths", []):
                        if x not in merged["target_xml_paths"]:
                            merged["target_xml_paths"].append(x)
                    merged["match_reason"].update(r.get("match_reason", {}))
                    merged["confidence"] = min(merged["confidence"], r.get("confidence", 0.9))
            merged["judge_reason"] = "；".join(f"{k}:{v}" for k,v in merged.get("match_reason",{}).items())[:120]
            merged["source"] = "compound"
            merged["sub_queries"] = sub_queries
            return merged

        return self._route_single(condition)

    def _split_compound(self, condition: str) -> list:
        """Use LLM to detect and split compound queries."""
        # Quick pre-check: if it's very short, don't bother
        if len(condition) < 8:
            return [condition]

        # Regex fallback for obvious conjunctions (fast, no LLM needed)
        import re
        parts = re.split(r'(?:并且|且|和|与)[ ]*', condition)
        parts = [p.strip() for p in parts if len(p.strip()) >= 2]
        if len(parts) > 1:
            return parts

        # Let LLM decide
        try:
            prompt = f"""判断以下查询是否为复合条件（包含多个独立筛选条件）。如果是，拆分为子条件列表。

查询：{condition}

规则：
- "住院小于5天"、"血糖大于7" → 单一条件，不拆分
- "住院小于5天并且背痛" → 复合条件，拆分为 ["住院小于5天", "背痛"]
- "2024年9月之前发现乳腺肿块" → 复合条件，拆分为 ["2024年9月之前", "发现乳腺肿块"]
- "糖尿病患者"、"有手术记录" → 单一条件
- 只输出JSON数组或["无"]

输出：["子条件1", "子条件2"] 或 ["无"]"""

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

    def _route_single(self, condition: str) -> dict:
        """Route a single (non-compound) query."""
        # Fast path: keyword matching
        kw_result = self.match_keywords(condition)
        if kw_result and kw_result["confidence"] >= 0.9:
            return kw_result

        # LLM path: semantic reasoning with DOCUMENT_CATALOG
        result = self._route_llm(condition, kw_result)
        if result.get("confidence", 0) >= 0.5:
            return result

        # Last resort
        if kw_result:
            kw_result["source"] = "keyword_fallback"
            return kw_result
        return result

    # ── Helpers ──────────────────────────────────────────────

    def _parse_json(self, text: str) -> dict:
        """Robust JSON extraction from LLM response."""
        cleaned = text.strip()
        for fence in ("```json", "```"):
            if fence in cleaned:
                parts = cleaned.split(fence)
                if len(parts) >= 2:
                    inner = parts[1].split("```")[0] if "```" in parts[1] else parts[1]
                    cleaned = inner.strip()
                    break
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Try extracting just the { } block
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start >= 0 and end > start:
                return json.loads(cleaned[start:end+1])
            raise


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
