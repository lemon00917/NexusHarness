"""
Semantic query classes for medical filtering.

Rules here describe reusable query shapes such as inpatient duration and
discharge outcome. They should not contain concrete disease or drug names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .temporal_parser import compare_values, is_duration_comparison, is_numeric_comparison, parse_cn_number


@dataclass
class SemanticClass:
    name: str
    docs: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    keyword: str = ""


DURATION_CLASS = SemanticClass(
    name="住院时长比较",
    docs=["出院记录"],
    sections=["入院日期", "出院日期"],
    services=["encounter-info"],
    keyword="住院天数",
)

OUTCOME_CLASS = SemanticClass(
    name="出院/治疗转归",
    docs=["出院记录"],
    sections=["出院情况", "诊疗经过", "出院诊断"],
    services=["diagnosis-query"],
)

PRE_ADMISSION_CLASS = SemanticClass(
    name="入院前/既往存在",
    docs=["入院记录"],
    sections=["既往史", "现病史", "主诉", "初步诊断", "辅助检查"],
    services=["diagnosis-query"],
)

LAB_RESULT_CLASS = SemanticClass(
    name="检验指标",
    docs=[],
    sections=[],
    services=["lab-results"],
)

DRUG_USE_CLASS = SemanticClass(
    name="用药医嘱",
    docs=[],
    sections=[],
    services=["drug-interaction"],
)

DIAGNOSIS_EXISTENCE_CLASS = SemanticClass(
    name="疾病/症状存在",
    docs=["入院记录", "出院记录", "门急诊病历"],
    sections=["既往史", "现病史", "主诉", "初步诊断", "入院诊断", "出院诊断", "诊断"],
    services=["diagnosis-query"],
)

DIAGNOSIS_LIKE_SECTIONS = {
    "既往史", "现病史", "主诉", "初步诊断", "入院诊断", "出院诊断", "诊断",
    "术前诊断", "术中诊断", "术后诊断",
}

NON_DIAGNOSIS_KEYWORDS = {
    "年龄", "住院天数", "住院时间", "住院时长", "住院日", "性别",
    "用药", "药物", "医嘱", "检验", "化验", "指标", "检查",
}

LAB_OBSERVATION_RE = re.compile(
    r"(检验|化验|指标|结果|参考范围|偏高|偏低|升高|降低|增高|异常|正常|阳性|阴性|"
    r"计数|数值|水平|浓度|大于|小于|高于|低于|不低于|不高于|[<>≤≥=＞＜])",
    re.I,
)

LAB_EXPLICIT_INTENT_RE = re.compile(
    r'(检验|化验|检验指标|化验指标|指标|参考范围|检验结果|化验结果|血常规|生化|肝功|肾功|电解质)',
    re.I,
)

LAB_MEASUREMENT_UNIT_RE = re.compile(
    r'(?:[x×*]\s*10(?:\^)?[³⁶⁹369]?\s*/\s*[Ll]|'
    r'(?:mmol|μmol|umol|mol|g|mg|μg|ug|ng|pg|U|IU)\s*/\s*(?:[Ll]|d[Ll]))',
    re.I,
)

AGE_COMPARISON_RE = re.compile(
    r'(?:^|[^一-龥])(?:年龄\s*)?(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百]+)\s*岁\s*'
    r'(?:以上|以下|及以上|及以下|[<>≤≥=＞＜])|'
    r'年龄\s*(?:大于|小于|高于|低于|超过|不少于|不低于|不超过|至少|至多|[<>≤≥=＞＜])',
    re.I,
)

LAB_GENERIC_TRIGGERS = {
    '检验', '化验', '检验指标', '化验指标', '指标', '结果', '异常', '正常',
    '偏高', '偏低', '升高', '降低', '增高', '减少', '高于', '低于', '大于', '小于',
    '不低于', '不高于', '参考范围', '阳性', '阴性', '计数', '数值', '水平', '浓度',
}

EXPLICIT_DIAGNOSIS_INTENT_RE = re.compile(
    r"(诊断为|确诊为|患有|疾病|病症|综合征|(?:^|[，,；;\s]).+症(?:的患者)?$)"
)

EXPLICIT_MEDICATION_ACTION_RE = re.compile(
    r"(?:开药|开立(?:过)?|开具(?:过)?|下达(?:过)?医嘱|下过医嘱|"
    r"开(?:过|了)(?![^，,。；;]{0,4}(?:手术|刀))|"
    r"服用|服过|吃过|吃了|使用过|用过|给药|注射|输注|停药|停用)"
)

OUTCOME_PATTERNS = [
    r"没有(?:明显)?好转",
    r"未(?:见)?(?:明显)?好转",
    r"无(?:明显)?好转",
    r"不见好",
    r"没有(?:明显)?缓解",
    r"未(?:见)?(?:明显)?缓解",
    r"无(?:明显)?缓解",
    r"未(?:见)?(?:明显)?改善",
    r"无(?:明显)?改善",
    r"没有(?:明显)?改善",
    r"好转",
    r"缓解",
    r"改善",
    r"治愈",
    r"痊愈",
    r"恢复",
    r"加重",
    r"恶化",
    r"进展",
    r"复发",
    r"持续(?:存在)?",
    r"仍(?:然)?(?:存在|有|为)?",
    r"尚未(?:恢复|缓解|好转)",
]


def append_unique(seq: list, values: list) -> list:
    out = list(seq or [])
    seen = set(out)
    for value in values or []:
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out


def extract_outcome_modifiers(condition: str) -> list[str]:
    hits = []
    for pattern in OUTCOME_PATTERNS:
        for match in re.finditer(pattern, condition or ""):
            if match.group(0):
                hits.append((match.start(), match.end(), match.group(0)))

    # Prefer longer phrases first so "没有好转" suppresses nested "好转".
    hits.sort(key=lambda item: (-(item[1] - item[0]), item[0]))
    used = []
    modifiers = []
    for start, end, phrase in hits:
        if any(not (end <= used_start or start >= used_end) for used_start, used_end in used):
            continue
        used.append((start, end))
        if phrase not in modifiers:
            modifiers.append(phrase)
    modifiers.sort(key=lambda phrase: (condition or "").find(phrase))
    return modifiers


def has_outcome_phase(text: str) -> bool:
    return bool(re.search(r"(出院|离院|治疗后|用药后|术后|手术后|住院期间|住院后|复查时)", text or ""))


def is_outcome_state_condition(condition: str, context: str = "") -> bool:
    # Outcome words such as "好转/缓解/改善/未好转" describe a state change.
    # Even without an explicit phase word, the reliable evidence usually lives
    # in discharge/treatment summary rather than admission complaint/history.
    return bool(extract_outcome_modifiers(condition))


def is_pre_admission_condition(condition: str) -> bool:
    return bool(re.search(r"(入院前|入院时已|入院时有|既往有|既往史|病史|原有|既有|(?:有|患有|存在|既往).{2,12}史)", condition or ""))


def should_route_to_diagnosis_service(condition: str, cond: dict) -> bool:
    """Route disease/symptom existence queries to structured diagnoses.

    This is driven by semantic section roles, not by concrete disease names.
    """
    text = str(condition or "")
    keyword = str((cond or {}).get("keyword") or "").strip()
    if not text and not keyword:
        return False
    if any(token in text or token == keyword for token in NON_DIAGNOSIS_KEYWORDS):
        return False
    if (cond or {}).get("is_numeric"):
        return False
    sections = set((cond or {}).get("target_sections") or [])
    if sections & DIAGNOSIS_LIKE_SECTIONS:
        return True
    if re.search(r"(诊断|确诊|患有|存在|得了|有.+病史|症状|疾病|病症)", text):
        return True
    if re.search(r"^(?:有|发现|提示|考虑|符合).{2,}", text) and not re.search(
        r"^(?:有无|有没有|是否|有几个|有多少)", text
    ):
        return True
    return False


def is_lab_result_condition(condition: str, cond: dict | None = None) -> bool:
    text = str(condition or "")
    if not text:
        return False

    cond = cond or {}
    skills = set(cond.get("target_skills") or [])
    entity_type = str(cond.get("entity_type") or "").lower()
    domain = str(cond.get("domain") or "").lower()
    semantic_class = str(cond.get("semantic_class") or "")

    if has_explicit_medication_action(text):
        return False

    # Numeric syntax is shared by demographics, encounter duration, laboratory
    # results and many other domains. These established structural conditions
    # must win even when a small model labels them as laboratory conditions.
    if is_duration_comparison(text) or AGE_COMPARISON_RE.search(text):
        return False
    if entity_type in {"age", "demographic", "encounter"} or domain in {"demographic", "encounter"}:
        return False

    trusted_lab_route = (
        "lab-results" in skills
        or entity_type in {"lab", "laboratory"}
        or domain == "laboratory"
        or "检验" in semantic_class
    )

    observation_intent = bool(LAB_OBSERVATION_RE.search(text))
    explicit_lab_intent = bool(LAB_EXPLICIT_INTENT_RE.search(text))
    explicit_diagnosis_intent = bool(EXPLICIT_DIAGNOSIS_INTENT_RE.search(text))
    lab_concept_match = _service_concept_trigger_match("lab-results", text)
    measurement_value = bool(is_numeric_comparison(text) and LAB_MEASUREMENT_UNIT_RE.search(text))

    # A diagnosis name may contain a laboratory concept, for example a
    # disease ending in “症”. Keep explicit disease-existence queries on the
    # diagnosis service unless the user also supplied an observation/value
    # predicate. A wrong diagnosis-query emitted by the LLM alone must not
    # block deterministic laboratory repair.
    if explicit_diagnosis_intent and not explicit_lab_intent and not measurement_value:
        return False
    if "drug-interaction" in skills and not explicit_lab_intent and not measurement_value:
        return False

    if trusted_lab_route:
        return True
    return bool(
        explicit_lab_intent
        or measurement_value
        or (lab_concept_match and observation_intent)
    )


def _service_metadata(service_id: str) -> dict:
    try:
        from microharness.services.service_catalog import load_services

        svc = load_services().get(service_id, {})
        return svc if isinstance(svc, dict) else {}
    except Exception:
        return {}


def _service_trigger_match(service_id: str, text: str) -> bool:
    svc = _service_metadata(service_id)
    triggers = svc.get("triggers") or []
    return any(str(token) and str(token) in str(text or "") for token in triggers)


def _service_concept_trigger_match(service_id: str, text: str) -> bool:
    """Match entity-like service triggers, excluding generic predicates."""
    metadata = _service_metadata(service_id)
    triggers = metadata.get("triggers") or []
    normalized = str(text or "").lower()
    return any(
        str(token)
        and str(token) not in LAB_GENERIC_TRIGGERS
        and str(token).lower() in normalized
        for token in triggers
    )


def has_explicit_medication_action(condition: str) -> bool:
    """Return whether the text explicitly describes a medication action."""
    return bool(EXPLICIT_MEDICATION_ACTION_RE.search(str(condition or "")))


def is_drug_use_condition(condition: str, cond: dict | None = None) -> bool:
    text = str(condition or "")
    if not text:
        return False
    if has_explicit_medication_action(text):
        return True
    if any(svc in ((cond or {}).get("target_skills") or []) for svc in ("lab-results", "diagnosis-query")):
        return False
    if (cond or {}).get("entity_type") == "drug":
        return True
    return _service_trigger_match("drug-interaction", text) and not is_lab_result_condition(text, cond)


def extract_pre_admission_keyword(condition: str, fallback_keyword_fn=None) -> str:
    text = (condition or "").strip()
    text = re.sub(r"(的患者|的病人|的病例|患者|病人|病例)$", "", text)
    text = re.sub(r"^(入院前|入院时已|入院时有|既往有|既往史|病史|原有|既有)\s*", "", text)
    text = re.sub(r"^(就)?(有|患有|存在|诊断为|确诊为|得过|有过)\s*", "", text)
    text = re.sub(r"(病史|疾病史|既往史|史)$", "", text)
    text = re.sub(r"[\s　的了，,。;；、]+", "", text)
    if len(text) >= 2:
        return text
    return fallback_keyword_fn(condition) if fallback_keyword_fn else text


def extract_outcome_keyword(condition: str, fallback_keyword_fn=None) -> str:
    text = (condition or "").strip()
    text = re.sub(r"(的患者|的病人|的病例|患者|病人|病例)$", "", text)
    text = re.sub(
        r"(出院时|出院的时候|出院后|出院前|离院时|治疗后|用药后|术后|手术后|住院期间|住院后|复查时)",
        "",
        text,
    )
    for pattern in OUTCOME_PATTERNS:
        text = re.sub(pattern, "", text)
    text = re.sub(r"(诊断为|诊断|确诊为|症状|体征|表现|是否|有没有|仍然|仍|存在|有|为|是|未|无|没有)", "", text)
    text = re.sub(r"[\s　的了，,。;；、]+", "", text)
    chunks = [chunk for chunk in re.split(r"[,，;；、\s]+", text) if len(chunk) >= 2]
    if chunks:
        return max(chunks, key=len)
    return fallback_keyword_fn(condition) if fallback_keyword_fn else ""


def judge_outcome_polarity(modifiers: list, text: str) -> Optional[dict]:
    """Deterministic polarity check for outcome/state modifiers."""
    if not modifiers or not text:
        return None
    mod_text = " ".join(modifiers)
    positive_words = ("好转", "改善", "缓解", "减轻", "恢复", "治愈", "痊愈", "无不适", "未见明显不适")
    no_improve_words = (
        "没有好转",
        "没有明显好转",
        "未好转",
        "未见好转",
        "未见明显好转",
        "无好转",
        "无明显好转",
        "不见好",
        "未缓解",
        "未见缓解",
        "未见明显缓解",
        "无缓解",
        "无明显缓解",
        "没有缓解",
        "没有明显缓解",
        "未改善",
        "未见改善",
        "未见明显改善",
        "无改善",
        "无明显改善",
        "没有改善",
        "没有明显改善",
        "仍存在",
        "仍有",
        "持续存在",
        "持续",
        "加重",
        "恶化",
        "进展",
    )
    worsen_words = ("加重", "恶化", "进展")

    wants_no_improve = any(
        word in mod_text
        for word in (
            "没有好转",
            "未好转",
            "无好转",
            "不见好",
            "未缓解",
            "无缓解",
            "没有缓解",
            "未改善",
            "无改善",
            "没有改善",
        )
    )
    wants_positive = any(word in mod_text for word in ("好转", "改善", "缓解", "恢复", "治愈", "痊愈")) and not wants_no_improve
    wants_worse = any(word in mod_text for word in worsen_words)

    if wants_no_improve:
        if any(word in text for word in no_improve_words):
            return {"matched": True, "reason": "转归记录提示未缓解/仍存在，符合没有好转"}
        if any(word in text for word in positive_words):
            return {"matched": False, "reason": "转归记录提示已有好转/改善，不符合没有好转"}
    if wants_positive:
        if any(word in text for word in no_improve_words):
            return {"matched": False, "reason": "转归记录提示未缓解/仍存在，不符合好转"}
        if any(word in text for word in positive_words):
            return {"matched": True, "reason": "转归记录提示好转/改善"}
    if wants_worse:
        if any(word in text for word in worsen_words):
            return {"matched": True, "reason": "转归记录提示加重/恶化"}
        if any(word in text for word in positive_words):
            return {"matched": False, "reason": "转归记录提示好转/改善，不符合加重"}
    return None


def judge_explicit_absence(keyword: str, text: str) -> Optional[dict]:
    """Detect explicit absence statements such as '否认高血压病史'."""
    if not keyword or not text:
        return None
    kw = re.escape(keyword)
    patterns = [
        rf"否认[^。；;\n]{{0,12}}{kw}[^。；;\n]{{0,8}}(病史|史|疾病)?",
        rf"(无|未见|没有)[^。；;\n]{{0,8}}{kw}[^。；;\n]{{0,8}}(病史|史|疾病)?",
        rf"{kw}[^。；;\n]{{0,8}}(阴性|否认)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return {"matched": False, "reason": f"原文明确否认{keyword}相关病史"}
    return None


def judge_history_duration(keyword: str, modifiers: list, text: str) -> Optional[dict]:
    """Deterministically judge disease-history duration requirements.

    Handles grammar such as "高血压病史>=10年" against free-text evidence like
    "高血压病史10余年" or "既往患高血压超过10年". This is not tied to a
    specific disease name; the caller supplies the extracted keyword.
    """
    if not keyword or not text or not modifiers:
        return None

    requirement = ""
    for mod in modifiers:
        if "病史" in str(mod) and "年" in str(mod):
            requirement = str(mod)
            break
    if not requirement:
        return None

    req_match = re.search(
        r"病史\s*(>=|<=|>|<|=|≥|≤|以上|以下|不少于|不低于|至少|不超过|至多)?\s*"
        r"(\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)\s*年",
        requirement,
    )
    if not req_match:
        return None

    operator = req_match.group(1) or ">="
    operator = {
        "≥": ">=",
        "≤": "<=",
        "以上": ">=",
        "不少于": ">=",
        "不低于": ">=",
        "至少": ">=",
        "以下": "<=",
        "不超过": "<=",
        "至多": "<=",
    }.get(operator, operator)
    threshold = parse_cn_number(req_match.group(2))
    if threshold is None:
        return None

    absence = judge_explicit_absence(keyword, text)
    if absence is not None:
        return absence

    sentences = [
        s.strip()
        for s in re.split(r"[。；;\n\r]+", text)
        if keyword in s and any(tag in s for tag in ("病史", "既往", "患", "诊断", "确诊", "史"))
    ]
    if not sentences:
        return {"matched": False, "reason": f"未找到{keyword}病史证据"}

    duration_patterns = [
        r"(?:超过|大于|多于)\s*(\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)\s*年",
        r"(?:不少于|不低于|至少)\s*(\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)\s*年",
        r"(\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)\s*年\s*(?:以上|及以上|余|多)?",
        r"(\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)\s*(?:余|多)\s*年",
    ]
    found_keyword_only = False
    for sentence in sentences:
        found_keyword_only = True
        for pattern in duration_patterns:
            m = re.search(pattern, sentence)
            if not m:
                continue
            years = parse_cn_number(m.group(1))
            if years is None:
                continue
            ok = compare_values(years, operator, threshold)
            if ok is None:
                return None
            display_years = int(years) if float(years).is_integer() else years
            display_threshold = int(threshold) if float(threshold).is_integer() else threshold
            return {
                "matched": bool(ok),
                "reason": f"{keyword}病史年限={display_years}年 {operator} {display_threshold}年",
            }

    if found_keyword_only:
        return {"matched": False, "reason": f"找到{keyword}病史，但未找到明确年限证据"}
    return None


def split_compound_clauses(condition: str) -> tuple[list[str], str]:
    text = (condition or "").strip()
    text = re.sub(r"(的患者|的病人|的病例|患者|病人|病例)$", "", text)
    if re.search(r"(或者|或)", text):
        parts = [part.strip() for part in re.split(r"\s*(?:或者|或)\s*", text) if part.strip()]
        return (parts, "or") if len(parts) > 1 else ([], "")
    if re.search(r"(并且|而且|同时|且|以及|[，,；;])", text):
        parts = [
            part.strip()
            for part in re.split(r"\s*(?:并且|而且|同时|且|以及|[，,；;])\s*", text)
            if part.strip()
        ]
        return (parts, "and") if len(parts) > 1 else ([], "")
    return [], ""


def augment_analysis_routes(analysis: dict, original_condition: str, fallback_keyword_fn=None) -> dict:
    """Enrich LLM/fallback analysis with deterministic semantic classes."""
    if not isinstance(analysis, dict):
        return analysis

    for cond in analysis.get("conditions", []) or []:
        sq = cond.get("text") or original_condition
        if is_duration_comparison(sq):
            cond["target_skills"] = [
                service_id
                for service_id in (cond.get("target_skills") or [])
                if service_id not in {"lab-results", "drug-interaction", "diagnosis-query"}
            ]
            cond["target_docs"] = append_unique(cond.get("target_docs", []), DURATION_CLASS.docs)
            cond["target_sections"] = append_unique(cond.get("target_sections", []), DURATION_CLASS.sections)
            cond["target_skills"] = append_unique(cond.get("target_skills", []), DURATION_CLASS.services)
            cond["keyword"] = DURATION_CLASS.keyword
            cond["is_numeric"] = True
            cond["entity_type"] = "encounter"
            cond["domain"] = "encounter"
            cond["predicate"] = "compare"
            cond["semantic_class"] = DURATION_CLASS.name

        if is_outcome_state_condition(sq, original_condition):
            outcome_kw = extract_outcome_keyword(sq, fallback_keyword_fn=fallback_keyword_fn)
            outcome_mods = extract_outcome_modifiers(sq)
            cond["target_docs"] = append_unique(cond.get("target_docs", []), OUTCOME_CLASS.docs)
            cond["target_sections"] = append_unique(cond.get("target_sections", []), OUTCOME_CLASS.sections)
            cond["target_skills"] = append_unique(cond.get("target_skills", []), OUTCOME_CLASS.services)
            if outcome_kw:
                cond["keyword"] = outcome_kw
            cond["modifiers"] = append_unique(cond.get("modifiers", []), outcome_mods)
            cond["semantic_class"] = OUTCOME_CLASS.name

        has_pre_admission_context = is_pre_admission_condition(sq) or (
            is_pre_admission_condition(original_condition)
            and bool(re.match(r"^(就)?(有|患有|存在|诊断为|确诊为|得过|有过)", sq or ""))
        )
        if has_pre_admission_context:
            pre_kw = extract_pre_admission_keyword(sq, fallback_keyword_fn=fallback_keyword_fn)
            cond["target_docs"] = append_unique(cond.get("target_docs", []), PRE_ADMISSION_CLASS.docs)
            cond["target_sections"] = append_unique(cond.get("target_sections", []), PRE_ADMISSION_CLASS.sections)
            cond["target_skills"] = append_unique(cond.get("target_skills", []), PRE_ADMISSION_CLASS.services)
            if pre_kw:
                cond["keyword"] = pre_kw
                cond["entity"] = cond.get("entity") or pre_kw
            if re.search(r"(病史|史)", str(sq)) and "病史" not in (cond.get("modifiers") or []):
                cond["modifiers"] = append_unique(cond.get("modifiers", []), ["病史"])
            cond["entity_type"] = cond.get("entity_type") or "diagnosis"
            cond["predicate"] = cond.get("predicate") or "history"
            cond["semantic_class"] = PRE_ADMISSION_CLASS.name

        explicit_medication_action = has_explicit_medication_action(sq)
        lab_result_condition = is_lab_result_condition(sq, cond)

        if not explicit_medication_action and not lab_result_condition and should_route_to_diagnosis_service(sq, cond):
            cond["target_docs"] = append_unique(cond.get("target_docs", []), DIAGNOSIS_EXISTENCE_CLASS.docs)
            cond["target_sections"] = append_unique(cond.get("target_sections", []), DIAGNOSIS_EXISTENCE_CLASS.sections)
            cond["target_skills"] = append_unique(cond.get("target_skills", []), DIAGNOSIS_EXISTENCE_CLASS.services)
            if fallback_keyword_fn:
                kw = fallback_keyword_fn(sq)
                if kw:
                    cond["keyword"] = kw
                    cond["entity"] = cond.get("entity") or kw
            cond["entity_type"] = cond.get("entity_type") or "diagnosis"
            cond["predicate"] = cond.get("predicate") or ("diagnosed" if "诊断" in str(sq) else "exists")
            if not cond.get("semantic_class"):
                cond["semantic_class"] = DIAGNOSIS_EXISTENCE_CLASS.name

        if is_drug_use_condition(sq, cond):
            from .medication_rules import infer_medication_predicate

            drug_semantic = _service_metadata("drug-interaction").get("semantic") or {}
            if explicit_medication_action:
                cond["target_skills"] = [
                    service_id
                    for service_id in (cond.get("target_skills") or [])
                    if service_id not in {"diagnosis-query", "lab-results"}
                ]
                cond["target_docs"] = [
                    doc
                    for doc in (cond.get("target_docs") or [])
                    if doc not in DIAGNOSIS_EXISTENCE_CLASS.docs or doc in str(sq)
                ]
                cond["target_sections"] = [
                    section
                    for section in (cond.get("target_sections") or [])
                    if section not in DIAGNOSIS_LIKE_SECTIONS or section in str(sq)
                ]
            cond["target_skills"] = append_unique(cond.get("target_skills", []), DRUG_USE_CLASS.services)
            if fallback_keyword_fn:
                kw = fallback_keyword_fn(sq)
                if kw:
                    cond["keyword"] = kw
                    cond["entity"] = cond.get("entity") or kw
            cond["entity_type"] = drug_semantic.get("entity_type") or "drug"
            cond["predicate"] = infer_medication_predicate(sq, cond.get("predicate") or drug_semantic.get("predicate"))
            cond["semantic_class"] = drug_semantic.get("semantic_class") or DRUG_USE_CLASS.name

        if lab_result_condition:
            cond["target_skills"] = [
                service_id
                for service_id in (cond.get("target_skills") or [])
                if service_id not in {"diagnosis-query", "drug-interaction"}
            ]
            cond["target_docs"] = [
                doc
                for doc in (cond.get("target_docs") or [])
                if doc not in DIAGNOSIS_EXISTENCE_CLASS.docs or doc in str(sq)
            ]
            cond["target_sections"] = [
                section
                for section in (cond.get("target_sections") or [])
                if section not in DIAGNOSIS_LIKE_SECTIONS or section in str(sq)
            ]
            cond["target_skills"] = append_unique(cond.get("target_skills", []), LAB_RESULT_CLASS.services)
            if "住院" in str(sq) or "入院" in str(sq) or "出院" in str(sq):
                cond["target_skills"] = append_unique(cond.get("target_skills", []), ["encounter-info"])
            if fallback_keyword_fn:
                kw = fallback_keyword_fn(sq)
                if kw:
                    cond["keyword"] = kw
                    cond["entity"] = kw
            cond["entity_type"] = "lab"
            cond["domain"] = "laboratory"
            if any(token in str(sq) for token in ("偏低", "降低", "低于")):
                cond["predicate"] = "low"
            elif any(token in str(sq) for token in ("偏高", "升高", "增高", "高于")):
                cond["predicate"] = "high"
            elif any(token in str(sq) for token in ("异常", "不正常")):
                cond["predicate"] = "abnormal"
            elif "正常" in str(sq):
                cond["predicate"] = "normal"
            elif is_numeric_comparison(str(sq)):
                cond["predicate"] = "compare"
            else:
                cond["predicate"] = "unknown"
            cond["semantic_class"] = LAB_RESULT_CLASS.name
    return analysis


def maybe_split_compound_analysis(analysis: dict, original_condition: str, fallback_keyword_fn=None) -> dict:
    """Fallback split only when the LLM did not split explicit compound clauses."""
    conditions = analysis.get("conditions", []) if isinstance(analysis, dict) else []
    if len(conditions) > 1:
        return analysis
    parts, connector = split_compound_clauses(original_condition)
    if len(parts) <= 1:
        return analysis
    analysis["type"] = "compound"
    analysis["connector"] = connector
    analysis["conditions"] = [
        {
            "text": part,
            "keyword": fallback_keyword_fn(part) if fallback_keyword_fn else part,
            "modifiers": [],
            "is_numeric": is_numeric_comparison(part),
            "target_docs": [],
            "target_sections": [],
            "target_skills": [],
        }
        for part in parts
    ]
    analysis["source"] = f"{analysis.get('source', 'unknown')}+deterministic_split"
    return analysis
