"""Deterministic semantic guards for free-text medical document evidence.

Rules are entity-agnostic: callers provide the extracted entity, while this
module only reasons about assertion, subject, and temporal context.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

from microharness.medical.time_window import TimeWindow, parse_datetime_values


MATCHED = "MATCHED"
NOT_MATCHED = "NOT_MATCHED"
NOT_MENTIONED = "NOT_MENTIONED"
UNKNOWN = "UNKNOWN"
NO_DECISION = "NO_DECISION"


@dataclass(frozen=True)
class DocumentSemanticDecision:
    status: str
    reason: str
    reason_code: str
    evidence: str = ""
    categories: tuple[str, ...] = field(default_factory=tuple)
    trace: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def matched(self) -> bool:
        return self.status == MATCHED

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "matched": self.matched,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "evidence": self.evidence,
            "categories": list(self.categories),
            "trace": list(self.trace),
        }


_SENTENCE_SPLIT_RE = re.compile(r"[。；;！？!?\n\r]+")
_INLINE_SENTENCE_SPLIT_RE = re.compile(r"[。；;！？!?]+")
_SECTION_PREFIX_RE = re.compile(r"^(?P<label>[^：:\n]{1,24})[：:]\s*(?P<body>.+)$")
_REFERENCE_RE = re.compile(
    r"该症状|上述症状|此症状|这种症状|该疾病|上述疾病|此病|该情况|上述情况|这种情况|"
    r"其(?=目前|当前|现|仍|已|尚|未|无|有|出现|发生|持续|加重|缓解|消失|明确)"
)
_REFERENCE_NEGATION_RE = re.compile(r"否认|未诉|未见|未发现|没有|不存在|目前无|现无")
_REFERENCE_EXCLUDED_RE = re.compile(r"排除|已排除|不支持|未支持|除外")
_REFERENCE_RESOLVED_RE = re.compile(r"已缓解|已消失|已治愈|已痊愈|未再|不再")
_REFERENCE_POSITIVE_RE = re.compile(r"出现|发生|存在|持续|加重|明显|有|仍有|伴有")
_COREFERENCE_MAX_SENTENCE_DISTANCE = 2
_PATIENT_TERMS_RE = re.compile(r"患者|病人|本人|患儿|该患者|其本人")
_NON_PATIENT_TERMS_RE = re.compile(
    r"家族史|家属|父亲|母亲|父母|祖父|祖母|外祖父|外祖母|兄弟|姐妹|"
    r"哥哥|弟弟|姐姐|妹妹|儿子|女儿|子女|丈夫|妻子|配偶"
)
_UNCERTAIN_TERMS_RE = re.compile(
    r"考虑|疑似|可疑|可能|待排|待除外|不除外|不能排除|尚不能排除|"
    r"倾向于?|拟诊|初步考虑|诊断未明"
)
_EXCLUDED_TERMS_RE = re.compile(r"排除|已排除|予以排除|不支持|未支持|除外")
_NEGATION_TERMS_RE = re.compile(r"否认|未诉|未述及|未见|未发现|未提示|未查见|没有|不存在")
_HISTORY_TERMS_RE = re.compile(r"既往|曾经|曾有|既往史|病史|多年前|幼年|小时候")
_RESOLVED_TERMS_RE = re.compile(r"已缓解|已消失|已治愈|已痊愈|目前无|现无|未再|不再")
_HISTORY_QUERY_RE = re.compile(r"既往|曾经|病史|入院前|过去|历史")
_UNCERTAIN_QUERY_RE = re.compile(r"疑似|考虑|可能|待排|不除外|不能排除|倾向|拟诊")
_RELATIVE_TIME_RE = re.compile(
    r"(?P<anchor>手术|术|入院|出院)\s*(?P<direction>前|后)\s*第?\s*"
    r"(?P<amount>\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)?\s*"
    r"(?P<unit>分钟|小时|天|日|周|月|个月)?\s*(?P<within>内)?"
)
_AMOUNT_FIRST_TIME_RE = re.compile(
    r"(?P<anchor>手术|术|入院|出院)\s*"
    r"(?P<amount>\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)\s*"
    r"(?P<unit>分钟|小时|天|日|周|月|个月)\s*(?P<direction>前|后|内)"
)
_INPATIENT_CONTEXT_RE = re.compile(
    r"住院期间|住院过程中|本次住院|住院第\s*[零〇一二两三四五六七八九十百千万亿\d]+\s*[天日]|"
    r"入院时|入院后|出院时|出院前"
)
_OUTSIDE_INPATIENT_CONTEXT_RE = re.compile(r"入院前|出院后")
_GENERIC_RELATIVE_TIME_RE = re.compile(
    r"(?P<anchor>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9]{0,11}?)\s*"
    r"(?P<direction>前|后)\s*第?\s*"
    r"(?P<amount>\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)?\s*"
    r"(?P<unit>分钟|小时|天|日|周|月|个月)?\s*(?P<within>内)?"
)

_TEMPORAL_MATCHED = "TEMPORAL_MATCHED"
_TEMPORAL_OUTSIDE = "TEMPORAL_OUTSIDE"
_TEMPORAL_UNKNOWN = "TEMPORAL_UNKNOWN"

_CURRENT_POSITIVE_TERMS_RE = re.compile(
    r"\u76ee\u524d(?:\u4ecd\u6709|\u5b58\u5728|\u51fa\u73b0)|"
    r"\u5f53\u524d(?:\u4ecd\u6709|\u5b58\u5728|\u51fa\u73b0)|"
    r"\u73b0(?:\u4ecd\u6709|\u6709|\u51fa\u73b0)|"
    r"\u518d\u6b21\u51fa\u73b0|\u53c8\u51fa\u73b0|\u7ee7\u7eed\u6709"
)
_CLAUSE_BOUNDARY_RE = re.compile(r"[,\uFF0C\u3002\uFF1B;\uFF01\uFF1F!?]")


@dataclass(frozen=True)
class _DocumentSentence:
    text: str
    section: int
    paragraph: int
    index: int


@dataclass(frozen=True)
class _DocumentMention:
    category: str
    sentence: str
    temporal_status: str
    temporal_reason: str
    sentence_index: int = -1
    is_coreference: bool = False
    antecedent: str = ""
    reference: str = ""
    subject: str = "patient"

    @property
    def evidence(self) -> str:
        if self.is_coreference and self.antecedent:
            return f"先行句：{self.antecedent[:180]}；引用句：{self.reference[:180]}"
        return self.sentence[:240]

    def trace_item(self) -> dict:
        assertion_reasons = {
            "POSITIVE": "当前句明确肯定目标实体",
            "NEGATED": "当前句明确否认目标实体",
            "EXCLUDED": "当前句明确排除或不支持目标实体",
            "RESOLVED_HISTORY": "当前句说明目标实体已缓解或消失",
            "UNCERTAIN": "当前句对目标实体仅作不确定陈述",
            "HISTORY_ONLY": "当前句仅在历史语境提及目标实体",
            "NON_PATIENT_SUBJECT": "当前句主体不是患者本人",
            "UNKNOWN_CONTEXT": "当前句不足以确认主体或断言",
        }
        return {
            "type": "coreference" if self.is_coreference else "direct_mention",
            "antecedent_sentence": self.antecedent,
            "referring_sentence": self.reference if self.is_coreference else "",
            "subject": self.subject,
            "assertion": self.category,
            "time_status": self.temporal_status,
            "time_reason": self.temporal_reason,
            "final_reason": (
                f"{assertion_reasons.get(self.category, '当前句语义未确定')}；"
                f"{self.temporal_reason}"
            ),
        }


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _shortest_ordered_span(keyword: str, text: str) -> tuple[int, int] | None:
    """Find a compact ordered span, allowing one missing char for long terms."""
    if not keyword or not text:
        return None
    variants = [keyword]
    if len(keyword) >= 3:
        variants.extend(keyword[:idx] + keyword[idx + 1 :] for idx in range(len(keyword)))

    best = None
    max_width = max(12, len(keyword) * 4)
    for variant in dict.fromkeys(variants):
        if len(variant) < 2:
            continue
        starts = (m.start() for m in re.finditer(re.escape(variant[0]), text, re.IGNORECASE))
        for start in starts:
            pos = start + 1
            end = start + 1
            found = True
            for char in variant[1:]:
                match = re.search(re.escape(char), text[pos:], re.IGNORECASE)
                if not match:
                    found = False
                    break
                end = pos + match.end()
                pos = end
            if not found or end - start > max_width:
                continue
            candidate = (start, end)
            if best is None or candidate[1] - candidate[0] < best[1] - best[0]:
                best = candidate
    return best


def _find_mentions(keyword: str, sentence: str) -> list[tuple[int, int]]:
    exact = [match.span() for match in re.finditer(re.escape(keyword), sentence, re.IGNORECASE)]
    if exact:
        return exact
    fuzzy = _shortest_ordered_span(keyword, sentence)
    return [fuzzy] if fuzzy else []


def _nearest_match(pattern: re.Pattern, text: str) -> re.Match | None:
    matches = list(pattern.finditer(text))
    return matches[-1] if matches else None


def _marker_has_crossed_clause_boundary(
    marker: re.Match | None,
    left: str,
) -> bool:
    """Keep assertion cues inside their own clause; retain enumeration scope."""
    return bool(marker and _CLAUSE_BOUNDARY_RE.search(left[marker.end() :]))


def _classify_mention(sentence: str, start: int, end: int, *, query: str) -> str:
    left = sentence[max(0, start - 32) : start]
    right = sentence[end : min(len(sentence), end + 24)]
    local = sentence[max(0, start - 32) : min(len(sentence), end + 24)]

    subject_match = _nearest_match(_NON_PATIENT_TERMS_RE, left)
    query_requests_non_patient_subject = bool(_NON_PATIENT_TERMS_RE.search(query))
    if subject_match and not query_requests_non_patient_subject:
        after_subject = left[subject_match.end() :]
        if not _PATIENT_TERMS_RE.search(after_subject):
            return "NON_PATIENT_SUBJECT"

    excluded_before = _nearest_match(_EXCLUDED_TERMS_RE, left)
    if (
        excluded_before
        and len(left) - excluded_before.end() <= 12
        and not _marker_has_crossed_clause_boundary(excluded_before, left)
    ):
        return "EXCLUDED"
    if re.match(r".{0,6}(?:已排除|予以排除|不支持|未支持)", right):
        return "EXCLUDED"

    negation_match = _nearest_match(_NEGATION_TERMS_RE, left)
    if (
        negation_match
        and len(left) - negation_match.end() <= 20
        and not _marker_has_crossed_clause_boundary(negation_match, left)
    ):
        between = left[negation_match.end() :]
        blockers = (
            r"诱因|原因|但|然而|后(?:出现|发生)|转而|现(?:出现|有)|"
            r"住院第\s*[零〇一二两三四五六七八九十百千万亿\d]+\s*[天日].{0,8}(?:出现|发生)|"
            r"(?:入院|出院)后.{0,8}(?:出现|发生)"
        )
        if not re.search(blockers, between):
            return "NEGATED"
    no_match = _nearest_match(re.compile(r"无"), left)
    if (
        no_match
        and len(left) - no_match.end() <= 16
        and not _marker_has_crossed_clause_boundary(no_match, left)
    ):
        between = left[no_match.end() :]
        blockers = (
            r"诱因|原因|外伤|但|然而|后(?:出现|发生)|转而|现(?:出现|有)|"
            r"住院第\s*[零〇一二两三四五六七八九十百千万亿\d]+\s*[天日].{0,8}(?:出现|发生)|"
            r"(?:入院|出院)后.{0,8}(?:出现|发生)"
        )
        if not re.search(blockers, between):
            return "NEGATED"
    if re.match(r".{0,6}(?:阴性|否认|不存在)", right):
        return "NEGATED"

    uncertain_match = _nearest_match(_UNCERTAIN_TERMS_RE, left)
    if not _UNCERTAIN_QUERY_RE.search(query):
        if (
            uncertain_match
            and len(left) - uncertain_match.end() <= 16
            and not _marker_has_crossed_clause_boundary(uncertain_match, left)
        ):
            return "UNCERTAIN"
        if re.match(r".{0,8}(?:待排|待除外|\?|？|可能|可疑)", right):
            return "UNCERTAIN"

    if _RESOLVED_TERMS_RE.search(right) and not _HISTORY_QUERY_RE.search(query):
        return "RESOLVED_HISTORY"

    current_positive = _nearest_match(_CURRENT_POSITIVE_TERMS_RE, left)
    if (
        current_positive
        and len(left) - current_positive.end() <= 18
        and not _marker_has_crossed_clause_boundary(current_positive, left)
    ):
        return "POSITIVE"

    history_match = _nearest_match(_HISTORY_TERMS_RE, left)
    if (
        history_match
        and len(left) - history_match.end() <= 18
        and not _marker_has_crossed_clause_boundary(history_match, left)
        and not _HISTORY_QUERY_RE.search(query)
    ):
        return "HISTORY_ONLY"

    if _PATIENT_TERMS_RE.search(local) or not subject_match or query_requests_non_patient_subject:
        return "POSITIVE"
    return "UNKNOWN_CONTEXT"


def _normalize_anchor(value: str) -> str:
    value = re.sub(r"^(?:患者|病人|本人|于|在)+", "", str(value or "").strip())
    return "手术" if value in {"术", "手术"} else value


def _relative_time_spec(text: str, *, expected_anchor: str = "") -> dict | None:
    source = text or ""
    matched = None
    amount_first = False
    if expected_anchor:
        anchor_pattern = re.escape(expected_anchor)
        matched = re.search(
            rf"(?P<anchor>{anchor_pattern})\s*(?P<direction>前|后)\s*第?\s*"
            r"(?P<amount>\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)?\s*"
            r"(?P<unit>分钟|小时|天|日|周|月|个月)?\s*(?P<within>内)?",
            source,
        )
    if not matched:
        matched = _RELATIVE_TIME_RE.search(source)
    if not matched:
        matched = _AMOUNT_FIRST_TIME_RE.search(source)
        amount_first = matched is not None
    if not matched and not expected_anchor:
        matched = _GENERIC_RELATIVE_TIME_RE.search(source)
    if not matched:
        return None

    direction = matched.group("direction")
    if amount_first and direction == "内":
        direction = "后"
    amount_text = matched.group("amount") or ""
    unit_text = matched.group("unit") or ""
    amount_hours = None
    if amount_text:
        from microharness.medical.temporal_parser import (
            convert_numeric_unit,
            normalize_time_unit,
            parse_cn_number,
        )

        amount = parse_cn_number(amount_text)
        if amount is not None:
            unit = normalize_time_unit(unit_text or "天")
            amount_hours = convert_numeric_unit(float(amount), unit, "小时")
    raw_anchor = _normalize_anchor(matched.group("anchor"))
    return {
        "anchor": raw_anchor,
        "direction": direction,
        "amount_hours": amount_hours,
        "within": bool(matched.groupdict().get("within")) or (amount_first and matched.group("direction") == "内"),
    }


def _assess_temporal_context(
    sentence: str,
    start: int,
    end: int,
    *,
    condition: str,
    time_window: TimeWindow | None,
    record_time: datetime | None,
) -> tuple[str, str]:
    if not time_window or not time_window.required:
        return _TEMPORAL_MATCHED, "查询未要求文档时间窗"
    if not time_window.resolved:
        return _TEMPORAL_UNKNOWN, f"缺少{time_window.source or time_window.scope}时间锚点"

    local = sentence[max(0, start - 48) : min(len(sentence), end + 48)]
    local_datetimes = parse_datetime_values(local)
    if local_datetimes:
        if any(time_window.contains(value) for value in local_datetimes):
            return _TEMPORAL_MATCHED, f"局部文本日期位于{time_window.describe()}"
        return _TEMPORAL_OUTSIDE, f"局部文本日期不在{time_window.describe()}"

    if time_window.scope == "住院期间":
        if _OUTSIDE_INPATIENT_CONTEXT_RE.search(local):
            return _TEMPORAL_OUTSIDE, "局部文本明确位于住院时间窗之外"
        if _INPATIENT_CONTEXT_RE.search(local):
            return _TEMPORAL_MATCHED, "局部文本明确描述住院期间事件"

    query_relative = _relative_time_spec(condition)
    mention_relative = _relative_time_spec(
        local,
        expected_anchor=str((query_relative or {}).get("anchor") or ""),
    )
    if query_relative:
        if not mention_relative:
            return _TEMPORAL_UNKNOWN, "实体局部文本缺少可核验的相对时间"
        if mention_relative["anchor"] != query_relative["anchor"]:
            return _TEMPORAL_UNKNOWN, "实体局部文本的时间锚点与查询锚点不能对应"
        if mention_relative["direction"] != query_relative["direction"]:
            return _TEMPORAL_OUTSIDE, "实体局部文本位于查询锚点的相反时间方向"
        query_hours = query_relative.get("amount_hours")
        mention_hours = mention_relative.get("amount_hours")
        if query_hours is None:
            return _TEMPORAL_MATCHED, "实体局部文本与查询的相对时间方向一致"
        if mention_hours is None:
            return _TEMPORAL_UNKNOWN, "实体局部文本缺少可比较的时间偏移量"
        if mention_relative.get("within") and mention_hours > query_hours:
            return _TEMPORAL_UNKNOWN, "实体局部文本的时间范围宽于查询范围，无法确认具体发生时间"
        if mention_hours <= query_hours:
            return _TEMPORAL_MATCHED, "实体局部文本的时间偏移位于查询范围内"
        return _TEMPORAL_OUTSIDE, "实体局部文本的时间偏移超出查询范围"

    if record_time is not None:
        if time_window.contains(record_time):
            return _TEMPORAL_MATCHED, f"文档记录时间{record_time:%Y-%m-%d %H:%M:%S}位于目标时间窗内"
        return _TEMPORAL_OUTSIDE, f"文档记录时间{record_time:%Y-%m-%d %H:%M:%S}不在目标时间窗内"

    return _TEMPORAL_UNKNOWN, "实体局部文本和文档字段均缺少可比较的记录时间"


def _segment_document(text: str) -> list[_DocumentSentence]:
    """Split text without allowing coreference to leak across field boundaries."""
    sentences: list[_DocumentSentence] = []
    section = 0
    paragraph = 0
    sentence_index = 0
    blocks = re.split(r"\n\s*\n+", text)
    for block in blocks:
        for raw_line in block.splitlines() or [block]:
            line = raw_line.strip()
            if not line:
                continue
            paragraph += 1
            prefix = _SECTION_PREFIX_RE.match(line)
            if prefix:
                section += 1
                line = prefix.group("body").strip()
            for raw_sentence in _INLINE_SENTENCE_SPLIT_RE.split(line):
                sentence = raw_sentence.strip()
                if not sentence:
                    continue
                sentences.append(
                    _DocumentSentence(
                        text=sentence,
                        section=section,
                        paragraph=paragraph,
                        index=sentence_index,
                    )
                )
                sentence_index += 1
    return sentences


def _has_competing_medical_entity(sentence: str, keyword: str) -> bool:
    """Detect coordinated clinical concepts without maintaining entity dictionaries."""
    coordinated = re.compile(
        rf"(?:{re.escape(keyword)}.{{0,6}}(?:及|和|与|、).{{1,16}}|"
        rf".{{1,16}}(?:及|和|与|、).{{0,6}}{re.escape(keyword)})"
    )
    if coordinated.search(sentence):
        return True
    entity_pattern = re.compile(
        r"[\u4e00-\u9fffA-Za-z0-9]{1,12}?(?:疼痛|痛|不适|异常|增高|降低|偏高|偏低|"
        r"炎|病|症|癌|瘤|息肉|狭窄|损伤|骨折|感染)"
    )
    candidates = [match.group(0) for match in entity_pattern.finditer(sentence)]
    if len(candidates) < 2:
        return False
    has_target = any(_find_mentions(keyword, candidate) for candidate in candidates)
    return has_target and len(set(candidates)) > 1


def _contains_other_medical_entity(sentence: str, keyword: str) -> bool:
    if _find_mentions(keyword, sentence):
        return _has_competing_medical_entity(sentence, keyword)
    return bool(
        re.search(
            r"[\u4e00-\u9fffA-Za-z0-9]{1,12}?(?:疼痛|痛|不适|异常|增高|降低|偏高|偏低|"
            r"炎|病|症|癌|瘤|息肉|狭窄|损伤|骨折|感染)",
            sentence,
        )
    )


def _classify_reference(
    sentence: str,
    reference_span: tuple[int, int],
    *,
    antecedent_category: str,
    query: str,
) -> tuple[str, str]:
    local = sentence[max(0, reference_span[0] - 32) : min(len(sentence), reference_span[1] + 48)]
    non_patient = _nearest_match(_NON_PATIENT_TERMS_RE, local)
    patient = _PATIENT_TERMS_RE.search(local)
    subject = "patient"
    if non_patient and not _NON_PATIENT_TERMS_RE.search(query):
        after_non_patient = local[non_patient.end() :]
        if not _PATIENT_TERMS_RE.search(after_non_patient):
            return "NON_PATIENT_SUBJECT", "non_patient"
    if antecedent_category == "NON_PATIENT_SUBJECT" and not patient:
        return "NON_PATIENT_SUBJECT", "non_patient"

    if _REFERENCE_EXCLUDED_RE.search(local):
        return "EXCLUDED", subject
    if _REFERENCE_RESOLVED_RE.search(local):
        return "RESOLVED_HISTORY", subject
    if _REFERENCE_NEGATION_RE.search(local):
        return "NEGATED", subject

    uncertain = _UNCERTAIN_TERMS_RE.search(local) or re.search(r"尚未明确|仍未明确|不能明确", local)
    query_accepts_uncertainty = bool(_UNCERTAIN_QUERY_RE.search(query))
    if uncertain and not query_accepts_uncertainty:
        return "UNCERTAIN", subject

    explicitly_confirmed = re.search(r"确诊|明确诊断|证实|确认", local)
    if antecedent_category == "UNCERTAIN" and not explicitly_confirmed and not query_accepts_uncertainty:
        return "UNCERTAIN", subject
    if _HISTORY_TERMS_RE.search(local) and not _HISTORY_QUERY_RE.search(query):
        return "HISTORY_ONLY", subject
    if _REFERENCE_POSITIVE_RE.search(local) or explicitly_confirmed:
        return "POSITIVE", subject
    return "UNKNOWN_CONTEXT", subject


def _collect_document_mentions(
    keyword: str,
    text: str,
    *,
    condition: str,
    time_window: TimeWindow | None,
    record_time: datetime | None,
) -> tuple[list[_DocumentMention], tuple[dict, ...], str, str]:
    sentence_mentions: dict[int, list[_DocumentMention]] = {}
    sentence_metadata: dict[int, _DocumentSentence] = {}
    all_mentions: list[_DocumentMention] = []
    sentences = _segment_document(text)

    for sentence_item in sentences:
        sentence_metadata[sentence_item.index] = sentence_item
        for span in _find_mentions(keyword, sentence_item.text):
            category = _classify_mention(sentence_item.text, span[0], span[1], query=condition)
            temporal_status, temporal_reason = _assess_temporal_context(
                sentence_item.text,
                span[0],
                span[1],
                condition=condition,
                time_window=time_window,
                record_time=record_time,
            )
            mention = _DocumentMention(
                category=category,
                sentence=sentence_item.text,
                temporal_status=temporal_status,
                temporal_reason=temporal_reason,
                sentence_index=sentence_item.index,
                subject="non_patient" if category == "NON_PATIENT_SUBJECT" else "patient",
            )
            sentence_mentions.setdefault(sentence_item.index, []).append(mention)
            all_mentions.append(mention)

    coreferences: list[_DocumentMention] = []
    superseded_sentences: set[str] = set()
    unresolved_traces: list[dict] = []
    for sentence_item in sentences:
        for reference_match in _REFERENCE_RE.finditer(sentence_item.text):
            candidates: list[tuple[_DocumentSentence, _DocumentMention]] = []
            for candidate_index in range(
                max(0, sentence_item.index - _COREFERENCE_MAX_SENTENCE_DISTANCE),
                sentence_item.index,
            ):
                candidate_sentence = sentence_metadata.get(candidate_index)
                if not candidate_sentence:
                    continue
                if (
                    candidate_sentence.section != sentence_item.section
                    or candidate_sentence.paragraph != sentence_item.paragraph
                ):
                    continue
                for mention in sentence_mentions.get(candidate_index, []):
                    candidates.append((candidate_sentence, mention))

            if not candidates:
                prior_target_mentions = [
                    candidate_index for candidate_index in sentence_mentions
                    if candidate_index < sentence_item.index
                ]
                if not prior_target_mentions:
                    continue
                unresolved_traces.append(
                    {
                        "type": "unresolved_coreference",
                        "antecedent_sentence": "",
                        "referring_sentence": sentence_item.text,
                        "subject": "unknown",
                        "assertion": "UNKNOWN_CONTEXT",
                        "time_status": _TEMPORAL_UNKNOWN,
                        "time_reason": "同段落有限句距内没有唯一先行词",
                        "final_reason": "缺少唯一先行词，拒绝跨边界继承",
                    }
                )
                continue

            nearest_index = max(candidate[0].index for candidate in candidates)
            nearest = [candidate for candidate in candidates if candidate[0].index == nearest_index]
            antecedent_sentence = nearest[0][0]
            antecedent_categories = {candidate[1].category for candidate in nearest}
            intervening_sentences = [
                item.text for item in sentences
                if antecedent_sentence.index < item.index < sentence_item.index
                and item.section == sentence_item.section
                and item.paragraph == sentence_item.paragraph
            ]
            has_intervening_entity = any(
                _contains_other_medical_entity(item, keyword)
                for item in intervening_sentences
            )
            if (
                len(antecedent_categories) != 1
                or _has_competing_medical_entity(antecedent_sentence.text, keyword)
                or has_intervening_entity
            ):
                ambiguity_trace = {
                    "type": "ambiguous_coreference",
                    "antecedent_sentence": antecedent_sentence.text,
                    "referring_sentence": sentence_item.text,
                    "subject": "unknown",
                    "assertion": "UNKNOWN_CONTEXT",
                    "time_status": _TEMPORAL_UNKNOWN,
                    "time_reason": "先行词上下文存在多个候选医学实体或断言冲突",
                    "final_reason": "存在多个候选实体或冲突断言，无法唯一解析指代",
                }
                trace = (
                    tuple(item.trace_item() for item in all_mentions + coreferences)
                    + tuple(unresolved_traces)
                    + (ambiguity_trace,)
                )
                return all_mentions, trace, "DOCUMENT_COREFERENCE_AMBIGUOUS", (
                    f"引用句'{sentence_item.text[:80]}'之前存在多个可能的医学实体或冲突先行词，无法唯一解析指代"
                )

            antecedent = nearest[0][1]
            category, subject = _classify_reference(
                sentence_item.text,
                reference_match.span(),
                antecedent_category=antecedent.category,
                query=condition,
            )
            temporal_status, temporal_reason = _assess_temporal_context(
                sentence_item.text,
                reference_match.start(),
                reference_match.end(),
                condition=condition,
                time_window=time_window,
                record_time=record_time,
            )
            if temporal_status == _TEMPORAL_UNKNOWN and antecedent.temporal_status != _TEMPORAL_UNKNOWN:
                temporal_status = antecedent.temporal_status
                temporal_reason = f"继承同段落唯一先行词时间语境：{antecedent.temporal_reason}"
            coreference = _DocumentMention(
                category=category,
                sentence=sentence_item.text,
                temporal_status=temporal_status,
                temporal_reason=temporal_reason,
                sentence_index=sentence_item.index,
                is_coreference=True,
                antecedent=antecedent_sentence.text,
                reference=sentence_item.text,
                subject=subject,
            )
            coreferences.append(coreference)
            sentence_mentions.setdefault(sentence_item.index, []).append(coreference)
            superseded_sentences.add(antecedent_sentence.index)

    effective_mentions = [
        mention for mention in all_mentions + coreferences
        if mention.sentence_index not in superseded_sentences
    ]
    trace = tuple(item.trace_item() for item in all_mentions + coreferences) + tuple(unresolved_traces)
    if unresolved_traces:
        return effective_mentions, trace, "DOCUMENT_COREFERENCE_UNRESOLVED", (
            "文档存在指代表达，但同段落有限句距内没有唯一先行词；为避免跨段、跨章节或超距继承，无法确认"
        )
    return effective_mentions, trace, "", ""


def assess_document_semantics(
    keyword: str,
    text: str,
    *,
    condition: str = "",
    time_window: TimeWindow | None = None,
    record_time: datetime | None = None,
) -> DocumentSemanticDecision:
    """Classify free-text evidence around an entity using strict four-state rules."""
    normalized_keyword = _normalize(keyword)
    normalized_text = _normalize(text)
    normalized_condition = _normalize(condition)
    if not normalized_keyword or not normalized_text:
        return DocumentSemanticDecision(
            NO_DECISION,
            "缺少文档语义判断所需的关键词或文本",
            "DOCUMENT_SEMANTICS_INPUT_MISSING",
        )

    mentions, trace, coreference_reason_code, coreference_reason = _collect_document_mentions(
        normalized_keyword,
        normalized_text,
        condition=normalized_condition,
        time_window=time_window,
        record_time=record_time,
    )

    if coreference_reason_code:
        return DocumentSemanticDecision(
            UNKNOWN,
            coreference_reason,
            coreference_reason_code,
            trace[-1].get("referring_sentence", "") if trace else "",
            tuple(item.category for item in mentions),
            trace,
        )

    if not mentions:
        return DocumentSemanticDecision(
            NOT_MENTIONED,
            f"未找到与'{normalized_keyword}'构成同一局部语义片段的文本",
            "DOCUMENT_LOCAL_MENTION_NOT_FOUND",
        )

    categories = tuple(item.category for item in mentions)
    first_evidence = mentions[0].evidence
    positive = [item for item in mentions if item.category == "POSITIVE"]
    if time_window and time_window.required:
        positive_in_window = [item for item in positive if item.temporal_status == _TEMPORAL_MATCHED]
        if positive_in_window:
            return DocumentSemanticDecision(
                MATCHED,
                f"文档局部语境明确陈述患者存在'{normalized_keyword}'相关情况，且{positive_in_window[0].temporal_reason}",
                "DOCUMENT_POSITIVE_ASSERTION",
                positive_in_window[0].evidence,
                categories,
                trace,
            )

        positive_time_unknown = [item for item in positive if item.temporal_status == _TEMPORAL_UNKNOWN]
        if positive_time_unknown:
            return DocumentSemanticDecision(
                UNKNOWN,
                f"文档提及患者存在'{normalized_keyword}'相关情况，但{positive_time_unknown[0].temporal_reason}，无法确认是否满足{time_window.scope}",
                "DOCUMENT_MENTION_TIME_UNKNOWN",
                positive_time_unknown[0].evidence,
                categories,
                trace,
            )

        decisive_negative = {"NEGATED", "EXCLUDED", "RESOLVED_HISTORY"}
        negative_in_window = [
            item for item in mentions
            if item.category in decisive_negative and item.temporal_status == _TEMPORAL_MATCHED
        ]
        if negative_in_window:
            category = negative_in_window[0].category
            if category == "NEGATED":
                reason = f"文档在{time_window.scope}内明确否认患者存在'{normalized_keyword}'相关情况"
                reason_code = "DOCUMENT_EXPLICIT_NEGATION"
            elif category == "EXCLUDED":
                reason = f"文档在{time_window.scope}内显示'{normalized_keyword}'已被排除或不支持"
                reason_code = "DOCUMENT_EXCLUDED_ASSERTION"
            else:
                reason = f"文档在{time_window.scope}内仅记录'{normalized_keyword}'既往存在且已缓解或消失"
                reason_code = "DOCUMENT_RESOLVED_HISTORY"
            return DocumentSemanticDecision(
                NOT_MATCHED,
                reason,
                reason_code,
                negative_in_window[0].evidence,
                categories,
                trace,
            )

        semantic_unknown = {"NON_PATIENT_SUBJECT", "UNCERTAIN", "HISTORY_ONLY", "UNKNOWN_CONTEXT"}
        unknown_in_window = [
            item for item in mentions
            if item.category in semantic_unknown and item.temporal_status != _TEMPORAL_OUTSIDE
        ]
        if unknown_in_window:
            return DocumentSemanticDecision(
                UNKNOWN,
                f"文档在目标时间语境中提及'{normalized_keyword}'，但主体或确定性不足，无法确认",
                "DOCUMENT_SEMANTIC_CONFLICT",
                unknown_in_window[0].evidence,
                categories,
                trace,
            )

        positive_outside = [item for item in positive if item.temporal_status == _TEMPORAL_OUTSIDE]
        if positive_outside:
            return DocumentSemanticDecision(
                NOT_MATCHED,
                f"文档提及患者存在'{normalized_keyword}'相关情况，但{positive_outside[0].temporal_reason}，不满足{time_window.scope}",
                "DOCUMENT_TIME_OUTSIDE_WINDOW",
                positive_outside[0].evidence,
                categories,
                trace,
            )

        temporal_unknown = [item for item in mentions if item.temporal_status == _TEMPORAL_UNKNOWN]
        if temporal_unknown:
            return DocumentSemanticDecision(
                UNKNOWN,
                f"文档提及'{normalized_keyword}'，但{temporal_unknown[0].temporal_reason}，无法确认是否满足{time_window.scope}",
                "DOCUMENT_MENTION_TIME_UNKNOWN",
                temporal_unknown[0].evidence,
                categories,
                trace,
            )

        return DocumentSemanticDecision(
            UNKNOWN,
            f"文档中关于'{normalized_keyword}'的内容缺少与{time_window.scope}对应的有效时间证据",
            "DOCUMENT_TIME_CONTEXT_INSUFFICIENT",
            first_evidence,
            categories,
            trace,
        )

    if positive:
        return DocumentSemanticDecision(
            MATCHED,
            f"文档局部语境明确陈述患者存在'{normalized_keyword}'相关情况",
            "DOCUMENT_POSITIVE_ASSERTION",
            positive[0].evidence,
            categories,
            trace,
        )

    decisive_negative = {"NEGATED", "EXCLUDED", "RESOLVED_HISTORY"}
    unknown_categories = {"UNCERTAIN", "HISTORY_ONLY", "UNKNOWN_CONTEXT"}
    category_set = set(categories)
    if category_set <= decisive_negative:
        if category_set == {"NEGATED"}:
            reason = f"文档局部语境明确否认患者存在'{normalized_keyword}'相关情况"
            reason_code = "DOCUMENT_EXPLICIT_NEGATION"
        elif category_set == {"EXCLUDED"}:
            reason = f"文档局部语境显示'{normalized_keyword}'已被排除或不支持"
            reason_code = "DOCUMENT_EXCLUDED_ASSERTION"
        else:
            reason = f"文档仅记录'{normalized_keyword}'既往存在且当前已缓解或消失"
            reason_code = "DOCUMENT_RESOLVED_HISTORY"
        return DocumentSemanticDecision(
            NOT_MATCHED,
            reason,
            reason_code,
            first_evidence,
            categories,
            trace,
        )

    if category_set == {"NON_PATIENT_SUBJECT"}:
        return DocumentSemanticDecision(
            NOT_MATCHED,
            f"文档提及'{normalized_keyword}'，但该内容属于家属或其他非患者主体，不满足患者本人条件",
            "DOCUMENT_NON_PATIENT_SUBJECT",
            first_evidence,
            categories,
            trace,
        )

    if category_set & unknown_categories or len(category_set) > 1:
        if category_set == {"UNCERTAIN"}:
            reason = f"文档仅以疑似、考虑或待排语气提及'{normalized_keyword}'，无法确认"
            reason_code = "DOCUMENT_UNCERTAIN_ASSERTION"
        elif category_set == {"HISTORY_ONLY"}:
            reason = f"文档仅在既往或历史语境中提及'{normalized_keyword}'，无法判断当前是否存在"
            reason_code = "DOCUMENT_HISTORY_CONTEXT"
        else:
            reason = f"文档中关于'{normalized_keyword}'的主体、确定性或时态证据存在冲突，无法判断"
            reason_code = "DOCUMENT_SEMANTIC_CONFLICT"
        return DocumentSemanticDecision(
            UNKNOWN,
            reason,
            reason_code,
            first_evidence,
            categories,
            trace,
        )

    return DocumentSemanticDecision(
        UNKNOWN,
        f"文档提及'{normalized_keyword}'，但局部语义不足以确认患者本人当前存在",
        "DOCUMENT_SEMANTICS_INSUFFICIENT",
        first_evidence,
        categories,
        trace,
    )
