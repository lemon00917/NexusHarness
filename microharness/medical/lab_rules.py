"""
Deterministic rules for structured lab-result filtering.

The rules operate on service metadata fields, not on example questions. LLMs may
normalize a user's wording, but lab item matching and numeric/abnormal checks are
computed from structured result fields here.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from microharness.medical.entity_normalization import entity_candidates as normalized_entity_candidates
from microharness.medical.temporal_parser import (
    compare_values,
    normalize_numeric_text,
    operator_display,
    parse_measurement_value,
)
from microharness.medical.query_ir_validator import parse_executable_numeric_comparison
from microharness.medical.display_text import display_status
from microharness.medical.record_identity import display_record_reference, identity_from_binding
from microharness.medical.time_window import TimeWindow, parse_datetime_value, requires_period_window


_LAB_FIELDS = {
    "inspItemCode",
    "inspItemDesc",
    "inspectionValue",
    "inspResultUnitCode",
    "inspectionResult",
    "inspResultDesc",
    "inspAbnoFlag",
    "inspResultRange",
    "inspItemAbbr",
    "inspExtraResult",
    "inspectionDate",
    "inspectionTime",
    "inspectionDateTime",
}

_ITEM_FIELDS = {"inspItemDesc", "inspItemAbbr", "inspItemCode", "化验项目描述", "缩写", "化验项目代码"}
_RESULT_FIELDS = {
    "inspectionValue",
    "inspectionResult",
    "inspResultUnitCode",
    "inspResultDesc",
    "inspExtraResult",
    "结果",
    "定性结果",
    "单位",
    "结果说明",
    "扩展结果",
}


@dataclass
class LabRecord:
    prefix: str
    record_id: str = ""
    record_id_label: str = ""
    record_id_field: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    display_fields: dict[str, str] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)

    def get(self, *names: str) -> str:
        for name in names:
            val = self.fields.get(name)
            if val:
                return val
        for name in names:
            val = self.display_fields.get(name)
            if val:
                return val
        return ""

    def item_text(self) -> str:
        return " ".join(
            v for v in (
                self.get("inspItemDesc", "化验项目描述"),
                self.get("inspItemAbbr", "缩写"),
                self.get("inspItemCode", "化验项目代码"),
            ) if v
        )

    def result_text(self) -> str:
        return " ".join(
            v for v in (
                self.get("inspectionValue", "结果"),
                self.get("inspectionResult", "定性结果"),
                self.get("inspResultDesc", "结果说明"),
                self.get("inspExtraResult", "扩展结果"),
                self.get("inspAbnoFlag", "异常标志"),
                self.get("inspResultRange", "参考范围"),
            ) if v
        )

    def compact_line(self) -> str:
        return " | ".join(self.lines)

    def display_reference(self) -> str:
        return display_record_reference(self.prefix or "检验记录", self.record_id, self.record_id_label)

    def inspection_datetime(self) -> Optional[datetime]:
        raw = " ".join(
            v for v in (
                self.get("inspectionDateTime", "检测日期时间"),
                " ".join(
                    v2 for v2 in (
                        self.get("inspectionDate", "检测日期"),
                        self.get("inspectionTime", "检测时间"),
                    ) if v2
                ),
            ) if v
        ).strip()
        return parse_datetime_value(raw)


def _record_prefix(field: str) -> str:
    m = re.match(r"(\[[^]]+\])", field or "")
    return m.group(1) if m else ""


def _strip_prefix(field: str) -> str:
    if field.startswith("[") and "] " in field:
        return field.split("] ", 1)[1]
    return field


def is_lab_bindings(bindings: list[dict[str, Any]]) -> bool:
    for b in bindings or []:
        eng = str(b.get("eng_field") or b.get("xml_path", "").split("/")[-1])
        label = _strip_prefix(str(b.get("html_field", "")))
        if eng in _LAB_FIELDS or label in _ITEM_FIELDS or label in _RESULT_FIELDS:
            return True
    return False


def lab_records_from_bindings(bindings: list[dict[str, Any]]) -> list[LabRecord]:
    records: dict[str, LabRecord] = {}
    for b in bindings or []:
        label = str(b.get("html_field", ""))
        val = str(b.get("html_value") or b.get("value") or "").strip()
        if not val:
            continue
        prefix = _record_prefix(label)
        field_label = _strip_prefix(label)
        eng = str(b.get("eng_field") or b.get("xml_path", "").split("/")[-1] or field_label)
        rec = records.setdefault(prefix, LabRecord(prefix=prefix))
        identity = identity_from_binding(b)
        if identity and not rec.record_id:
            rec.record_id = identity["record_id"]
            rec.record_id_label = identity["record_id_label"]
            rec.record_id_field = identity["record_id_field"]
        rec.fields[eng] = val
        rec.display_fields[field_label] = val
        rec.lines.append(f"{label}: {val}" if label else val)
    return list(records.values())


def _condition_without_time_scope(condition: str) -> str:
    """Remove temporal scope grammar before parsing lab value comparisons."""
    text = normalize_numeric_text(condition or "")
    duration = r"第?[零〇一二两三四五六七八九十百千万亿\d.]+\s*(?:天|日|小时|分钟|周|月|个月)"
    text = re.sub(r"(?:住院期间|住院期内|本次住院|住院内|住院过程中)", "", text)
    text = re.sub(
        rf"(?:入院前|入院后|出院前|出院后|术前|术后|手术前|手术后)\s*{duration}\s*(?:内|前|后)?",
        "",
        text,
    )
    text = re.sub(rf"(?:入院|出院)\s*{duration}\s*(?:内|之内)", "", text)
    text = re.sub(r"(?:入院前|入院后|出院前|出院后|术前|术后|手术前|手术后)", "", text)
    return text.strip()


def extract_lab_keyword(condition: str) -> str:
    condition = normalize_numeric_text(condition or "")
    clauses = [
        c.strip()
        for c in re.split(r"\s*(?:并且|而且|同时|且|以及|和|，|,|；|;)\s*", condition)
        if c.strip()
    ]
    if len(clauses) > 1:
        lab_clauses = [
            c for c in clauses
            if re.search(r"(指标|检验|化验|项目|结果|数值|水平|偏高|偏低|升高|降低|增高|减少|异常|正常|阳性|阴性|>|<|≥|≤|=)", c)
            and not re.fullmatch(r".*(?:岁|年龄|病史|史).*", c)
        ]
        if lab_clauses:
            condition = max(lab_clauses, key=len)
    value_condition = _condition_without_time_scope(condition)
    parsed = parse_executable_numeric_comparison(value_condition)
    text = parsed.subject if parsed else value_condition
    scope_match = list(re.finditer(r"(住院期间|住院期内|入院前|入院后|出院前|出院后|术前|术后|手术前|手术后)", text))
    if scope_match:
        last_scope = scope_match[-1]
        tail = text[last_scope.start():]
        if re.search(r"(指标|检验|化验|项目|结果|数值|水平|偏高|偏低|升高|降低|增高|减少|异常|正常|阳性|阴性|>|<|≥|≤|=)", tail):
            text = tail
    text = re.sub(
        r"(住院期间|住院期内|入院前|入院后|出院前|出院后|术前|术后|手术前|手术后|"
        r"第?[零〇一二两三四五六七八九十百千万亿\d.]+\s*(?:天|日|小时|分钟|周|月|个月)(?:内|前|后)?)",
        "",
        text,
    )
    text = re.sub(r"^(手术|入院|出院)(?:时|中|期间)?", "", text)
    text = re.sub(r"^(做了|做过|检测|检查|查|测|使用过|使用|用了|用过|有|存在)", "", text)
    text = re.sub(
        r"(指标|检验|化验|项目|结果|数值|水平|值|偏高|偏低|升高|降低|增高|减少|不正常|"
        r"异常|正常|阳性|阴性|高于参考范围|低于参考范围|的患者|的病人|患者|病人)",
        "",
        text,
    )
    text = re.sub(
        r"\s*(大于|小于|高于|低于|不超过|不低于|不少于|至少|至多|等于|>=|<=|>|<|=)\s*"
        r"[\d.]+\s*(?:[x*]\s*10(?:\^\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹]+))?(?:\S*/\S*)?",
        "",
        text,
    )
    text = re.sub(r"[\s　,，;；、。]+", "", text)
    return text


def _char_overlap_match(keyword: str, text: str) -> bool:
    if not keyword or not text:
        return False
    if keyword in text:
        return True
    chars = list(keyword)
    found = sum(1 for c in chars if c in text)
    if len(chars) <= 2:
        return found == len(chars)
    return found >= len(chars) - 1


def _item_matches(keyword: str, record: LabRecord) -> bool:
    if not keyword:
        return True
    item_text = record.item_text()
    return _char_overlap_match(keyword, item_text)


def _normalize_item_name(text: str) -> str:
    text = re.sub(r"[(（][^)）]*[)）]", "", text or "")
    return re.sub(r"[\s　,，;；、。]+", "", text)


def _item_match_score(keyword: str, record: LabRecord) -> int:
    """Score item-name relevance so direct indicators beat related derivatives."""
    if not keyword:
        return 100
    kw = _normalize_item_name(keyword)
    desc = _normalize_item_name(record.get("inspItemDesc", "化验项目描述"))
    abbr = _normalize_item_name(record.get("inspItemAbbr", "缩写"))
    code = _normalize_item_name(record.get("inspItemCode", "化验项目代码"))
    if not kw:
        return 0
    if kw == desc:
        return 100
    if abbr and kw.upper() == abbr.upper():
        return 95
    if code and kw == code:
        return 92
    if desc and desc in kw and len(desc) >= 2:
        return 82
    if desc and desc.startswith(kw):
        return 88
    if desc and kw in desc:
        return 65
    if _char_overlap_match(kw, " ".join([desc, abbr, code])):
        return 55
    return 0


def _select_candidate_records(keyword: str, records: list[LabRecord]) -> list[LabRecord]:
    scored = [(score, rec) for rec in records if (score := _item_match_score(keyword, rec)) > 0]
    if not scored:
        return []
    best = max(score for score, _ in scored)
    if best >= 85:
        return [rec for score, rec in scored if score >= 85]
    return [rec for _, rec in scored]


def _select_candidate_records_for_entities(
    keywords: list[str],
    records: list[LabRecord],
) -> tuple[list[LabRecord], dict[int, str]]:
    """Select records by the best canonical-name or exact-alias candidate."""
    scored: list[tuple[int, LabRecord, str]] = []
    for record in records:
        candidate_scores = [
            (_item_match_score(keyword, record), keyword)
            for keyword in keywords
            if keyword
        ]
        if not candidate_scores:
            continue
        score, matched_entity = max(candidate_scores, key=lambda item: item[0])
        if score > 0:
            scored.append((score, record, matched_entity))
    if not scored:
        return [], {}
    best = max(score for score, _, _ in scored)
    selected = [item for item in scored if best < 85 or item[0] >= 85]
    return (
        [record for _, record, _ in selected],
        {id(record): matched_entity for _, record, matched_entity in selected},
    )


def _unit_has_scientific_volume(unit: str) -> bool:
    text = normalize_numeric_text(unit or "")
    text = re.sub(r"\s+", "", text)
    return bool(re.search(r"(?:x|\*)?10(?:\^|[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻])", text) and re.search(r"/?[Ll升]", text))


def _condition_expects_scientific_volume(condition: str) -> bool:
    text = _condition_without_time_scope(condition)
    return bool(
        re.search(r"(?:x|\*)\s*10(?:\s*\^\s*[+-]?\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+)", text)
        and ("/" in text or re.search(r"[Ll升]", text) or parse_executable_numeric_comparison(text))
    )


def _filter_by_numeric_dimension(condition: str, records: list[LabRecord]) -> list[LabRecord]:
    value_condition = _condition_without_time_scope(condition)
    if not records or not parse_executable_numeric_comparison(value_condition):
        return records
    if not _condition_expects_scientific_volume(value_condition):
        return records
    compatible = [
        rec for rec in records
        if _unit_has_scientific_volume(rec.get("inspResultUnitCode", "单位"))
    ]
    return compatible or records


def _display_unit(unit: str) -> str:
    text = normalize_numeric_text(unit or "")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"^\*", "×", text)
    text = text.replace("x", "×")
    text = re.sub(r"^10\^", "×10^", text)
    text = re.sub(r"10\^([+-]?\d+)", r"10^\1", text)
    return text


def _display_measurement(value: str, unit: str) -> str:
    value_text = str(value or "").strip()
    unit_text = _display_unit(unit)
    if not unit_text:
        return value_text
    if re.search(r"×?10\^[+-]?\d+", unit_text) and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value_text):
        return f"{value_text}{unit_text if unit_text.startswith('×') else '×' + unit_text}"
    return f"{value_text}{unit_text}"


def _comparison_reason(value: float, operator: str, threshold: float, ok: bool) -> str:
    left = _format_number_for_display(value)
    right = _format_number_for_display(threshold)
    op = operator_display(operator)
    if ok:
        return f"结果满足：{left} {op} {right}"
    negative = {
        ">": "不大于",
        "≥": "不大于等于",
        "<": "不小于",
        "≤": "不小于等于",
        "=": "不等于",
    }.get(op, f"不满足 {op}")
    return f"结果不满足：{left} {negative} {right}"


def _parse_range_bounds(raw_range: str, unit: str) -> tuple[Optional[float], Optional[float]]:
    raw = normalize_numeric_text(raw_range or "")
    if not raw:
        return None, None
    raw = re.sub(r"(?<=\d)\s*(?:-|~|－|—|–|至)\s*(?=\d)", " ", raw)
    nums = re.findall(
        r"[+-]?\d+(?:\.\d+)?(?:\s*(?:x|\*)\s*10(?:\s*\^\s*[+-]?\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+))?",
        raw,
    )
    parsed = [parse_measurement_value(n, unit) for n in nums]
    parsed = [p for p in parsed if p is not None]
    if len(parsed) >= 2:
        return min(parsed[0], parsed[1]), max(parsed[0], parsed[1])
    if len(parsed) == 1:
        if "<" in raw or "≤" in raw:
            return None, parsed[0]
        if ">" in raw or "≥" in raw:
            return parsed[0], None
    return None, None


def _record_numeric_value(record: LabRecord) -> Optional[float]:
    unit = record.get("inspResultUnitCode", "单位")
    for field in ("inspectionValue", "结果", "inspectionResult", "定性结果"):
        val = record.get(field)
        parsed = parse_measurement_value(val, unit)
        if parsed is not None:
            return parsed
    return None


def _range_status(record: LabRecord) -> str:
    value = _record_numeric_value(record)
    if value is None:
        return ""
    unit = record.get("inspResultUnitCode", "单位")
    low, high = _parse_range_bounds(record.get("inspResultRange", "参考范围"), unit)
    if high is not None and value > high:
        return "high"
    if low is not None and value < low:
        return "low"
    if low is not None or high is not None:
        return "normal"
    return ""


def _abnormal_status(record: LabRecord) -> str:
    flag = record.get("inspAbnoFlag", "异常标志").strip()
    text = " ".join([flag, record.result_text()])
    if any(token in text for token in ("↑", "H", "High", "HIGH", "偏高", "升高", "增高", "高于参考")):
        return "high"
    if any(token in text for token in ("↓", "L", "Low", "LOW", "偏低", "降低", "减少", "低于参考")):
        return "low"
    status = _range_status(record)
    if status:
        return status
    if flag and flag not in {"N", "正常", "-", "无"}:
        return "abnormal"
    return "normal" if flag in {"N", "正常", "-", "无"} else ""


def _wanted_status(condition: str) -> str:
    text = normalize_numeric_text(condition or "")
    if re.search(r"(偏高|升高|增高|高于参考范围|高于正常|大于参考)", text):
        return "high"
    if re.search(r"(偏低|降低|减少|低于参考范围|低于正常|小于参考)", text):
        return "low"
    if re.search(r"(异常|不正常)", text):
        return "abnormal"
    if re.search(r"(正常|未见异常)", text):
        return "normal"
    return ""


def _qualitative_wanted(condition: str) -> str:
    if "阳性" in condition or "(+)" in condition:
        return "阳性"
    if "阴性" in condition or "(-)" in condition:
        return "阴性"
    return ""


def _record_satisfies(record: LabRecord, condition: str) -> tuple[bool, str]:
    value_condition = _condition_without_time_scope(condition)
    cmp_info = parse_executable_numeric_comparison(value_condition)
    if cmp_info:
        value = _record_numeric_value(record)
        if value is None:
            return False, "结果字段无法解析为数值"
        ok = compare_values(value, cmp_info.operator, cmp_info.threshold)
        if ok is None:
            return False, f"不支持的比较符{cmp_info.operator}"
        return ok, _comparison_reason(value, cmp_info.operator, cmp_info.threshold, bool(ok))

    wanted = _wanted_status(value_condition)
    if wanted:
        status = _abnormal_status(record)
        status_text = display_status(status or "unknown")
        if wanted == "abnormal":
            return status in {"high", "low", "abnormal"}, f"异常状态：{status_text}"
        if wanted == "normal":
            return status == "normal", f"异常状态：{status_text}"
        return status == wanted, f"异常状态：{status_text}"

    qualitative = _qualitative_wanted(value_condition)
    if qualitative:
        text = record.result_text()
        return qualitative in text, f"定性结果{'包含' if qualitative in text else '不包含'}{qualitative}"

    return True, "项目匹配"


def _format_number_for_display(value: float) -> str:
    """Avoid leaking Python scientific notation into user-facing evidence."""
    try:
        number = float(value)
    except Exception:
        return str(value)
    if number == 0:
        return "0"
    abs_number = abs(number)
    if abs_number >= 1_000_000 or abs_number < 0.0001:
        exponent = int(math.floor(math.log10(abs_number)))
        mantissa = number / (10 ** exponent)
        return f"{mantissa:g}×10^{exponent}"
    return f"{number:g}"


def _record_evidence_line(record: LabRecord, condition: str) -> str:
    inspected_at = record.inspection_datetime()
    inspected_text = inspected_at.strftime("%Y-%m-%d %H:%M:%S") if inspected_at else "缺少检测时间"
    value = record.get("inspectionValue", "结果", "inspectionResult", "定性结果")
    unit = record.get("inspResultUnitCode", "单位")
    flag = record.get("inspAbnoFlag", "异常标志") or "无"
    _, status_reason = _record_satisfies(record, condition)
    return (
        f"{record.display_reference()} 检测时间={inspected_text}，"
        f"结果：{_display_measurement(value, unit)}，异常标志：{flag}，{status_reason}"
    )


def _record_evidence_summary(records: list[LabRecord], condition: str, limit: int = 20) -> str:
    sorted_records = sorted(
        records or [],
        key=lambda rec: rec.inspection_datetime() or datetime.max,
    )
    lines = [_record_evidence_line(rec, condition) for rec in sorted_records[:limit]]
    if len(sorted_records) > limit:
        lines.append(f"另有{len(sorted_records) - limit}条候选记录未展开")
    return "；".join(lines)


def _record_judgment_summary(record: LabRecord, judgment: str) -> str:
    inspected_at = record.inspection_datetime()
    inspected_text = inspected_at.strftime("%Y-%m-%d %H:%M:%S") if inspected_at else "缺少检测时间"
    value = record.get("inspectionValue", "结果", "inspectionResult", "定性结果")
    unit = _display_unit(record.get("inspResultUnitCode", "单位"))
    flag = record.get("inspAbnoFlag", "异常标志") or "无"
    reference = record.get("inspResultRange", "参考范围") or "未提供"
    item = record.get("inspItemDesc", "化验项目描述") or record.get("inspItemAbbr", "缩写") or "检验项目"
    return (
        f"{record.display_reference()} 项目={item}，检测时间={inspected_text}，"
        f"结果={_display_measurement(value, unit)}，异常标志={flag}，参考范围={reference}，{judgment}"
    )


def _record_candidate_detail(
    record: LabRecord,
    condition: str,
    time_window: Optional[TimeWindow] = None,
) -> dict[str, Any]:
    inspected_at = record.inspection_datetime()
    ok, value_reason = _record_satisfies(record, condition)
    in_window: Optional[bool] = None
    if time_window and time_window.resolved and time_window.start and inspected_at:
        in_window = time_window.contains(inspected_at)
    if time_window and time_window.required:
        if not time_window.resolved or inspected_at is None:
            scope_status = "UNKNOWN"
        else:
            scope_status = "IN_SCOPE" if in_window else "OUT_OF_SCOPE"
    else:
        scope_status = "NOT_REQUIRED"
    return {
        "记录": record.display_reference(),
        "记录序号": record.prefix or "检验记录",
        "记录ID": record.record_id,
        "记录标识名称": record.record_id_label,
        "记录标识字段": record.record_id_field,
        "项目": record.get("inspItemDesc", "化验项目描述"),
        "缩写": record.get("inspItemAbbr", "缩写"),
        "检测时间": inspected_at.strftime("%Y-%m-%d %H:%M:%S") if inspected_at else "",
        "结果": record.get("inspectionValue", "结果", "inspectionResult", "定性结果"),
        "单位": _display_unit(record.get("inspResultUnitCode", "单位")),
        "异常标志": record.get("inspAbnoFlag", "异常标志") or "无",
        "参考范围": record.get("inspResultRange", "参考范围"),
        "数值判断": value_reason,
        "数值是否满足": bool(ok),
        "是否在时间窗": in_window,
        "record_status": "MATCHED" if ok else "NOT_MATCHED",
        "record_reason_code": "VALUE_CONDITION_MET" if ok else "VALUE_CONDITION_NOT_MET",
        "record_reason": value_reason,
        "scope_status": scope_status,
        "event_time": inspected_at.strftime("%Y-%m-%d %H:%M:%S") if inspected_at else "",
    }


def _candidate_details(
    records: list[LabRecord],
    condition: str,
    time_window: Optional[TimeWindow] = None,
    matched_entities: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    details = []
    for record in sorted(records or [], key=lambda r: r.inspection_datetime() or datetime.max):
        detail = _record_candidate_detail(record, condition, time_window)
        if matched_entities and id(record) in matched_entities:
            detail["匹配实体"] = matched_entities[id(record)]
        details.append(detail)
    return details


def judge_lab_condition(
    condition: str,
    bindings: list[dict[str, Any]],
    time_window: Optional[TimeWindow] = None,
    *,
    entity_candidates: list[str] | None = None,
) -> dict[str, Any]:
    if not is_lab_bindings(bindings):
        return {"applicable": False}

    records = lab_records_from_bindings(bindings)
    if not records:
        return {
            "applicable": True,
            "matched": False,
            "status": "NOT_MENTIONED",
            "reason_code": "NO_MATCHING_RECORD",
            "reason": "检验数据源查询成功，但未返回检验记录",
            "fields": "",
            "candidate_count": 0,
            "candidate_records": [],
        }

    keyword = extract_lab_keyword(condition)
    keywords = normalized_entity_candidates(
        keyword,
        {"entity_candidates": entity_candidates or []},
    )
    value_condition = _condition_without_time_scope(condition)
    has_lab_predicate = bool(
        parse_executable_numeric_comparison(value_condition)
        or _wanted_status(value_condition)
        or _qualitative_wanted(value_condition)
    )
    if len(keyword) < 2 and not has_lab_predicate:
        return {"applicable": False}
    candidate_records, matched_entities = _select_candidate_records_for_entities(keywords, records)
    candidate_records = _filter_by_numeric_dimension(condition, candidate_records)
    matched_entities = {
        id(record): matched_entities[id(record)]
        for record in candidate_records
        if id(record) in matched_entities
    }
    all_fields = "\n".join(r.compact_line() for r in records)[:4000]

    if not candidate_records:
        reason = f"未找到检验项目'{keyword}'" if keyword else "未找到可判定的检验项目"
        return {
            "applicable": True,
            "matched": False,
            "status": "NOT_MENTIONED",
            "reason_code": "NO_MATCHING_RECORD",
            "reason": reason,
            "fields": all_fields,
            "matched_prefixes": [],
            "keyword": keyword,
            "candidate_count": 0,
            "candidate_records": [],
        }

    all_candidate_records = list(candidate_records)
    if requires_period_window(condition):
        if not time_window or not time_window.resolved or not time_window.start:
            details = _candidate_details(candidate_records, condition, time_window, matched_entities)
            return {
                "applicable": True,
                "matched": False,
                "status": "UNKNOWN",
                "reason_code": "MISSING_EVENT_TIME",
                "reason": (
                    f"共找到{len(candidate_records)}条检验项目'{keyword}'记录，"
                    f"但缺少{time_window.scope if time_window else '时间范围'}锚点，"
                    f"无法判断检验记录是否发生在目标期间："
                    f"{_record_evidence_summary(candidate_records, condition)}"
                ),
                "fields": "\n".join(r.compact_line() for r in candidate_records)[:4000],
                "matched_prefixes": [],
                "keyword": keyword,
                "candidate_count": len(candidate_records),
                "candidate_records": details,
            }
        in_window_records = []
        missing_time_records = []
        outside_records = []
        for rec in candidate_records:
            inspected_at = rec.inspection_datetime()
            if inspected_at is None:
                missing_time_records.append(rec)
                continue
            if time_window.contains(inspected_at):
                in_window_records.append(rec)
            else:
                outside_records.append(rec)
        if not in_window_records:
            window_text = time_window.describe()
            if missing_time_records and not outside_records:
                details = _candidate_details(candidate_records, condition, time_window, matched_entities)
                return {
                    "applicable": True,
                    "matched": False,
                    "status": "UNKNOWN",
                    "reason_code": "MISSING_EVENT_TIME",
                    "reason": (
                        f"共找到{len(candidate_records)}条检验项目'{keyword}'记录，"
                        f"但均缺少检测日期时间，无法判断是否在{time_window.scope}"
                        f"（范围：{window_text}）："
                        f"{_record_evidence_summary(candidate_records, condition)}"
                    ),
                    "fields": "\n".join(r.compact_line() for r in candidate_records)[:4000],
                    "matched_prefixes": [],
                    "keyword": keyword,
                    "candidate_count": len(candidate_records),
                    "candidate_records": details,
                }
            outside_text = _record_evidence_summary(outside_records + missing_time_records, condition)
            details = _candidate_details(candidate_records, condition, time_window, matched_entities)
            return {
                "applicable": True,
                "matched": False,
                "status": "NOT_MATCHED",
                "reason_code": "TIME_OUTSIDE_WINDOW",
                "reason": (
                    f"共找到{len(candidate_records)}条检验项目'{keyword}'记录，"
                    f"其中{len(outside_records)}条检测时间不在{time_window.scope}"
                    f"（范围：{window_text}）"
                    f"{'，另有' + str(len(missing_time_records)) + '条缺少检测时间' if missing_time_records else ''}："
                    f"{outside_text}"
                ),
                "fields": "\n".join(r.compact_line() for r in candidate_records)[:4000],
                "matched_prefixes": [],
                "keyword": keyword,
                "candidate_count": len(candidate_records),
                "candidate_records": details,
            }
        candidate_records = in_window_records

    matched_records = []
    failed_reasons = []
    all_compared_records = []
    for rec in candidate_records:
        ok, reason = _record_satisfies(rec, condition)
        all_compared_records.append(rec)
        if ok:
            matched_records.append((rec, reason))
        else:
            failed_reasons.append(_record_judgment_summary(rec, reason))

    if matched_records:
        prefixes = [rec.prefix for rec, _ in matched_records if rec.prefix]
        evidence = "\n".join(rec.compact_line() for rec, _ in matched_records)[:4000]
        detail = "；".join(
            _record_judgment_summary(rec, reason) for rec, reason in matched_records[:5]
        )
        in_window_count = len(candidate_records) if requires_period_window(condition) else len(all_candidate_records)
        return {
            "applicable": True,
            "matched": True,
            "status": "MATCHED",
            "reason_code": "MATCH_CONFIRMED",
            "reason": (
                f"共找到{len(all_candidate_records)}条检验项目'{keyword}'记录，"
                + (f"其中{in_window_count}条在{time_window.scope}内，" if requires_period_window(condition) and time_window else "")
                + f"{len(matched_records)}条符合数值条件：{detail or '检验记录符合条件'}"
            ),
            "fields": evidence,
            "matched_prefixes": prefixes,
            "keyword": keyword,
            "candidate_count": len(all_candidate_records),
            "candidate_records": _candidate_details(all_candidate_records, condition, time_window, matched_entities),
        }

    return {
        "applicable": True,
        "matched": False,
        "status": "NOT_MATCHED",
        "reason_code": "VALUE_CONDITION_NOT_MET",
        "reason": (
            f"共找到{len(all_candidate_records)}条检验项目'{keyword}'记录，"
            "但结果均不符合：" + "；".join(failed_reasons[:20])
        ),
        "fields": all_fields,
        "matched_prefixes": [],
        "keyword": keyword,
        "candidate_count": len(all_candidate_records),
        "candidate_records": _candidate_details(all_candidate_records, condition, time_window, matched_entities),
    }
