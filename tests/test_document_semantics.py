from datetime import datetime

import pytest

from microharness.medical.document_semantics import (
    MATCHED,
    NOT_MATCHED,
    NOT_MENTIONED,
    NO_DECISION,
    UNKNOWN,
    assess_document_semantics,
)
from microharness.medical.structured_time import first_labeled_record_time_from_bindings
from microharness.medical.time_window import TimeWindow


def test_positive_patient_assertion_is_matched():
    result = assess_document_semantics("背痛", "患者诉胸背部疼痛3天。", condition="背痛")

    assert result.status == MATCHED
    assert result.reason_code == "DOCUMENT_POSITIVE_ASSERTION"


def test_explicit_negation_is_not_matched():
    result = assess_document_semantics("背痛", "患者否认胸痛、背痛及腹痛。", condition="背痛")

    assert result.status == NOT_MATCHED
    assert result.reason_code == "DOCUMENT_EXPLICIT_NEGATION"


def test_enumeration_separator_keeps_negation_scope():
    result = assess_document_semantics(
        "\u5934\u6655",
        "\u60a3\u8005\u5426\u8ba4\u5934\u75db\u3001\u5934\u6655\u53ca\u6076\u5fc3\u3002",
        condition="\u5934\u6655",
    )

    assert result.status == NOT_MATCHED
    assert result.reason_code == "DOCUMENT_EXPLICIT_NEGATION"


def test_negation_does_not_cross_independent_clause():
    result = assess_document_semantics(
        "\u5934\u6655",
        "\u60a3\u8005\u5426\u8ba4\u5934\u75db\uff0c\u76ee\u524d\u51fa\u73b0\u5934\u6655\u3002",
        condition="\u5934\u6655",
    )

    assert result.status == MATCHED
    assert result.reason_code == "DOCUMENT_POSITIVE_ASSERTION"


def test_uncertainty_does_not_cross_confirmed_clause():
    result = assess_document_semantics(
        "\u5934\u6655",
        "\u8003\u8651\u5934\u75db\uff0c\u60a3\u8005\u76ee\u524d\u660e\u786e\u6709\u5934\u6655\u3002",
        condition="\u5934\u6655",
    )

    assert result.status == MATCHED
    assert result.reason_code == "DOCUMENT_POSITIVE_ASSERTION"


def test_non_patient_subject_is_not_matched_for_patient_query():
    result = assess_document_semantics("背痛", "患者父亲长期背痛。", condition="背痛")

    assert result.status == NOT_MATCHED
    assert result.reason_code == "DOCUMENT_NON_PATIENT_SUBJECT"


def test_family_report_about_patient_keeps_patient_subject():
    result = assess_document_semantics("背痛", "家属诉患者近两日背痛明显。", condition="背痛")

    assert result.status == MATCHED


def test_explicit_family_history_query_accepts_family_subject():
    result = assess_document_semantics("背痛", "患者父亲长期背痛。", condition="父亲有背痛")

    assert result.status == MATCHED


def test_uncertain_assertion_is_unknown():
    result = assess_document_semantics("背痛", "初步考虑背痛与腰椎病变相关。", condition="背痛")

    assert result.status == UNKNOWN
    assert result.reason_code == "DOCUMENT_UNCERTAIN_ASSERTION"


def test_excluded_assertion_is_not_matched():
    result = assess_document_semantics("背痛", "经检查已排除背痛相关急症。", condition="背痛")

    assert result.status == NOT_MATCHED
    assert result.reason_code == "DOCUMENT_EXCLUDED_ASSERTION"


def test_history_only_is_unknown_for_current_query():
    result = assess_document_semantics("背痛", "既往有背痛病史。", condition="当前背痛")

    assert result.status == UNKNOWN
    assert result.reason_code == "DOCUMENT_HISTORY_CONTEXT"


def test_history_query_accepts_history_evidence():
    result = assess_document_semantics("背痛", "既往有背痛病史。", condition="既往有背痛")

    assert result.status == MATCHED


def test_current_assertion_overrides_history_context():
    result = assess_document_semantics(
        "\u5934\u6655",
        "\u65e2\u5f80\u6709\u5934\u6655\uff0c\u76ee\u524d\u4ecd\u6709\u5934\u6655\u3002",
        condition="\u5f53\u524d\u5934\u6655",
    )

    assert result.status == MATCHED
    assert "POSITIVE" in result.categories


def test_resolved_current_state_overrides_history_context():
    result = assess_document_semantics(
        "\u5934\u6655",
        "\u65e2\u5f80\u6709\u5934\u6655\uff0c\u76ee\u524d\u5df2\u7f13\u89e3\u3002",
        condition="\u5f53\u524d\u5934\u6655",
    )

    assert result.status == NOT_MATCHED
    assert result.reason_code == "DOCUMENT_RESOLVED_HISTORY"


def test_explicit_patient_subject_stops_non_patient_subject_scope():
    result = assess_document_semantics(
        "\u5934\u6655",
        "\u60a3\u8005\u7236\u4eb2\u957f\u671f\u5934\u75db\uff0c\u60a3\u8005\u76ee\u524d\u51fa\u73b0\u5934\u6655\u3002",
        condition="\u5934\u6655",
    )

    assert result.status == MATCHED
    assert result.reason_code == "DOCUMENT_POSITIVE_ASSERTION"


def test_positive_evidence_wins_over_separate_negative_sentence():
    result = assess_document_semantics(
        "背痛",
        "入院时否认背痛。住院第二日患者出现背痛。",
        condition="住院期间背痛",
    )

    assert result.status == MATCHED
    assert set(result.categories) == {"NEGATED", "POSITIVE"}


def test_positive_evidence_wins_over_earlier_negation_in_same_sentence():
    result = assess_document_semantics(
        "背痛",
        "入院时否认背痛，但住院第二日患者出现背痛。",
        condition="住院期间背痛",
    )

    assert result.status == MATCHED
    assert set(result.categories) == {"NEGATED", "POSITIVE"}


def test_scattered_characters_are_not_a_local_mention():
    result = assess_document_semantics(
        "背痛",
        "背部查体未见异常，患者一般情况稳定，数日后诉关节疼痛。",
        condition="背痛",
    )

    assert result.status == NOT_MENTIONED
    assert result.reason_code == "DOCUMENT_LOCAL_MENTION_NOT_FOUND"


def test_no_cause_phrase_does_not_negate_positive_symptom():
    result = assess_document_semantics("背痛", "患者无明显诱因出现背痛。", condition="背痛")

    assert result.status == MATCHED


def test_absolute_mention_date_inside_window_is_matched():
    window = TimeWindow(
        scope="出院后时间窗",
        start=datetime(2026, 6, 10),
        end=datetime(2026, 6, 20, 23, 59, 59),
        source="encounter-info",
        required=True,
    )

    result = assess_document_semantics(
        "背痛",
        "2026-06-15患者出现背痛。",
        condition="出院后10天内背痛",
        time_window=window,
    )

    assert result.status == MATCHED


def test_absolute_mention_date_outside_window_is_not_matched():
    window = TimeWindow(
        scope="出院后时间窗",
        start=datetime(2026, 6, 10),
        end=datetime(2026, 6, 20, 23, 59, 59),
        source="encounter-info",
        required=True,
    )

    result = assess_document_semantics(
        "背痛",
        "2026-07-01患者仍有背痛。",
        condition="出院后10天内背痛",
        time_window=window,
    )

    assert result.status == NOT_MATCHED
    assert result.reason_code == "DOCUMENT_TIME_OUTSIDE_WINDOW"


def test_time_constrained_positive_without_time_evidence_is_unknown():
    window = TimeWindow(
        scope="出院后时间窗",
        start=datetime(2026, 6, 10),
        end=datetime(2026, 6, 20, 23, 59, 59),
        source="encounter-info",
        required=True,
    )

    result = assess_document_semantics(
        "背痛",
        "患者诉背痛。",
        condition="出院后10天内背痛",
        time_window=window,
    )

    assert result.status == UNKNOWN
    assert result.reason_code == "DOCUMENT_MENTION_TIME_UNKNOWN"


def test_unresolved_required_window_keeps_document_result_unknown():
    window = TimeWindow(
        scope="术前48小时",
        source="手术记录",
        required=True,
        reason="缺少手术时间",
    )

    result = assess_document_semantics(
        "背痛",
        "术前24小时患者出现背痛。",
        condition="术前48小时内背痛",
        time_window=window,
    )

    assert result.status == UNKNOWN
    assert result.reason_code == "DOCUMENT_MENTION_TIME_UNKNOWN"


def test_relative_offset_inside_query_window_is_matched():
    window = TimeWindow(
        scope="事件前时间窗",
        start=datetime(2026, 6, 8),
        end=datetime(2026, 6, 10),
        source="手术记录",
        required=True,
    )

    result = assess_document_semantics(
        "背痛",
        "术前24小时患者出现背痛。",
        condition="术前48小时内背痛",
        time_window=window,
    )

    assert result.status == MATCHED


def test_relative_offset_outside_query_window_is_not_matched():
    window = TimeWindow(
        scope="事件前时间窗",
        start=datetime(2026, 6, 8),
        end=datetime(2026, 6, 10),
        source="手术记录",
        required=True,
    )

    result = assess_document_semantics(
        "背痛",
        "术前72小时患者出现背痛。",
        condition="术前48小时内背痛",
        time_window=window,
    )

    assert result.status == NOT_MATCHED
    assert result.reason_code == "DOCUMENT_TIME_OUTSIDE_WINDOW"


def test_custom_event_anchor_uses_same_generic_relative_time_rule():
    window = TimeWindow(
        scope="事件后时间窗",
        start=datetime(2026, 6, 10),
        end=datetime(2026, 6, 17),
        source="custom-event",
        required=True,
    )

    result = assess_document_semantics(
        "背痛",
        "化疗后第3天患者出现背痛。",
        condition="化疗后7天内背痛",
        time_window=window,
    )

    assert result.status == MATCHED


def test_inpatient_relative_context_matches_resolved_encounter_window():
    window = TimeWindow(
        scope="住院期间",
        start=datetime(2026, 6, 8),
        end=datetime(2026, 6, 10),
        source="encounter-info",
        required=True,
    )

    result = assess_document_semantics(
        "背痛",
        "入院时否认背痛，住院第二日患者出现背痛。",
        condition="住院期间背痛",
        time_window=window,
    )

    assert result.status == MATCHED


def test_explicit_labeled_record_time_can_bind_plain_document_statement():
    window = TimeWindow(
        scope="住院期间",
        start=datetime(2026, 6, 8),
        end=datetime(2026, 6, 10, 23, 59, 59),
        source="encounter-info",
        required=True,
    )
    record_time = first_labeled_record_time_from_bindings(
        [
            {"html_field": "记录日期时间", "value": "2026-06-09 08:30:00", "xml_path": "recording_time"},
            {"html_field": "住院病程", "value": "患者诉背痛", "xml_path": "progress_note"},
        ]
    )

    result = assess_document_semantics(
        "背痛",
        "患者诉背痛。",
        condition="住院期间背痛",
        time_window=window,
        record_time=record_time,
    )

    assert record_time == datetime(2026, 6, 9, 8, 30)
    assert result.status == MATCHED


def test_single_unambiguous_coreference_inherits_target_entity():
    result = assess_document_semantics(
        "背痛",
        "患者因腰部背痛入院。该症状持续3天，夜间加重。",
        condition="背痛",
    )

    assert result.status == MATCHED
    assert "先行句" in result.evidence
    assert result.trace[-1]["type"] == "coreference"
    assert result.trace[-1]["antecedent_sentence"] == "患者因腰部背痛入院"
    assert result.trace[-1]["referring_sentence"] == "该症状持续3天,夜间加重"


def test_bounded_coreference_chain_rechecks_each_referring_sentence():
    result = assess_document_semantics(
        "背痛",
        "患者出现背痛。该症状持续3天。其目前仍有加重。",
        condition="背痛",
    )

    assert result.status == MATCHED
    coreference_traces = [item for item in result.trace if item["type"] == "coreference"]
    assert len(coreference_traces) == 2
    assert coreference_traces[-1]["antecedent_sentence"] == "该症状持续3天"


def test_later_negation_in_coreference_chain_supersedes_intermediate_positive():
    result = assess_document_semantics(
        "背痛",
        "患者出现背痛。该症状持续3天。现否认其仍然存在。",
        condition="背痛",
    )

    assert result.status == NOT_MATCHED
    assert result.reason_code == "DOCUMENT_EXPLICIT_NEGATION"
    assert result.categories == ("NEGATED",)
    assert result.trace[-1]["final_reason"].startswith("当前句明确否认目标实体")


def test_unrelated_unresolved_reference_does_not_poison_direct_target_evidence():
    result = assess_document_semantics(
        "背痛",
        "该症状已缓解。患者随后明确出现背痛。",
        condition="背痛",
    )

    assert result.status == MATCHED
    assert result.reason_code == "DOCUMENT_POSITIVE_ASSERTION"


def test_multiple_entities_make_coreference_unknown():
    result = assess_document_semantics(
        "背痛",
        "患者有背痛及腹痛。该症状持续3天。",
        condition="背痛",
    )

    assert result.status == UNKNOWN
    assert result.reason_code == "DOCUMENT_COREFERENCE_AMBIGUOUS"
    assert result.trace[-1]["type"] == "ambiguous_coreference"


def test_intervening_medical_entity_makes_coreference_unknown():
    result = assess_document_semantics(
        "背痛",
        "患者有背痛。随后出现腹痛。该症状持续3天。",
        condition="背痛",
    )

    assert result.status == UNKNOWN
    assert result.reason_code == "DOCUMENT_COREFERENCE_AMBIGUOUS"


def test_coreference_rechecks_family_subject_switch():
    result = assess_document_semantics(
        "背痛",
        "患者有背痛。患者母亲称该症状持续3天。",
        condition="背痛",
    )

    assert result.status == NOT_MATCHED
    assert result.reason_code == "DOCUMENT_NON_PATIENT_SUBJECT"
    assert result.trace[-1]["subject"] == "non_patient"


def test_coreference_keeps_non_patient_antecedent_without_explicit_subject_switch():
    result = assess_document_semantics(
        '\u80cc\u75db',
        '\u60a3\u8005\u7236\u4eb2\u6709\u80cc\u75db\u3002\u8be5\u75c7\u72b6\u6301\u7eed3\u5929\u3002',
        condition='\u80cc\u75db',
    )

    assert result.status == NOT_MATCHED
    assert result.reason_code == 'DOCUMENT_NON_PATIENT_SUBJECT'
    assert result.trace[-1]['subject'] == 'non_patient'


def test_coreference_allows_explicit_switch_from_family_to_patient():
    result = assess_document_semantics(
        '\u80cc\u75db',
        '\u60a3\u8005\u7236\u4eb2\u6709\u80cc\u75db\u3002\u60a3\u8005\u76ee\u524d\u4e5f\u51fa\u73b0\u8be5\u75c7\u72b6\u3002',
        condition='\u80cc\u75db',
    )

    assert result.status == MATCHED
    assert result.reason_code == 'DOCUMENT_POSITIVE_ASSERTION'
    assert result.trace[-1]['subject'] == 'patient'


def test_referenced_negation_overrides_earlier_positive_assertion():
    result = assess_document_semantics(
        "背痛",
        "患者有背痛。现否认该症状。",
        condition="背痛",
    )

    assert result.status == NOT_MATCHED
    assert result.reason_code == "DOCUMENT_EXPLICIT_NEGATION"
    assert result.categories == ("NEGATED",)


def test_uncertain_antecedent_stays_unknown_through_reference():
    result = assess_document_semantics(
        "腰椎病变",
        "考虑腰椎病变。其目前尚未明确。",
        condition="腰椎病变",
    )

    assert result.status == UNKNOWN
    assert result.reason_code == "DOCUMENT_UNCERTAIN_ASSERTION"
    assert result.trace[-1]["assertion"] == "UNCERTAIN"


def test_inpatient_referenced_onset_rechecks_assertion_and_time():
    window = TimeWindow(
        scope="住院期间",
        start=datetime(2026, 6, 8),
        end=datetime(2026, 6, 10),
        source="encounter-info",
        required=True,
    )

    result = assess_document_semantics(
        "背痛",
        "入院时否认背痛。住院第二日出现该症状，活动后明显。",
        condition="住院期间背痛",
        time_window=window,
    )

    assert result.status == MATCHED
    assert result.categories == ("POSITIVE",)
    assert result.trace[-1]["time_status"] == "TEMPORAL_MATCHED"


@pytest.mark.parametrize(
    "text",
    [
        "患者有背痛。\n\n该症状持续3天。",
        "入院记录：患者有背痛。\n出院记录：该症状持续3天。",
        "患者有背痛。生命体征平稳。饮食正常。该症状持续3天。",
    ],
)
def test_coreference_does_not_cross_paragraph_section_or_distance(text):
    result = assess_document_semantics("背痛", text, condition="背痛")

    assert result.status == UNKNOWN
    assert result.reason_code == "DOCUMENT_COREFERENCE_UNRESOLVED"
    assert result.trace[-1]["type"] == "unresolved_coreference"


def test_coreference_inherits_unique_antecedent_time_context_when_reference_has_none():
    window = TimeWindow(
        scope="出院后时间窗",
        start=datetime(2026, 6, 10),
        end=datetime(2026, 6, 20, 23, 59, 59),
        source="encounter-info",
        required=True,
    )

    result = assess_document_semantics(
        "背痛",
        "2026-07-01患者出现背痛。该症状持续3天。",
        condition="出院后10天内背痛",
        time_window=window,
    )

    assert result.status == NOT_MATCHED
    assert result.reason_code == "DOCUMENT_TIME_OUTSIDE_WINDOW"
    assert "继承同段落唯一先行词时间语境" in result.trace[-1]["time_reason"]
