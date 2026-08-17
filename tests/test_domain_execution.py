from datetime import datetime
from types import SimpleNamespace

from microharness.medical.domain_execution import (
    ConditionSemanticType,
    DomainExecutionRequest,
    EvidenceCapability,
    execute_document_domain,
    execute_numeric_domain,
    execute_recalled_document_domain,
    execute_structured_domain,
    normalize_domain_result,
    resolve_condition_semantic_type,
    resolve_evidence_requirement,
)
from microharness.medical.evidence import (
    DataQuality,
    EvidenceRole,
    EvidenceStatus,
    EvidenceUncertaintyKind,
)
from microharness.medical.semantic_entity_recall import (
    aggregate_semantic_entity_decisions,
    assess_semantic_entity_recall,
    parse_semantic_candidate_batch,
)
from microharness.medical.time_window import TimeWindow


def test_request_is_built_from_execution_spec_without_reparsing_text():
    spec = SimpleNamespace(
        condition_id="c1",
        text="展示文本与执行语义无关",
        domain="laboratory",
        entity_type="lab",
        canonical_entity="血红蛋白",
        entity="",
        keyword="",
        entity_candidates=("血红蛋白", "HGB"),
        predicate="compare",
        modifiers=(),
        numeric_comparison={
            "subject": "血红蛋白",
            "operator": ">",
            "threshold": 120,
            "unit": "g/L",
        },
        numeric_execution_required=True,
        attributes={"source": "ir"},
    )

    request = DomainExecutionRequest.from_execution_spec(spec, [])

    assert request.condition == "展示文本与执行语义无关"
    assert request.entity == "血红蛋白"
    assert request.entity_candidates == ("血红蛋白", "HGB")
    assert request.numeric_comparison["threshold"] == 120
    assert request.is_numeric is True


def test_normalization_preserves_four_state_and_legacy_fields():
    request = DomainExecutionRequest(
        condition_id="c1",
        condition="目标诊断",
        bindings=(),
        domain="diagnosis",
        entity="目标诊断",
    )
    result = normalize_domain_result(
        {
            "applicable": True,
            "matched": False,
            "status": "NOT_MENTIONED",
            "reason_code": "NO_MATCHING_RECORD",
            "reason": "未找到目标诊断",
            "fields": "诊断名称=其他诊断",
            "candidate_count": 0,
            "candidate_records": [],
            "custom_trace": ["kept"],
        },
        request,
        domain="diagnosis",
        executor="diagnosis_rules",
    )

    legacy = result.to_legacy_dict()
    assert result.status == EvidenceStatus.NOT_MENTIONED
    assert result.matched is False
    assert result.data_quality == DataQuality.COMPLETE
    assert legacy["custom_trace"] == ["kept"]
    assert legacy["status"] == "NOT_MENTIONED"


def test_lab_dispatch_declares_numeric_and_temporal_capabilities():
    bindings = [
        {"html_field": "[检验1] 化验项目描述", "eng_field": "inspItemDesc", "value": "血红蛋白"},
        {"html_field": "[检验1] 结果", "eng_field": "inspectionValue", "value": "130"},
        {"html_field": "[检验1] 单位", "eng_field": "inspResultUnitCode", "value": "g/L"},
    ]
    request = DomainExecutionRequest(
        condition_id="c1",
        condition="血红蛋白>120g/L",
        bindings=tuple(bindings),
        domain="laboratory",
        entity_type="lab",
        entity="血红蛋白",
        entity_candidates=("血红蛋白",),
        predicate="compare",
        numeric_comparison={
            "subject": "血红蛋白",
            "operator": ">",
            "threshold": 120,
            "unit": "g/L",
        },
        is_numeric=True,
    )

    result = execute_structured_domain(request)

    assert result is not None
    assert result.domain == "laboratory"
    assert result.status == EvidenceStatus.MATCHED
    assert EvidenceCapability.NUMERIC_VALUE in result.required_capabilities
    assert EvidenceCapability.NUMERIC_VALUE in result.supported_capabilities
    assert result.missing_capabilities == ()


def test_medication_administration_requirement_reports_missing_capability():
    bindings = [
        {"html_field": "[用药1] 医嘱项", "eng_field": "orderName", "value": "阿司匹林肠溶片"},
        {"html_field": "[用药1] 开立日期时间", "eng_field": "orderDateTime", "value": "2026-06-01 10:00:00"},
        {"html_field": "[用药1] 医嘱状态", "eng_field": "ordStatusDesc", "value": "有效"},
    ]
    request = DomainExecutionRequest(
        condition_id="c1",
        condition="使用过阿司匹林",
        bindings=tuple(bindings),
        domain="medication",
        entity_type="drug",
        entity="阿司匹林",
        entity_candidates=("阿司匹林",),
        predicate="administered",
        semantic={
            "domain": "medication",
            "entity_type": "drug",
            "predicate": "administered",
            "evidence_capabilities": {"administered": False},
        },
    )

    result = execute_structured_domain(request)

    assert result is not None
    assert result.domain == "medication"
    assert result.status == EvidenceStatus.UNKNOWN
    assert result.reason_code == "INSUFFICIENT_EVIDENCE"
    assert EvidenceCapability.ADMINISTRATION_EVENT in result.required_capabilities
    assert EvidenceCapability.ADMINISTRATION_EVENT in result.missing_capabilities
    assert EvidenceCapability.ORDER_EVENT in result.supported_capabilities


def test_diagnosis_dispatch_keeps_domain_specific_reason_code_and_candidates():
    bindings = [
        {"html_field": "[诊断1] 诊断名称", "eng_field": "diagnoseName", "value": "背痛"},
        {"html_field": "[诊断1] 诊断类型", "eng_field": "diagTypeDesc", "value": "门诊诊断"},
    ]
    request = DomainExecutionRequest(
        condition_id="c1",
        condition="背痛",
        bindings=tuple(bindings),
        domain="diagnosis",
        entity_type="diagnosis",
        entity="背痛",
        entity_candidates=("背痛",),
        predicate="exists",
        semantic={"domain": "diagnosis", "entity_type": "diagnosis"},
    )

    result = execute_structured_domain(request)
    file_result = result.to_file_result("诊断查询 (1条)") if result else {}

    assert result is not None
    assert result.status == EvidenceStatus.MATCHED
    assert result.reason_code == "DIAGNOSIS_CONFIRMED"
    assert EvidenceCapability.DIAGNOSIS_ASSERTION in result.required_capabilities
    assert file_result["候选记录数"] == 1
    assert file_result["候选记录"][0]["诊断名称"] == "背痛"


def _document_request(*, time_window=None):
    return DomainExecutionRequest(
        condition_id="c-doc",
        condition="\u80cc\u75db",
        bindings=(),
        domain="diagnosis",
        entity_type="diagnosis",
        entity="\u80cc\u75db",
        entity_candidates=("\u80cc\u75db",),
        predicate="exists",
        time_window=time_window,
    )


def test_document_adapter_preserves_positive_assertion_and_semantic_trace():
    result = execute_document_domain(
        _document_request(),
        "\u60a3\u8005\u8bc9\u80f8\u80cc\u90e8\u75bc\u75db3\u5929\u3002",
    )

    assert result is not None
    assert result.status == EvidenceStatus.MATCHED
    assert result.reason_code == "DOCUMENT_POSITIVE_ASSERTION"
    assert EvidenceCapability.DOCUMENT_CONTEXT in result.required_capabilities
    assert EvidenceCapability.SUBJECT_ATTRIBUTION in result.supported_capabilities
    file_result = result.to_file_result("\u5165\u9662\u8bb0\u5f55")
    assert file_result["matched"] is True
    assert file_result["status"] == "MATCHED"
    assert isinstance(file_result["semantic_trace"], list)
    assert file_result["semantic_trace"]


def test_document_adapter_maps_negation_and_absence_to_distinct_states():
    negated = execute_document_domain(
        _document_request(),
        "\u60a3\u8005\u5426\u8ba4\u80cc\u75db\u3002",
    )
    absent = execute_document_domain(
        _document_request(),
        "\u60a3\u8005\u8bc9\u8179\u75db\u3002",
    )

    assert negated is not None
    assert negated.status == EvidenceStatus.NOT_MATCHED
    assert negated.reason_code == "DOCUMENT_EXPLICIT_NEGATION"
    assert absent is not None
    assert absent.status == EvidenceStatus.NOT_MENTIONED
    assert absent.reason_code == "DOCUMENT_LOCAL_MENTION_NOT_FOUND"
    assert absent.data_quality == DataQuality.COMPLETE


def test_document_adapter_reports_missing_temporal_capability_as_unknown():
    window = TimeWindow(
        scope="\u4f4f\u9662\u671f\u95f4",
        start=datetime(2026, 6, 8),
        end=datetime(2026, 6, 10),
        source="encounter-info",
        required=True,
    )

    result = execute_document_domain(
        _document_request(time_window=window),
        "\u60a3\u8005\u8bc9\u80cc\u75db\u3002",
    )

    assert result is not None
    assert result.status == EvidenceStatus.UNKNOWN
    assert result.reason_code == "DOCUMENT_MENTION_TIME_UNKNOWN"
    assert EvidenceCapability.TEMPORAL_OCCURRENCE in result.required_capabilities
    assert EvidenceCapability.TEMPORAL_OCCURRENCE in result.missing_capabilities


def _encounter_request(operator="<", threshold=5, bindings=()):
    return DomainExecutionRequest(
        condition_id="c-encounter",
        condition="\u4f4f\u9662\u5929\u6570\u6bd4\u8f83",
        bindings=tuple(bindings),
        domain="encounter",
        entity_type="duration",
        entity="\u4f4f\u9662\u5929\u6570",
        predicate="compare",
        numeric_comparison={
            "subject": "\u4f4f\u9662\u5929\u6570",
            "operator": operator,
            "threshold": threshold,
            "unit": "\u5929",
        },
        is_numeric=True,
    )


def test_encounter_duration_adapter_preserves_matched_and_not_matched_results():
    hints = (
        "[\u9884\u8ba1\u7b97] \u51fa\u9662\u65e5\u671f\u65f6\u95f4 - "
        "\u5165\u9662\u65e5\u671f\u65f6\u95f4(\u5929) = 2\u5929"
    )

    matched = execute_numeric_domain(_encounter_request("<", 5), hints)
    not_matched = execute_numeric_domain(_encounter_request(">", 5), hints)

    assert matched is not None
    assert matched.status == EvidenceStatus.MATCHED
    assert matched.reason_code == "NUMERIC_CONDITION_MET"
    assert not_matched is not None
    assert not_matched.status == EvidenceStatus.NOT_MATCHED
    assert not_matched.reason_code == "NUMERIC_CONDITION_NOT_MET"
    assert EvidenceCapability.ENCOUNTER_PERIOD in matched.required_capabilities
    assert EvidenceCapability.NUMERIC_VALUE in matched.supported_capabilities
    assert EvidenceCapability.TEMPORAL_OCCURRENCE in matched.supported_capabilities


def test_encounter_duration_adapter_returns_unknown_when_period_is_missing():
    result = execute_numeric_domain(
        _encounter_request(),
        "",
        fields="\u672a\u53d6\u5f97\u5165\u9662\u548c\u51fa\u9662\u65f6\u95f4",
    )

    assert result is not None
    assert result.status == EvidenceStatus.UNKNOWN
    assert result.reason_code == "MISSING_NUMERIC_EVIDENCE"
    assert result.data_quality == DataQuality.PARTIAL
    assert set(result.missing_capabilities) == {
        EvidenceCapability.ENCOUNTER_PERIOD,
        EvidenceCapability.NUMERIC_VALUE,
        EvidenceCapability.TEMPORAL_OCCURRENCE,
    }
    file_result = result.to_file_result("\u5c31\u8bca\u4fe1\u606f\u67e5\u8be2")
    assert file_result["file"] == "\u5c31\u8bca\u4fe1\u606f\u67e5\u8be2"
    assert file_result["matched"] is False
    assert file_result["status"] == "UNKNOWN"
    assert file_result["cot_response"] == ""


def test_encounter_duration_selects_complete_matching_visit_from_patient_scope():
    bindings = (
        {
            "html_field": "[\u5c31\u8bca1] \u5165\u9662\u65e5\u671f\u65f6\u95f4",
            "value": "2026-06-05 09:00:00",
            "record_id": "out-1",
            "record_id_label": "\u5c31\u8bca\u53f7",
            "record_id_field": "hosEncId",
        },
        {
            "html_field": "[\u5c31\u8bca2] \u5165\u9662\u65e5\u671f\u65f6\u95f4",
            "value": "2026-03-03 09:37:22",
            "record_id": "174",
            "record_id_label": "\u5c31\u8bca\u53f7",
            "record_id_field": "hosEncId",
        },
        {
            "html_field": "[\u5c31\u8bca2] \u51fa\u9662\u65e5\u671f\u65f6\u95f4",
            "value": "2026-03-05 12:00:00",
            "record_id": "174",
            "record_id_label": "\u5c31\u8bca\u53f7",
            "record_id_field": "hosEncId",
        },
    )

    result = execute_numeric_domain(_encounter_request("<", 5, bindings), "")

    assert result is not None
    assert result.status == EvidenceStatus.MATCHED
    assert result.reason_code == "NUMERIC_CONDITION_MET"
    legacy = result.to_legacy_dict()
    assert legacy["candidate_count"] == 2
    assert legacy["record_status_counts"] == {"MATCHED": 1, "UNKNOWN": 1}
    assert [item["record_id"] for item in legacy["candidate_records"]] == ["174"]


def test_encounter_duration_does_not_pair_dates_across_visit_records():
    bindings = (
        {
            "html_field": "[\u5c31\u8bca1] \u5165\u9662\u65e5\u671f\u65f6\u95f4",
            "value": "2026-03-03 09:37:22",
            "record_id": "visit-a",
        },
        {
            "html_field": "[\u5c31\u8bca2] \u51fa\u9662\u65e5\u671f\u65f6\u95f4",
            "value": "2026-03-05 12:00:00",
            "record_id": "visit-b",
        },
    )
    cross_record_hint = (
        "[\u9884\u8ba1\u7b97] \u51fa\u9662\u65e5\u671f\u65f6\u95f4 - "
        "\u5165\u9662\u65e5\u671f\u65f6\u95f4(\u5929) = 2\u5929"
    )

    result = execute_numeric_domain(_encounter_request("<", 5, bindings), cross_record_hint)

    assert result is not None
    assert result.status == EvidenceStatus.UNKNOWN
    assert result.reason_code == "MISSING_NUMERIC_EVIDENCE"
    assert result.to_legacy_dict()["candidate_count"] == 2


def test_encounter_duration_complete_nonmatching_records_are_not_blocked_by_incomplete_records():
    bindings = (
        {
            "html_field": "[\u5c31\u8bca1] \u5165\u9662\u65e5\u671f\u65f6\u95f4",
            "value": "2026-06-05 09:00:00",
            "record_id": "out-1",
        },
        {
            "html_field": "[\u5c31\u8bca2] \u5165\u9662\u65e5\u671f\u65f6\u95f4",
            "value": "2026-03-01 09:00:00",
            "record_id": "174",
        },
        {
            "html_field": "[\u5c31\u8bca2] \u51fa\u9662\u65e5\u671f\u65f6\u95f4",
            "value": "2026-03-10 09:00:00",
            "record_id": "174",
        },
    )

    result = execute_numeric_domain(_encounter_request("<", 5, bindings), "")

    assert result is not None
    assert result.status == EvidenceStatus.NOT_MATCHED
    assert result.reason_code == "NUMERIC_CONDITION_NOT_MET"
    assert [item["record_id"] for item in result.to_legacy_dict()["candidate_records"]] == ["174"]


def _history_duration_request():
    return DomainExecutionRequest(
        condition_id="c-history",
        condition="高血压病史不少于10年",
        bindings=(),
        domain="diagnosis",
        entity_type="diagnosis",
        entity="高血压",
        entity_candidates=("高血压",),
        predicate="exists",
        semantic_class="入院前/既往存在",
        modifiers=("高血压病史不少于10年",),
        history_context=True,
    )


def test_semantic_type_and_requirement_are_resolved_from_ir_fields():
    request = _history_duration_request()

    assert resolve_condition_semantic_type(request) == ConditionSemanticType.HISTORY_DURATION
    document_requirement = resolve_evidence_requirement(request, "document")
    diagnosis_requirement = resolve_evidence_requirement(request, "diagnosis")
    assert EvidenceCapability.HISTORY_DURATION in document_requirement.required_capabilities
    assert EvidenceCapability.HISTORY_DURATION in diagnosis_requirement.required_capabilities
    assert document_requirement.acceptable_source_roles == (
        EvidenceRole.PRIMARY,
        EvidenceRole.SUPPORTING,
        EvidenceRole.CANDIDATE,
    )


def test_diagnosis_assertion_cannot_independently_prove_history_duration():
    request = _history_duration_request()
    request = DomainExecutionRequest(
        **{
            **request.__dict__,
            "bindings": (
                {
                    "html_field": "[诊断1] 诊断名称",
                    "eng_field": "diagnoseName",
                    "value": "高血压",
                },
                {
                    "html_field": "[诊断1] 诊断类型",
                    "eng_field": "diagTypeDesc",
                    "value": "出院诊断",
                },
            ),
            "semantic": {"domain": "diagnosis", "entity_type": "diagnosis"},
        }
    )

    result = execute_structured_domain(request)

    assert result is not None
    assert result.status == EvidenceStatus.UNKNOWN
    assert result.reason_code == "MISSING_REQUIRED_CAPABILITY"
    assert EvidenceCapability.HISTORY_DURATION in result.missing_capabilities


def test_document_history_duration_preserves_all_four_states():
    request = _history_duration_request()

    matched = execute_document_domain(request, "既往高血压病史12年，规律服药。")
    not_matched = execute_document_domain(request, "既往高血压病史5年，规律服药。")
    not_mentioned = execute_document_domain(request, "既往有糖尿病病史8年。")
    unknown = execute_document_domain(request, "既往患高血压，规律服药。")

    assert matched is not None and matched.status == EvidenceStatus.MATCHED
    assert not_matched is not None and not_matched.status == EvidenceStatus.NOT_MATCHED
    assert not_mentioned is not None and not_mentioned.status == EvidenceStatus.NOT_MENTIONED
    assert unknown is not None and unknown.status == EvidenceStatus.UNKNOWN
    assert unknown.reason_code == "MISSING_HISTORY_DURATION"
    assert EvidenceCapability.HISTORY_DURATION in matched.supported_capabilities
    assert EvidenceCapability.HISTORY_DURATION in unknown.missing_capabilities


def _outcome_request():
    return DomainExecutionRequest(
        condition_id="c-outcome",
        condition="出院时背痛好转",
        bindings=(),
        domain="diagnosis",
        entity_type="diagnosis",
        entity="背痛",
        entity_candidates=("背痛",),
        predicate="outcome",
        modifiers=("好转",),
        is_outcome_condition=True,
        outcome_state="improved",
        outcome_phase="discharge",
    )


def test_document_outcome_requires_entity_and_explicit_state():
    request = _outcome_request()

    matched = execute_document_domain(request, "出院时背痛明显好转。")
    not_matched = execute_document_domain(request, "出院时背痛仍持续存在。")
    unknown = execute_document_domain(request, "出院诊断为背痛。")
    not_mentioned = execute_document_domain(request, "出院时腹痛明显好转。")

    assert matched is not None and matched.status == EvidenceStatus.MATCHED
    assert not_matched is not None and not_matched.status == EvidenceStatus.NOT_MATCHED
    assert unknown is not None and unknown.status == EvidenceStatus.UNKNOWN
    assert unknown.reason_code == "MISSING_OUTCOME_STATE"
    assert not_mentioned is not None and not_mentioned.status == EvidenceStatus.NOT_MENTIONED


def test_context_source_role_cannot_independently_prove_condition():
    request = DomainExecutionRequest(
        condition_id="c-role",
        condition="背痛",
        bindings=(),
        domain="diagnosis",
        entity="背痛",
        semantic={"source_role": "CONTEXT"},
    )

    result = normalize_domain_result(
        {
            "applicable": True,
            "matched": True,
            "status": "MATCHED",
            "reason": "文档提及背痛",
            "supported_capabilities": [
                "ENTITY_PRESENCE",
                "DIAGNOSIS_ASSERTION",
            ],
        },
        request,
        domain="diagnosis",
        executor="diagnosis_rules",
    )

    assert result.status == EvidenceStatus.UNKNOWN
    assert result.reason_code == "SOURCE_ROLE_NOT_DECISIVE"
    assert result.source_role_acceptable is False
    legacy = result.to_legacy_dict()
    assert legacy["semantic_type"] == "DIAGNOSIS_ASSERTION"
    assert "PRIMARY" in legacy["acceptable_source_roles"]


def test_normalization_uses_semantic_capability_profile_for_future_source():
    request = DomainExecutionRequest(
        condition_id="c-future",
        condition="future score > 10",
        bindings=(),
        domain="future-source",
        entity="future score",
        semantic={"supported_capabilities": ["NUMERIC_VALUE"]},
    )

    result = normalize_domain_result(
        {
            "applicable": True,
            "matched": True,
            "status": "MATCHED",
            "reason": "future source score matched",
            "required_capabilities": ["NUMERIC_VALUE"],
        },
        request,
        domain="future-source",
        executor="future_rules",
    )

    assert result.status == EvidenceStatus.MATCHED
    assert EvidenceCapability.NUMERIC_VALUE in result.supported_capabilities
    assert result.missing_capabilities == ()


def _multi_lab_bindings():
    return [
        {"html_field": "[检验1] 化验项目描述", "eng_field": "inspItemDesc", "value": "白细胞"},
        {"html_field": "[检验1] 结果", "eng_field": "inspectionValue", "value": "2.0"},
        {"html_field": "[检验1] 单位", "eng_field": "inspResultUnitCode", "value": "*10^9/L"},
        {"html_field": "[检验1] 检测日期", "eng_field": "inspectionDate", "value": "2026-06-09"},
        {"html_field": "[检验1] 检测时间", "eng_field": "inspectionTime", "value": "10:00:00"},
        {"html_field": "[检验2] 化验项目描述", "eng_field": "inspItemDesc", "value": "白细胞"},
        {"html_field": "[检验2] 结果", "eng_field": "inspectionValue", "value": "1.0"},
        {"html_field": "[检验2] 单位", "eng_field": "inspResultUnitCode", "value": "*10^9/L"},
        {"html_field": "[检验2] 检测日期", "eng_field": "inspectionDate", "value": "2026-06-08"},
        {"html_field": "[检验2] 检测时间", "eng_field": "inspectionTime", "value": "10:00:00"},
    ]


def _multi_lab_request(mode, *, time_window=None):
    spec = SimpleNamespace(
        condition_id="c-quantifier",
        text="白细胞>1.5×10^9/L",
        domain="laboratory",
        entity_type="lab",
        canonical_entity="白细胞",
        entity="白细胞",
        keyword="白细胞",
        entity_candidates=("白细胞",),
        predicate="compare",
        semantic_class="检验指标数值比较",
        modifiers=(),
        numeric_comparison={
            "subject": "白细胞",
            "operator": ">",
            "threshold": 1.5,
            "unit": "10^9/L",
        },
        numeric_execution_required=True,
        attributes={},
        quantifier={"mode": mode, "count": 1, "unit": "次"},
        history_context=False,
        internal_negation=False,
        is_outcome_condition=False,
        outcome_state="",
        outcome_phase="",
        diagnosis_phase_evidence_allowed=False,
    )
    return DomainExecutionRequest.from_execution_spec(
        spec,
        _multi_lab_bindings(),
        time_window=time_window,
    )


def test_quantifier_from_execution_spec_controls_multi_record_lab_result():
    all_result = execute_structured_domain(_multi_lab_request("all"))
    at_least_result = execute_structured_domain(_multi_lab_request("at_least"))

    assert all_result is not None
    assert all_result.status == EvidenceStatus.NOT_MATCHED
    assert all_result.reason_code == "QUANTIFIER_ALL_NOT_MET"
    assert all_result.quantifier_mode == "all"
    assert all_result.record_status_counts["matched"] == 1
    assert all_result.record_status_counts["not_matched"] == 1
    assert at_least_result is not None
    assert at_least_result.status == EvidenceStatus.MATCHED
    assert at_least_result.reason_code == "QUANTIFIER_COUNT_MATCHED"


def test_out_of_scope_record_does_not_fail_all_quantifier():
    window = TimeWindow(
        scope="住院期间",
        start=datetime(2026, 6, 9, 0, 0),
        end=datetime(2026, 6, 10, 0, 0),
        required=True,
    )

    result = execute_structured_domain(_multi_lab_request("all", time_window=window))

    assert result is not None
    assert result.status == EvidenceStatus.MATCHED
    assert result.reason_code == "QUANTIFIER_ALL_MATCHED"
    assert result.record_status_counts["in_scope"] == 1
    assert result.record_status_counts["out_of_scope"] == 1


_SYMPTOM_EQUIVALENCE = {
    "query_kind": "SYMPTOM_OR_SIGN",
    "source_kind": "SYMPTOM_OR_SIGN",
    "relation": "SAME_CONCEPT",
    "reason": "原文症状可直接证明查询症状",
}
_SAME_SYMPTOM = {
    "relation": "SAME_SYMPTOM",
    "reason": "两个表达描述同一症状",
}


def _recalled_request(
    *,
    entity="发烧",
    condition="发烧",
    time_window=None,
    quantifier=None,
    modifiers=(),
    history_context=False,
    is_outcome_condition=False,
    outcome_state="",
):
    return DomainExecutionRequest(
        condition_id="c-recalled",
        condition=condition,
        bindings=(),
        domain="diagnosis",
        entity_type="diagnosis",
        entity=entity,
        entity_candidates=(entity,),
        predicate="exists",
        modifiers=tuple(modifiers),
        time_window=time_window,
        quantifier=quantifier or {},
        history_context=history_context,
        is_outcome_condition=is_outcome_condition,
        outcome_state=outcome_state,
    )


def _recalled_decision(
    source_text,
    *,
    query_entity="发烧",
    matched_entity="发热",
    evidence_span=None,
    condition=None,
    time_window=None,
    record_time=None,
    equivalence=None,
    symptom_relation=None,
):
    evidence_span = evidence_span or source_text
    return assess_semantic_entity_recall(
        {
            "candidate_found": True,
            "matched_entity": matched_entity,
            "evidence_span": evidence_span,
        },
        query_entity=query_entity,
        entity_candidates=(query_entity,),
        source_text=source_text,
        equivalence_payload=equivalence or _SYMPTOM_EQUIVALENCE,
        symptom_relation_payload=symptom_relation or _SAME_SYMPTOM,
        condition=condition or query_entity,
        time_window=time_window,
        record_time=record_time,
    )


def test_recalled_document_synonym_is_adjudicated_by_deterministic_semantics():
    source = "患者发热两天。"
    decision = _recalled_decision(source)

    result = execute_recalled_document_domain(
        _recalled_request(), source, decision, candidate_decisions=(decision,)
    )

    assert result is not None
    assert result.status == EvidenceStatus.MATCHED
    assert result.reason_code == "DOCUMENT_POSITIVE_ASSERTION"
    assert result.extra["matched_entity"] == "发热"
    assert result.candidate_records[0]["matched_entity"] == "发热"


def test_recalled_document_marks_rejected_candidate_as_non_decisive_uncertainty():
    source = "患者无其他不适。"
    decision = _recalled_decision(
        source,
        query_entity="目标症状",
        matched_entity="目标症状",
        evidence_span="该片段不在原始文档中",
        condition="目标症状",
    )

    result = execute_recalled_document_domain(
        _recalled_request(entity="目标症状", condition="目标症状"),
        source,
        decision,
        candidate_decisions=(decision,),
    )

    assert result is not None
    assert result.status == EvidenceStatus.UNKNOWN
    assert result.reason_code == "SEMANTIC_RECALL_NON_VERBATIM_EVIDENCE"
    assert result.extra["uncertainty_kind"] == EvidenceUncertaintyKind.REJECTED_CANDIDATE.value
    assert result.to_file_result("arbitrary-document")["uncertainty_kind"] == "REJECTED_CANDIDATE"



def test_recalled_document_exposes_incomplete_candidate_search():
    source = "patient record without the target entity"
    candidate = assess_semantic_entity_recall(
        {
            "candidate_found": True,
            "matched_entity": "target entity",
            "evidence_span": "",
        },
        query_entity="target entity",
        entity_candidates=("target entity",),
        source_text=source,
        condition="target entity",
    )
    batch = parse_semantic_candidate_batch({
        "candidate_found": True,
        "candidates": [
            {"matched_entity": "target entity", "evidence_span": ""},
        ],
    })
    aggregate = aggregate_semantic_entity_decisions(
        (candidate,), query_entity="target entity", batch=batch
    )

    result = execute_recalled_document_domain(
        _recalled_request(entity="target entity", condition="target entity"),
        source,
        aggregate,
        candidate_decisions=(candidate,),
        candidates_complete=batch.complete,
    )

    assert result is not None
    assert result.status == EvidenceStatus.UNKNOWN
    assert result.extra["uncertainty_kind"] == EvidenceUncertaintyKind.INCOMPLETE_SEARCH.value
    assert result.selection_complete is False
    file_result = result.to_file_result("incomplete-document")
    assert file_result["selection_complete"] is False


def test_recalled_document_keeps_negation_and_non_patient_subject_decisive():
    negated_source = "患者否认发热。"
    family_source = "患者父亲反复发热。"
    negated = _recalled_decision(negated_source)
    family = _recalled_decision(family_source)

    negated_result = execute_recalled_document_domain(
        _recalled_request(), negated_source, negated, candidate_decisions=(negated,)
    )
    family_result = execute_recalled_document_domain(
        _recalled_request(), family_source, family, candidate_decisions=(family,)
    )

    assert negated_result is not None
    assert negated_result.status == EvidenceStatus.NOT_MATCHED
    assert negated_result.reason_code == "DOCUMENT_EXPLICIT_NEGATION"
    assert family_result is not None
    assert family_result.status == EvidenceStatus.NOT_MATCHED
    assert family_result.reason_code == "DOCUMENT_NON_PATIENT_SUBJECT"


def test_recalled_document_time_window_distinguishes_missing_and_outside_time():
    window = TimeWindow(
        scope="术前48小时内",
        start=datetime(2026, 6, 8, 12, 0),
        end=datetime(2026, 6, 10, 12, 0),
        required=True,
    )
    source = "患者发热。"
    missing_time = _recalled_decision(source, time_window=window)
    outside_time = _recalled_decision(
        source, time_window=window, record_time=datetime(2026, 6, 1, 12, 0)
    )

    missing_result = execute_recalled_document_domain(
        _recalled_request(time_window=window),
        source,
        missing_time,
        candidate_decisions=(missing_time,),
    )
    outside_result = execute_recalled_document_domain(
        _recalled_request(time_window=window),
        source,
        outside_time,
        candidate_decisions=(outside_time,),
        record_time=datetime(2026, 6, 1, 12, 0),
    )

    assert missing_result is not None
    assert missing_result.status == EvidenceStatus.UNKNOWN
    assert missing_result.reason_code == "DOCUMENT_MENTION_TIME_UNKNOWN"
    assert missing_result.candidate_records[0]["scope_status"] == "UNKNOWN"
    assert outside_result is not None
    assert outside_result.status == EvidenceStatus.NOT_MATCHED
    assert outside_result.reason_code == "DOCUMENT_TIME_OUTSIDE_WINDOW"
    assert outside_result.candidate_records[0]["scope_status"] == "OUT_OF_SCOPE"


def test_recalled_history_duration_uses_accepted_source_entity():
    request = _recalled_request(
        entity="高血压",
        condition="高血压病史不少于10年",
        modifiers=("高血压病史不少于10年",),
        history_context=True,
    )
    equivalence = {
        "query_kind": "DIAGNOSIS",
        "source_kind": "DIAGNOSIS",
        "relation": "SOURCE_MORE_SPECIFIC",
        "reason": "原文诊断是查询疾病的具体表达",
    }

    def execute(source):
        decision = _recalled_decision(
            source,
            query_entity="高血压",
            matched_entity="原发性高血压",
            condition=request.condition,
            equivalence=equivalence,
            symptom_relation={},
        )
        return execute_recalled_document_domain(
            request, source, decision, candidate_decisions=(decision,)
        )

    matched = execute("既往原发性高血压病史12年。")
    not_matched = execute("既往原发性高血压病史5年。")
    unknown = execute("既往患原发性高血压，规律服药。")

    assert matched is not None and matched.status == EvidenceStatus.MATCHED
    assert matched.reason_code == "HISTORY_DURATION_MET"
    assert not_matched is not None and not_matched.status == EvidenceStatus.NOT_MATCHED
    assert not_matched.reason_code == "HISTORY_DURATION_NOT_MET"
    assert unknown is not None and unknown.status == EvidenceStatus.UNKNOWN
    assert unknown.reason_code == "MISSING_HISTORY_DURATION"


def test_recalled_outcome_requires_explicit_outcome_state():
    request = _recalled_request(
        entity="背痛",
        condition="出院时背痛好转",
        modifiers=("好转",),
        is_outcome_condition=True,
        outcome_state="improved",
    )
    equivalence = {
        **_SYMPTOM_EQUIVALENCE,
        "relation": "SOURCE_MORE_SPECIFIC",
    }
    symptom_relation = {
        "relation": "SOURCE_QUALIFIED_SAME_SYMPTOM",
        "reason": "原文增加具体部位限定",
    }

    def execute(source):
        decision = _recalled_decision(
            source,
            query_entity="背痛",
            matched_entity="胸背部疼痛",
            condition=request.condition,
            equivalence=equivalence,
            symptom_relation=symptom_relation,
        )
        return execute_recalled_document_domain(
            request, source, decision, candidate_decisions=(decision,)
        )

    matched = execute("出院时患者胸背部疼痛明显好转。")
    not_matched = execute("出院时患者仍有胸背部疼痛。")
    unknown = execute("出院记录提及患者胸背部疼痛。")

    assert matched is not None and matched.status == EvidenceStatus.MATCHED
    assert matched.reason_code == "OUTCOME_STATE_MET"
    assert not_matched is not None
    assert not_matched.status == EvidenceStatus.NOT_MATCHED
    assert not_matched.reason_code == "OUTCOME_STATE_NOT_MET"
    assert unknown is not None and unknown.status == EvidenceStatus.UNKNOWN
    assert unknown.reason_code == "MISSING_OUTCOME_STATE"


def test_recalled_any_quantifier_uses_mentions_but_record_quantifiers_are_rejected():
    source = "患者早期否认发热，入院后出现高热。"
    negative = _recalled_decision(
        source,
        matched_entity="发热",
        evidence_span="患者早期否认发热",
    )
    positive = _recalled_decision(
        source,
        matched_entity="高热",
        evidence_span="入院后出现高热",
        equivalence={**_SYMPTOM_EQUIVALENCE, "relation": "SOURCE_MORE_SPECIFIC"},
        symptom_relation={
            "relation": "SOURCE_QUALIFIED_SAME_SYMPTOM",
            "reason": "原文增加程度限定",
        },
    )
    batch = parse_semantic_candidate_batch({
        "candidate_found": True,
        "search_complete": True,
        "candidates": [
            {"matched_entity": "发热", "evidence_span": "患者早期否认发热"},
            {"matched_entity": "高热", "evidence_span": "入院后出现高热"},
        ],
    })
    aggregate = aggregate_semantic_entity_decisions(
        (negative, positive), query_entity="发烧", batch=batch
    )

    any_result = execute_recalled_document_domain(
        _recalled_request(quantifier={"mode": "any"}),
        source,
        aggregate,
        candidate_decisions=(negative, positive),
        candidates_complete=True,
    )
    record_quantifier_results = [
        execute_recalled_document_domain(
            _recalled_request(quantifier=quantifier),
            source,
            aggregate,
            candidate_decisions=(negative, positive),
            candidates_complete=True,
        )
        for quantifier in (
            {"mode": "all"},
            {"mode": "at_least", "count": 2},
            {"mode": "latest"},
            {"mode": "earliest"},
        )
    ]

    assert any_result is not None and any_result.status == EvidenceStatus.MATCHED
    assert any_result.reason_code == "QUANTIFIER_ANY_MATCHED"
    assert all(result is not None for result in record_quantifier_results)
    assert all(
        result.status == EvidenceStatus.UNKNOWN
        for result in record_quantifier_results
        if result is not None
    )
    assert all(
        result.reason_code == "DOCUMENT_QUANTIFIER_RECORD_IDENTITY_UNAVAILABLE"
        for result in record_quantifier_results
        if result is not None
    )


def test_incomplete_recalled_candidate_search_only_allows_conclusive_positive_any():
    positive_source = "患者出现发热。"
    negative_source = "患者否认发热。"
    positive = _recalled_decision(positive_source)
    negative = _recalled_decision(negative_source)
    request = _recalled_request(quantifier={"mode": "any"})

    positive_result = execute_recalled_document_domain(
        request,
        positive_source,
        positive,
        candidate_decisions=(positive,),
        candidates_complete=False,
    )
    negative_result = execute_recalled_document_domain(
        request,
        negative_source,
        negative,
        candidate_decisions=(negative,),
        candidates_complete=False,
    )

    assert positive_result is not None and positive_result.status == EvidenceStatus.MATCHED
    assert negative_result is not None and negative_result.status == EvidenceStatus.UNKNOWN
    assert negative_result.reason_code == "QUANTIFIER_ANY_INDETERMINATE"
