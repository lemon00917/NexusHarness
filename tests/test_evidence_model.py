from microharness.medical.evidence import (
    ConditionAdjudicationRequest,
    ConflictLevel,
    DataQuality,
    EvidenceRole,
    EvidenceStatus,
    EvidenceUncertaintyKind,
    ReasonCode,
    adapt_legacy_evidence,
    adjudicate_condition,
    annotate_evidence_source,
    assess_condition_confidence,
    assess_file_confidence,
    assess_patient_confidence,
    attach_native_evidence_records,
    build_evidence_items,
    build_condition_result,
    build_overall_result,
    combine_condition_statuses,
    enrich_response_with_evidence_model,
    sync_condition_result,
)


def test_build_overall_result_preserves_and_four_state_semantics():
    def conditions(*statuses):
        return [
            {"condition_id": f"c{index}", "condition": f"条件{index}", "status": status}
            for index, status in enumerate(statuses, 1)
        ]

    assert build_overall_result(conditions("MATCHED", "MATCHED"), connector="and")["status"] == "MATCHED"
    assert build_overall_result(conditions("MATCHED", "NOT_MATCHED"), connector="and")["status"] == "NOT_MATCHED"
    assert build_overall_result(conditions("MATCHED", "UNKNOWN"), connector="and")["status"] == "UNKNOWN"
    assert build_overall_result(conditions("MATCHED", "NOT_MENTIONED"), connector="and")["status"] == "NOT_MENTIONED"


def test_build_overall_result_preserves_or_four_state_semantics():
    def conditions(*statuses):
        return [
            {"condition_id": f"c{index}", "condition": f"条件{index}", "status": status}
            for index, status in enumerate(statuses, 1)
        ]

    assert build_overall_result(conditions("MATCHED", "UNKNOWN"), connector="or")["status"] == "MATCHED"
    assert build_overall_result(conditions("NOT_MATCHED", "NOT_MATCHED"), connector="or")["status"] == "NOT_MATCHED"
    assert build_overall_result(conditions("NOT_MATCHED", "UNKNOWN"), connector="or")["status"] == "UNKNOWN"
    assert build_overall_result(conditions("NOT_MATCHED", "NOT_MENTIONED"), connector="or")["status"] == "NOT_MENTIONED"


def test_adapt_legacy_matched_evidence_preserves_structured_fields():
    item = adapt_legacy_evidence(
        {
            "file": "结构化服务",
            "source_type": "service",
            "record_id": "r-17",
            "entity": "目标实体",
            "event_time": "2026-06-10 12:00:00",
            "value": 4,
            "unit": "x10^9/L",
            "abnormal_flag": "N",
            "reference_range": "1.5-7.5",
            "matched": True,
            "reason": "结果满足数值条件",
            "fields": "结构化原始记录",
        },
        "c2",
    )

    assert item.condition_id == "c2"
    assert item.status == EvidenceStatus.MATCHED
    assert item.reason_code == ReasonCode.MATCH_CONFIRMED
    assert item.data_quality == DataQuality.COMPLETE
    assert item.record_id == "r-17"
    assert item.value == 4


def test_display_evidence_exposes_quantifier_adjudication_details():
    items = build_evidence_items([
        {
            "file": "检验指标查询 (2条)",
            "matched": False,
            "reason": "要求全部记录符合，但有1条记录不符合",
            "fields": "两条候选记录",
            "quantifier_mode": "all",
            "quantifier_count": None,
            "quantifier_unit": "条",
            "record_status_counts": {"matched": 1, "not_matched": 1},
            "selection_complete": True,
            "量词选中记录": [{"记录": "检验2"}],
        }
    ])

    adjudication = items[0]["量词裁决"]
    assert adjudication["模式"] == "all"
    assert adjudication["记录统计"]["not_matched"] == 1
    assert adjudication["候选完整"] is True
    assert adjudication["选中记录"][0]["记录"] == "检验2"


def test_adapt_legacy_evidence_preserves_document_semantic_trace():
    trace = [
        {
            "type": "coreference",
            "antecedent_sentence": "患者出现目标症状",
            "referring_sentence": "该症状持续3天",
            "subject": "patient",
            "assertion": "POSITIVE",
            "time_status": "TEMPORAL_MATCHED",
            "final_reason": "当前句明确肯定目标实体",
        }
    ]

    item = adapt_legacy_evidence(
        {
            "file": "病历文档",
            "matched": True,
            "reason": "文档语义明确支持",
            "fields": "患者出现目标症状。该症状持续3天。",
            "semantic_trace": trace,
        },
        "c-trace",
    )

    assert item.metadata["semantic_trace"] == trace


def test_source_failure_is_unknown_instead_of_not_matched():
    result = build_condition_result(
        {
            "condition": "目标条件",
            "matched": False,
            "reason": "关键证据不足，无法判断：外部数据源调用失败",
            "files": [
                {
                    "file": "外部数据源",
                    "matched": False,
                    "reason": "外部数据源调用失败",
                }
            ],
        },
        "c1",
    )

    assert result.status == EvidenceStatus.UNKNOWN
    assert result.reason_code == ReasonCode.SOURCE_UNAVAILABLE
    assert result.data_quality == DataQuality.SOURCE_ERROR
    assert result.evidence[0].status == EvidenceStatus.UNKNOWN


def test_time_window_miss_has_stable_reason_code():
    item = adapt_legacy_evidence(
        {
            "file": "候选记录",
            "matched": False,
            "reason": "记录时间不在事件前时间窗",
            "fields": "记录时间=2026-04-28 14:21:10",
        },
        "c1",
    )

    assert item.status == EvidenceStatus.NOT_MATCHED
    assert item.reason_code == ReasonCode.TIME_OUTSIDE_WINDOW


def test_candidate_records_become_record_level_evidence():
    result = build_condition_result(
        {
            "condition": "目标指标大于阈值",
            "matched": False,
            "reason": "候选记录不满足条件",
            "files": [
                {
                    "file": "结构化检验服务",
                    "matched": False,
                    "reason": "候选记录不满足条件",
                    "候选记录": [
                        {
                            "记录": "检验34",
                            "项目": "目标指标",
                            "检测时间": "2026-06-10 10:30:00",
                            "结果": "1.2",
                            "单位": "x10^9/L",
                            "异常标志": "L",
                            "参考范围": "1.5-7.5",
                            "数值判断": "1.2不大于1.5",
                            "数值是否满足": False,
                            "是否在时间窗": True,
                        }
                    ],
                }
            ],
        },
        "c3",
    )

    assert len(result.evidence) == 1
    evidence = result.evidence[0]
    assert evidence.record_id == "检验34"
    assert evidence.entity == "目标指标"
    assert evidence.event_time == "2026-06-10 10:30:00"
    assert evidence.value == "1.2"
    assert evidence.reason_code == ReasonCode.VALUE_CONDITION_NOT_MET
    assert evidence.status == EvidenceStatus.NOT_MATCHED


def test_candidate_record_without_event_time_is_unknown():
    result = build_condition_result(
        {
            "condition": "事件前使用目标药物",
            "matched": False,
            "reason": "缺少可比较的记录时间，无法判断",
            "files": [
                {
                    "file": "结构化服务",
                    "matched": False,
                    "reason": "缺少可比较的记录时间，无法判断",
                    "候选记录": [{"记录": "记录17", "记录时间": "未取得", "是否在时间窗": False}],
                }
            ],
        },
        "c4",
    )

    assert result.evidence[0].status == EvidenceStatus.UNKNOWN
    assert result.evidence[0].reason_code == ReasonCode.MISSING_EVENT_TIME


def test_candidate_value_match_does_not_override_unresolved_required_time_window():
    result = build_condition_result(
        {
            'condition': '事件前目标指标大于阈值',
            'matched': False,
            'reason': '缺少事件时间锚点，无法计算目标时间窗',
            'files': [
                {
                    'file': '结构化检验服务',
                    'matched': False,
                    'status': 'UNKNOWN',
                    'time_window_required': True,
                    'time_window_resolved': False,
                    '候选记录': [
                        {
                            '记录': '检验34',
                            '项目': '目标指标',
                            '检测时间': '2026-06-10 10:30:00',
                            '结果': '4.0',
                            '数值是否满足': True,
                            '是否在时间窗': None,
                        }
                    ],
                }
            ],
        },
        'c-unresolved-window',
    )

    assert result.status == EvidenceStatus.UNKNOWN
    assert result.evidence[0].status == EvidenceStatus.UNKNOWN
    assert result.evidence[0].reason_code == ReasonCode.MISSING_EVENT_TIME


def test_diagnosis_bindings_become_record_level_evidence():
    file_result = {"file": "诊断查询 (2条)", "matched": True, "reason": "存在背痛诊断", "fields": "背痛"}
    source = {
        "file": "诊断查询 (2条)", "template": "诊断查询", "service_id": "diagnosis-query",
        "semantic": {"entity_type": "diagnosis"},
        "bindings": [
            {"html_field": "[诊断1] 诊断名称", "value": "背痛", "xml_path": "external/diagnoseName"},
            {"html_field": "[诊断1] 诊断类型", "value": "入院诊断", "xml_path": "external/diagTypeDesc"},
            {"html_field": "[诊断1] 诊断日期", "value": "2026-06-08", "xml_path": "external/diagnoseDate"},
            {"html_field": "[诊断1] 诊断时间", "value": "10:58:37", "xml_path": "external/diagnoseTime"},
            {"html_field": "[诊断2] 诊断名称", "value": "高血压", "xml_path": "external/diagnoseName"},
        ],
    }
    attach_native_evidence_records(file_result, source, condition="背痛", entity="背痛")
    result = build_condition_result({"condition": "背痛", "matched": True, "reason": "存在背痛诊断", "files": [file_result]}, "c5")

    assert len(result.evidence) == 1
    evidence = result.evidence[0]
    assert evidence.source_type == "diagnosis"
    assert evidence.record_id == "诊断1"
    assert evidence.entity == "背痛"
    assert evidence.event_time == "2026-06-08 10:58:37"
    assert evidence.metadata["fields"]["诊断类型"] == "入院诊断"


def test_encounter_bindings_preserve_attributes_and_time():
    file_result = {"file": "就诊信息查询 (1条)", "matched": True, "reason": "住院天数满足", "fields": "住院"}
    source = {"file": file_result["file"], "template": "就诊信息查询", "service_id": "encounter-info",
              "semantic": {"entity_type": "encounter"}, "bindings": [
        {"html_field": "就诊类型", "value": "住院", "xml_path": "external/encTypeDesc"},
        {"html_field": "就诊科室", "value": "骨科", "xml_path": "external/encDeptName"},
        {"html_field": "入院日期时间", "value": "2026-06-01 08:00:00", "xml_path": "external/encStartDate"},
        {"html_field": "出院日期时间", "value": "2026-06-04 09:00:00", "xml_path": "external/encEndDate"},
    ]}
    attach_native_evidence_records(file_result, source, condition="住院天数小于5天", entity="住院天数")
    evidence = build_condition_result({"condition": "住院天数小于5天", "matched": True, "reason": "满足", "files": [file_result]}, "c6").evidence[0]

    assert evidence.source_type == "encounter"
    assert evidence.entity == "住院"
    assert evidence.event_time == "2026-06-01 08:00:00"
    assert evidence.metadata["fields"]["出院日期时间"] == "2026-06-04 09:00:00"


def test_document_bindings_preserve_section_text_and_path():
    file_result = {"file": "出院记录 (doc-1)", "matched": True, "reason": "出院诊断存在胃息肉", "fields": "出院诊断: 胃息肉"}
    source = {"file": file_result["file"], "template": "DischargeRecord", "bindings": [
        {"html_field": "出院诊断", "value": "胃息肉", "xml_path": "discharge_diagnosis"},
        {"html_field": "出院医嘱", "value": "定期复查", "xml_path": "discharge_orders"},
    ]}
    attach_native_evidence_records(file_result, source, condition="胃息肉", entity="胃息肉", target_sections=["出院诊断"])
    evidence = build_condition_result({"condition": "胃息肉", "matched": True, "reason": "满足", "files": [file_result]}, "c7").evidence[0]

    assert evidence.source_type == "document"
    assert evidence.document == "出院记录 (doc-1)"
    assert evidence.section == "出院诊断"
    assert evidence.raw_text == "胃息肉"
    assert evidence.metadata["template"] == "DischargeRecord"
    assert evidence.metadata["xml_path"] == "discharge_diagnosis"


def test_native_evidence_does_not_claim_unrelated_matched_record():
    file_result = {"file": "diagnosis source", "matched": True, "reason": "matched", "fields": "hypertension"}
    source = {"file": file_result["file"], "service_id": "diagnosis-query",
              "semantic": {"entity_type": "diagnosis"}, "bindings": [
        {"html_field": "[record1] name", "value": "hypertension", "xml_path": "external/diagnoseName"},
    ]}

    attach_native_evidence_records(file_result, source, condition="back pain", entity="back pain")

    assert "_structured_evidence_records" not in file_result


def test_numeric_document_evidence_keeps_selected_operand_fields():
    file_result = {"file": "discharge-document", "matched": True, "reason": "comparison matched", "fields": "2 days"}
    source = {"file": file_result["file"], "template": "discharge-template", "bindings": [
        {"html_field": "admission time", "value": "2026-06-01 08:00:00", "xml_path": "admission_time"},
        {"html_field": "discharge time", "value": "2026-06-03 08:00:00", "xml_path": "discharge_time"},
    ]}

    attach_native_evidence_records(
        file_result,
        source,
        condition="length of stay less than 5 days",
        entity="length of stay",
        target_sections=["admission time", "discharge time"],
        is_numeric=True,
    )

    records = file_result["_structured_evidence_records"]
    assert [record["section"] for record in records] == ["admission time", "discharge time"]
    assert all(record["status"] == "MATCHED" for record in records)


def test_service_failure_does_not_generate_native_clinical_records():
    file_result = {"file": "diagnosis unavailable", "matched": False, "reason": "source unavailable"}
    source = {"file": file_result["file"], "service_id": "diagnosis-query", "service_error": True,
              "semantic": {"entity_type": "diagnosis"}, "bindings": [
        {"html_field": "service status", "value": "unavailable", "xml_path": "external/service_status"},
    ]}

    attach_native_evidence_records(file_result, source, condition="back pain", entity="back pain")

    assert "_structured_evidence_records" not in file_result


def test_response_enrichment_removes_internal_native_record_field():
    source_result = {
        "file": "source-a",
        "matched": True,
        "reason": "matched",
        "fields": "raw-a",
        "_structured_evidence_records": [{
            "source_type": "document",
            "source_name": "source-a",
            "record_id": "record-a",
            "raw_text": "raw-a",
            "status": "MATCHED",
            "reason_code": "MATCH_CONFIRMED",
            "data_quality": "COMPLETE",
            "reason": "matched",
        }],
    }
    response = {"results": [{"per_condition": {"condition-a": {
        "condition": "condition-a", "matched": True, "reason": "matched", "files": [source_result],
    }}}]}

    enriched = enrich_response_with_evidence_model(response)
    condition = enriched["results"][0]["per_condition"]["condition-a"]

    assert condition["condition_result"]["evidence"][0]["record_id"] == "record-a"
    assert "_structured_evidence_records" not in condition["files"][0]


def test_response_enrichment_keeps_legacy_fields_and_aligns_condition_ids():
    response = {
        "results": [
            {
                "matched": True,
                "reason": "全部满足",
                "per_condition": {
                    "条件甲": {
                        "condition": "条件甲",
                        "matched": True,
                        "reason": "存在支持记录",
                        "files": [
                            {
                                "file": "来源甲",
                                "matched": True,
                                "reason": "存在支持记录",
                                "fields": "原文甲",
                            }
                        ],
                        "证据明细": [{"来源": "来源甲"}],
                    }
                },
            }
        ]
    }
    query_ir = {
        "子条件": [
            {"条件ID": "c7", "条件文本": "条件甲"},
        ]
    }

    enriched = enrich_response_with_evidence_model(response, query_ir)
    legacy = enriched["results"][0]["per_condition"]["条件甲"]
    unified = legacy["condition_result"]

    assert legacy["证据明细"] == [{"来源": "来源甲"}]
    assert legacy["matched"] is True
    assert unified["condition_id"] == "c7"
    assert unified["status"] == "MATCHED"
    assert unified["evidence"][0]["source_name"] == "来源甲"
    assert enriched["results"][0]["condition_results"] == [unified]
    assert enriched["evidence_model_version"] == "1.0"


def _source_result(name, status, role, service_id, reason=None, quality="COMPLETE"):
    return {
        "file": name,
        "service_id": service_id,
        "source_role": role,
        "status": status,
        "matched": status == "MATCHED",
        "reason": reason or f"{name}:{status}",
        "data_quality": quality,
        "fields": f"{name} evidence",
    }


def test_conflicting_primary_sources_return_unknown():
    result = build_condition_result(
        {
            "condition": "target condition",
            "matched": True,
            "reason": "legacy matched",
            "files": [
                _source_result("primary-a", "MATCHED", "PRIMARY", "service-a"),
                _source_result("primary-b", "NOT_MATCHED", "PRIMARY", "service-b"),
            ],
        },
        "c-conflict",
    )

    assert result.status == EvidenceStatus.UNKNOWN
    assert result.reason_code == ReasonCode.EVIDENCE_CONFLICT
    assert result.conflict_level == ConflictLevel.CONCLUSIVE_CONFLICT
    assert len(result.source_decisions) == 2


def test_primary_positive_retains_match_when_supporting_source_disagrees():
    result = build_condition_result(
        {
            "condition": "target condition",
            "matched": True,
            "reason": "primary matched",
            "files": [
                _source_result("primary", "MATCHED", "PRIMARY", "service-a"),
                _source_result("support", "NOT_MATCHED", "SUPPORTING", "service-b"),
            ],
        },
        "c-supporting-disagreement",
    )

    assert result.status == EvidenceStatus.MATCHED
    assert result.conflict_level == ConflictLevel.SUPPORTING_DISAGREEMENT
    assert result.reason_code == ReasonCode.MATCH_CONFIRMED
    assert "辅助证据存在分歧" in result.reason


def test_primary_negative_and_supporting_positive_return_unknown():
    result = build_condition_result(
        {
            "condition": "target condition",
            "matched": False,
            "reason": "primary negative",
            "files": [
                _source_result("primary", "NOT_MATCHED", "PRIMARY", "service-a"),
                _source_result("support", "MATCHED", "SUPPORTING", "service-b"),
            ],
        },
        "c-supporting-positive",
    )

    assert result.status == EvidenceStatus.UNKNOWN
    assert result.reason_code == ReasonCode.EVIDENCE_CONFLICT
    assert result.conflict_level == ConflictLevel.CONCLUSIVE_CONFLICT


def test_supporting_source_can_take_over_when_primary_is_unavailable():
    result = build_condition_result(
        {
            "condition": "target condition",
            "matched": False,
            "reason": "primary unavailable",
            "files": [
                _source_result(
                    "primary",
                    "UNKNOWN",
                    "PRIMARY",
                    "service-a",
                    reason="外部数据源调用失败",
                    quality="SOURCE_ERROR",
                ),
                _source_result("support", "MATCHED", "SUPPORTING", "service-b"),
            ],
        },
        "c-supporting-takeover",
    )

    assert result.status == EvidenceStatus.MATCHED
    assert result.reason_code == ReasonCode.MATCH_CONFIRMED


def test_document_match_wins_when_primary_is_unavailable_and_other_document_does_not_mention_entity():
    result = build_condition_result(
        {
            "condition": "背痛",
            "matched": False,
            "reason": "诊断接口不可用",
            "files": [
                _source_result(
                    "诊断查询",
                    "UNKNOWN",
                    "PRIMARY",
                    "diagnosis-query",
                    reason="未取得诊断查询接口数据",
                    quality="SOURCE_ERROR",
                ),
                _source_result(
                    "入院记录",
                    "MATCHED",
                    "SUPPORTING",
                    "document-admission",
                    reason="主诉和现病史明确提及胸背部疼痛",
                ),
                _source_result(
                    "出院记录",
                    "MATCHED",
                    "SUPPORTING",
                    "document-discharge",
                    reason="入院情况明确提及胸背部疼痛",
                ),
                _source_result(
                    "手术记录",
                    "NOT_MENTIONED",
                    "SUPPORTING",
                    "document-operation",
                    reason="未找到与背痛构成同一局部语义片段的文本",
                ),
            ],
        },
        "c-document-takeover",
    )

    assert result.status == EvidenceStatus.MATCHED
    assert result.reason_code == ReasonCode.MATCH_CONFIRMED
    assert result.conflict_level == ConflictLevel.NONE
    assert "胸背部疼痛" in result.reason


def test_response_enrichment_backfills_canonical_status_on_legacy_file_results():
    response = {
        "results": [
            {
                "per_condition": {
                    "背痛": {
                        "condition": "背痛",
                        "matched": False,
                        "reason": "诊断接口不可用",
                        "files": [
                            _source_result(
                                "诊断查询",
                                "UNKNOWN",
                                "PRIMARY",
                                "diagnosis-query",
                                reason="未取得诊断查询接口数据",
                                quality="SOURCE_ERROR",
                            ),
                            _source_result(
                                "入院记录",
                                "MATCHED",
                                "SUPPORTING",
                                "document-admission",
                                reason="主诉和现病史明确提及胸背部疼痛",
                            ),
                            _source_result(
                                "手术记录",
                                "NOT_MENTIONED",
                                "SUPPORTING",
                                "document-operation",
                                reason="关键字'背痛'未在数据中出现",
                            ),
                        ],
                    }
                }
            }
        ]
    }

    enriched = enrich_response_with_evidence_model(response)
    condition = enriched["results"][0]["per_condition"]["背痛"]
    statuses = {item["file"]: item["status"] for item in condition["files"]}

    assert condition["status"] == "MATCHED"
    assert statuses == {
        "诊断查询": "UNKNOWN",
        "入院记录": "MATCHED",
        "手术记录": "NOT_MENTIONED",
    }


def test_complete_supporting_negative_can_take_over_unavailable_primary():
    result = build_condition_result(
        {
            "condition": "target condition",
            "matched": False,
            "reason": "primary unavailable",
            "files": [
                _source_result(
                    "primary",
                    "UNKNOWN",
                    "PRIMARY",
                    "service-a",
                    reason="外部数据源调用失败",
                    quality="SOURCE_ERROR",
                ),
                _source_result("support", "NOT_MATCHED", "SUPPORTING", "service-b"),
            ],
        },
        "c-supporting-negative",
    )

    assert result.status == EvidenceStatus.NOT_MATCHED
    assert result.conflict_level == ConflictLevel.NONE


def test_complete_not_mentioned_can_take_over_unavailable_primary():
    result = build_condition_result(
        {
            "condition": "发烧",
            "matched": False,
            "reason": "诊断接口不可用",
            "files": [
                _source_result(
                    "诊断查询",
                    "UNKNOWN",
                    "PRIMARY",
                    "diagnosis-query",
                    reason="未取得诊断查询接口数据",
                    quality="SOURCE_ERROR",
                ),
                _source_result(
                    "入院记录",
                    "NOT_MENTIONED",
                    "SUPPORTING",
                    "document-admission",
                    reason="病历相关章节未提及发烧",
                    quality="COMPLETE",
                ),
            ],
        },
        "c-not-mentioned-takeover",
    )

    assert result.status == EvidenceStatus.NOT_MENTIONED
    assert result.reason_code == ReasonCode.NO_MATCHING_RECORD
    assert result.conflict_level == ConflictLevel.NONE
    assert "未提及发烧" in result.reason


def test_not_mentioned_does_not_override_ambiguous_unknown_source():
    result = build_condition_result(
        {
            "condition": "发烧",
            "matched": False,
            "reason": "证据语义不明确",
            "files": [
                _source_result(
                    "诊断查询",
                    "UNKNOWN",
                    "PRIMARY",
                    "diagnosis-query",
                    reason="诊断记录语义不明确",
                    quality="PARTIAL",
                ),
                _source_result(
                    "入院记录",
                    "NOT_MENTIONED",
                    "SUPPORTING",
                    "document-admission",
                    reason="病历相关章节未提及发烧",
                    quality="COMPLETE",
                ),
            ],
        },
        "c-ambiguous-unknown",
    )

    assert result.status == EvidenceStatus.UNKNOWN
    assert result.reason_code == ReasonCode.INSUFFICIENT_EVIDENCE


def test_complete_not_mentioned_overrides_rejected_semantic_candidate():
    rejected = _source_result(
        "semantic source",
        "UNKNOWN",
        "SUPPORTING",
        "future-source-b",
        reason="candidate was rejected by source validation",
        quality="PARTIAL",
    )
    rejected.update({
        "executor": "future-semantic-executor",
        "uncertainty_kind": EvidenceUncertaintyKind.REJECTED_CANDIDATE.value,
        "selection_complete": True,
    })

    result = build_condition_result(
        {
            "condition": "arbitrary target entity",
            "matched": False,
            "files": [
                _source_result(
                    "complete source",
                    "NOT_MENTIONED",
                    "PRIMARY",
                    "future-source-a",
                    reason="complete source contains no target entity",
                    quality="COMPLETE",
                ),
                rejected,
            ],
        },
        "c-rejected-candidate",
    )

    assert result.status == EvidenceStatus.NOT_MENTIONED
    assert result.reason_code == ReasonCode.NO_MATCHING_RECORD
    rejected_decision = next(
        item for item in result.source_decisions
        if item["source_name"] == "semantic source"
    )
    assert rejected_decision["uncertainty_kind"] == "REJECTED_CANDIDATE"


def test_same_source_not_mentioned_does_not_hide_rejected_candidate_kind():
    rejected = _source_result(
        "future source candidate",
        "UNKNOWN",
        "SUPPORTING",
        "future-source-shared",
        quality="PARTIAL",
    )
    rejected.update({
        "uncertainty_kind": EvidenceUncertaintyKind.REJECTED_CANDIDATE.value,
        "selection_complete": True,
    })

    result = build_condition_result(
        {
            "condition": "arbitrary future condition",
            "matched": False,
            "files": [
                _source_result(
                    "complete source",
                    "NOT_MENTIONED",
                    "PRIMARY",
                    "future-source-complete",
                    quality="COMPLETE",
                ),
                rejected,
                _source_result(
                    "future source complete section",
                    "NOT_MENTIONED",
                    "SUPPORTING",
                    "future-source-shared",
                    quality="COMPLETE",
                ),
            ],
        },
        "c-same-source-rejected-candidate",
    )

    assert result.status == EvidenceStatus.NOT_MENTIONED
    shared_decision = next(
        item for item in result.source_decisions
        if item["source_id"] == "service:future-source-shared"
    )
    assert shared_decision["status"] == "UNKNOWN"
    assert shared_decision["uncertainty_kind"] == "REJECTED_CANDIDATE"


def test_complete_primary_not_mentioned_outweighs_supporting_incomplete_search():
    incomplete = _source_result(
        "future incomplete source",
        "UNKNOWN",
        "SUPPORTING",
        "future-source-incomplete",
        quality="PARTIAL",
    )
    incomplete.update({
        "uncertainty_kind": EvidenceUncertaintyKind.REJECTED_CANDIDATE.value,
        "selection_complete": False,
        "semantic_trace": [
            {
                "stage": "semantic_candidate_result",
                "status": "UNKNOWN",
                "reason_code": "SEMANTIC_RECALL_EVIDENCE_MISSING",
            }
        ],
    })

    result = build_condition_result(
        {
            "condition": "arbitrary future condition",
            "matched": False,
            "files": [
                _source_result(
                    "complete source",
                    "NOT_MENTIONED",
                    "PRIMARY",
                    "future-source-complete",
                    quality="COMPLETE",
                ),
                incomplete,
            ],
        },
        "c-incomplete-rejected-candidate",
    )

    assert result.status == EvidenceStatus.NOT_MENTIONED
    assert result.reason_code == ReasonCode.NO_MATCHING_RECORD
    incomplete_decision = next(
        item for item in result.source_decisions
        if item["source_id"] == "service:future-source-incomplete"
    )
    assert incomplete_decision["status"] == "UNKNOWN"
    assert incomplete_decision["uncertainty_kind"] == "INCOMPLETE_SEARCH"


def test_legacy_rejected_candidate_trace_maps_to_uncertainty_contract():
    item = adapt_legacy_evidence(
        {
            "file": "legacy semantic source",
            "status": "UNKNOWN",
            "matched": False,
            "reason_code": "INSUFFICIENT_EVIDENCE",
            "reason": "semantic candidate could not be validated",
            "fields": "complete source text",
            "selection_complete": True,
            "semantic_trace": [
                {
                    "stage": "semantic_candidate_result",
                    "status": "UNKNOWN",
                    "reason_code": "SEMANTIC_RECALL_EVIDENCE_MISSING",
                }
            ],
        },
        "c-legacy-rejected-trace",
    )

    assert item.metadata["uncertainty_kind"] == "REJECTED_CANDIDATE"


def test_complete_primary_not_mentioned_outweighs_supporting_unresolved_candidate():
    unresolved = _source_result(
        "uncertain source",
        "UNKNOWN",
        "SUPPORTING",
        "future-source-d",
        reason="candidate meaning remains unresolved",
        quality="PARTIAL",
    )
    unresolved["uncertainty_kind"] = EvidenceUncertaintyKind.UNRESOLVED_CANDIDATE.value

    result = build_condition_result(
        {
            "condition": "肺炎",
            "matched": False,
            "files": [
                _source_result(
                    "诊断查询",
                    "NOT_MENTIONED",
                    "PRIMARY",
                    "diagnosis-query",
                    reason="诊断记录中未找到与'肺炎'匹配的诊断项",
                    quality="COMPLETE",
                ),
                unresolved,
            ],
        },
        "c-unresolved-candidate",
    )

    assert result.status == EvidenceStatus.NOT_MENTIONED
    assert result.reason_code == ReasonCode.NO_MATCHING_RECORD


def test_complete_not_mentioned_does_not_override_temporal_uncertainty():
    temporal = _source_result(
        "temporal source",
        "UNKNOWN",
        "SUPPORTING",
        "future-source-f",
        reason="event time is unavailable",
        quality="PARTIAL",
    )
    temporal.update({
        "reason_code": ReasonCode.MISSING_EVENT_TIME.value,
        "uncertainty_kind": EvidenceUncertaintyKind.TEMPORAL_UNRESOLVED.value,
    })

    result = build_condition_result(
        {
            "condition": "target entity inside time window",
            "matched": False,
            "files": [
                _source_result(
                    "complete source",
                    "NOT_MENTIONED",
                    "PRIMARY",
                    "future-source-e",
                    quality="COMPLETE",
                ),
                temporal,
            ],
        },
        "c-temporal-unknown",
    )

    assert result.status == EvidenceStatus.UNKNOWN
    assert result.source_decisions[1]["uncertainty_kind"] == "TEMPORAL_UNRESOLVED"


def test_negative_plus_unknown_is_unknown():
    result = build_condition_result(
        {
            "condition": "target condition",
            "matched": False,
            "reason": "negative",
            "files": [
                _source_result("primary-a", "NOT_MATCHED", "PRIMARY", "service-a"),
                _source_result("primary-b", "UNKNOWN", "PRIMARY", "service-b"),
            ],
        },
        "c-incomplete-negative",
    )

    assert result.status == EvidenceStatus.UNKNOWN
    assert result.reason_code == ReasonCode.INSUFFICIENT_EVIDENCE


def test_records_from_same_service_are_aggregated_before_conflict_resolution():
    result = build_condition_result(
        {
            "condition": "target condition",
            "matched": True,
            "reason": "one record matched",
            "files": [
                _source_result("service result 1", "MATCHED", "PRIMARY", "service-a"),
                _source_result("service result 2", "NOT_MATCHED", "PRIMARY", "service-a"),
            ],
        },
        "c-same-source",
    )

    assert result.status == EvidenceStatus.MATCHED
    assert result.conflict_level == ConflictLevel.NONE
    assert len(result.source_decisions) == 1
    assert result.source_decisions[0]["evidence_count"] == 2


def test_typed_condition_adjudicator_accepts_canonical_evidence():
    evidence = adapt_legacy_evidence(
        _source_result("primary", "MATCHED", "PRIMARY", "service-a"),
        "c-typed",
    )

    result = adjudicate_condition(ConditionAdjudicationRequest(
        condition_id="c-typed",
        condition="target condition",
        evidence=(evidence,),
        original_status=EvidenceStatus.UNKNOWN,
        original_reason="legacy state must not override evidence",
    ))

    assert result.status == EvidenceStatus.MATCHED
    assert result.reason_code == ReasonCode.MATCH_CONFIRMED
    assert result.source_decisions[0]["source_id"] == "service:service-a"


def test_legacy_summary_status_cannot_override_canonical_evidence():
    result = build_condition_result(
        {
            "condition": "target condition",
            "matched": False,
            "status": "UNKNOWN",
            "reason": "",
            "files": [_source_result("primary", "MATCHED", "PRIMARY", "service-a")],
        },
        "c-canonical-wins",
    )

    assert result.status == EvidenceStatus.MATCHED
    assert result.reason.startswith("primary:")


def test_condition_without_any_evidence_is_unknown():
    result = build_condition_result(
        {"condition": "target condition", "matched": False, "status": "UNKNOWN", "reason": ""},
        "c-no-evidence",
    )

    assert result.status == EvidenceStatus.UNKNOWN
    assert result.reason_code == ReasonCode.INSUFFICIENT_EVIDENCE
    assert result.data_quality == DataQuality.MISSING


def test_missing_required_capability_prevents_positive_condition_result():
    source = _source_result("primary", "MATCHED", "PRIMARY", "service-a")
    source.update({
        "supported_capabilities": ["ENTITY_PRESENCE"],
        "required_capabilities": ["ENTITY_PRESENCE", "TEMPORAL_OCCURRENCE"],
        "missing_capabilities": ["TEMPORAL_OCCURRENCE"],
    })

    result = build_condition_result(
        {"condition": "target in time window", "matched": True, "files": [source]},
        "c-capability",
    )

    assert result.status == EvidenceStatus.UNKNOWN
    assert result.reason_code == ReasonCode.MISSING_REQUIRED_CAPABILITY
    assert result.source_decisions[0]["missing_capabilities"] == ["TEMPORAL_OCCURRENCE"]


def test_incomplete_supporting_absence_cannot_replace_unavailable_primary():
    supporting = _source_result(
        "support", "NOT_MENTIONED", "SUPPORTING", "document-admission",
        reason="相关章节未提及目标实体",
    )
    supporting["selection_complete"] = False

    result = build_condition_result(
        {
            "condition": "target condition",
            "matched": False,
            "files": [
                _source_result(
                    "primary", "UNKNOWN", "PRIMARY", "service-a",
                    reason="外部数据源调用失败", quality="SOURCE_ERROR",
                ),
                supporting,
            ],
        },
        "c-incomplete-support",
    )

    assert result.status == EvidenceStatus.UNKNOWN
    assert result.reason_code == ReasonCode.SOURCE_UNAVAILABLE
    support_decision = next(
        item for item in result.source_decisions if item["source_name"] == "support"
    )
    assert support_decision["status"] == "UNKNOWN"
    assert support_decision["reason_code"] == "INCOMPLETE_CANDIDATE_SET"


def test_explicit_logical_source_id_groups_records_without_filename_guessing():
    first = _source_result("display record A", "MATCHED", "PRIMARY", "service-a")
    second = _source_result("unrelated display record B", "NOT_MATCHED", "PRIMARY", "service-b")
    first["logical_source_id"] = "plan:c1:primary-diagnosis"
    second["logical_source_id"] = "plan:c1:primary-diagnosis"

    result = build_condition_result(
        {"condition": "target condition", "matched": True, "files": [first, second]},
        "c-logical-source",
    )

    assert result.status == EvidenceStatus.MATCHED
    assert len(result.source_decisions) == 1
    assert result.source_decisions[0]["source_id"] == "plan:c1:primary-diagnosis"
    assert result.source_decisions[0]["evidence_count"] == 2


def test_quantifier_adjudication_is_not_overridden_by_candidate_record_votes():
    result = build_condition_result(
        {
            "condition": "所有白细胞记录均高于阈值",
            "matched": False,
            "status": "NOT_MATCHED",
            "reason_code": "QUANTIFIER_ALL_NOT_MET",
            "reason": "要求全部记录符合，但有1条记录不符合",
            "files": [
                {
                    "file": "检验指标查询 (2条)",
                    "source_type": "service",
                    "source_role": "PRIMARY",
                    "service_id": "lab-results",
                    "matched": False,
                    "status": "NOT_MATCHED",
                    "reason_code": "QUANTIFIER_ALL_NOT_MET",
                    "reason": "要求全部记录符合，但有1条记录不符合",
                    "data_quality": "COMPLETE",
                    "fields": "两条白细胞检验记录",
                    "quantifier_mode": "all",
                    "quantifier_count": None,
                    "quantifier_unit": "条",
                    "record_status_counts": {"matched": 1, "not_matched": 1},
                    "selection_complete": True,
                    "候选记录": [
                        {
                            "记录": "检验1",
                            "项目": "白细胞",
                            "结果": "2.0",
                            "数值是否满足": True,
                            "record_status": "MATCHED",
                        },
                        {
                            "记录": "检验2",
                            "项目": "白细胞",
                            "结果": "1.0",
                            "数值是否满足": False,
                            "record_status": "NOT_MATCHED",
                        },
                    ],
                }
            ],
        },
        "c-quantifier-all",
    )

    assert result.status == EvidenceStatus.NOT_MATCHED
    assert result.reason_code == ReasonCode.QUANTIFIER_ALL_NOT_MET
    assert result.conflict_level == ConflictLevel.NONE
    assert len(result.evidence) == 3
    assert result.source_decisions[0]["status"] == "NOT_MATCHED"
    assert result.source_decisions[0]["quantifier"]["quantifier_mode"] == "all"


def test_time_anchor_does_not_vote_on_condition_status():
    result = build_condition_result(
        {
            "condition": "target condition",
            "matched": True,
            "reason": "primary matched",
            "files": [
                _source_result("primary", "MATCHED", "PRIMARY", "service-a"),
                _source_result("anchor", "NOT_MATCHED", "TIME_ANCHOR", "service-time"),
            ],
        },
        "c-time-anchor",
    )

    assert result.status == EvidenceStatus.MATCHED
    assert result.conflict_level == ConflictLevel.NONE


def test_anchor_only_document_keeps_time_anchor_role_when_also_routed():
    file_result = {
        'file': '手术记录 (record-1)',
        'matched': False,
        'reason': '文件中无相关日期/数值字段，无法判断',
        'fields': '手术日期: 2026年06月10日 15:29--2026年06月10日 16:15',
    }
    source = {'file': '手术记录 (record-1)', 'template': 'SurgeryRecord'}

    annotate_evidence_source(
        file_result,
        source,
        primary_source_id='drug-interaction',
        routed_documents=['手术记录'],
        anchor_documents=['手术记录'],
        anchor_sections=['手术日期'],
    )

    assert file_result['source_role'] == EvidenceRole.TIME_ANCHOR.value


def test_anchor_document_still_votes_when_it_contains_condition_fields():
    file_result = {
        'file': '手术记录 (record-1)',
        'matched': True,
        'reason': '术前诊断符合条件',
        'fields': '术前诊断: 胃癌\n手术日期: 2026年06月10日 15:29',
    }
    source = {'file': '手术记录 (record-1)', 'template': 'SurgeryRecord'}

    annotate_evidence_source(
        file_result,
        source,
        primary_source_id='diagnosis-query',
        routed_documents=['手术记录'],
        anchor_documents=['手术记录'],
        anchor_sections=['手术日期'],
    )

    assert file_result['source_role'] == EvidenceRole.SUPPORTING.value


def test_source_annotation_uses_ids_and_metadata_instead_of_display_name():
    file_result = {"file": "arbitrary display text", "matched": True, "reason": "matched"}
    source = {
        "file": "arbitrary display text",
        "service_id": "service-42",
        "semantic": {"entity_type": "lab"},
    }

    annotate_evidence_source(
        file_result,
        source,
        primary_source_id="service-42",
        time_source_id="other-service.anchor",
    )

    assert file_result["source_role"] == EvidenceRole.PRIMARY.value
    assert file_result["service_id"] == "service-42"
    assert file_result["source_type"] == "service"


def test_source_annotation_exposes_generic_machine_contract_for_future_skill():
    file_result = {"file": "任意影像服务返回", "matched": True, "reason": "matched"}
    source = {
        "file": "任意影像服务返回",
        "label": "影像结果查询",
        "service_id": "future-imaging-skill",
        "semantic": {
            "entity_type": "imaging",
            "domain": "radiology",
            "evidence_types": ["imaging_evidence", "report_evidence"],
            "presentation": {"record_type": "imaging_report"},
        },
    }

    annotate_evidence_source(
        file_result,
        source,
        primary_source_id="future-imaging-skill",
    )

    assert file_result["service_id"] == "future-imaging-skill"
    assert file_result["source_kind"] == "service"
    assert file_result["source_label"] == "影像结果查询"
    assert file_result["domain"] == "radiology"
    assert file_result["entity_type"] == "imaging"
    assert file_result["evidence_type"] == "imaging_evidence"
    assert file_result["evidence_types"] == ["imaging_evidence", "report_evidence"]
    assert file_result["record_type"] == "imaging_report"
    assert file_result["source_role"] == EvidenceRole.PRIMARY.value


def test_confidence_uses_generic_service_identity_for_future_skill():
    result = assess_file_confidence(
        {
            "file": "任意未来风险评分",
            "service_id": "future-risk-score",
            "source_kind": "service",
            "source_type": "service",
            "matched": True,
            "reason": "候选记录满足条件",
            "fields": "score: 7",
        }
    )

    assert result["置信度"] == 0.78
    assert result["依据等级"] == "结构化接口字段"


def test_sync_condition_result_aligns_legacy_and_unified_fields():
    legacy = {"condition": "target condition", "matched": True, "reason": "legacy matched"}
    unified = build_condition_result(
        {
            "condition": "target condition",
            "matched": True,
            "reason": "legacy matched",
            "files": [
                _source_result("primary-a", "MATCHED", "PRIMARY", "service-a"),
                _source_result("primary-b", "NOT_MATCHED", "PRIMARY", "service-b"),
            ],
        },
        "c-sync",
    )

    sync_condition_result(legacy, unified)

    assert legacy["matched"] is False
    assert legacy["status"] == "UNKNOWN"
    assert legacy["判断状态"] == "无法判断"
    assert legacy["可判定"] is False
    assert legacy["reason_code"] == "EVIDENCE_CONFLICT"


def test_combine_condition_statuses_preserves_four_state_and_semantics():
    assert combine_condition_statuses(
        [EvidenceStatus.MATCHED, EvidenceStatus.NOT_MENTIONED], use_and=True
    ) == EvidenceStatus.NOT_MENTIONED
    assert combine_condition_statuses(
        [EvidenceStatus.MATCHED, EvidenceStatus.UNKNOWN], use_and=True
    ) == EvidenceStatus.UNKNOWN
    assert combine_condition_statuses(
        [EvidenceStatus.MATCHED, EvidenceStatus.NOT_MATCHED], use_and=True
    ) == EvidenceStatus.NOT_MATCHED
    assert combine_condition_statuses(
        [EvidenceStatus.NOT_MATCHED, EvidenceStatus.NOT_MENTIONED], use_and=False
    ) == EvidenceStatus.NOT_MENTIONED
    assert combine_condition_statuses(
        [EvidenceStatus.NOT_MATCHED, EvidenceStatus.UNKNOWN], use_and=False
    ) == EvidenceStatus.UNKNOWN
    assert combine_condition_statuses(
        [EvidenceStatus.NOT_MATCHED, EvidenceStatus.MATCHED], use_and=False
    ) == EvidenceStatus.MATCHED


def test_sync_condition_result_preserves_not_mentioned():
    legacy = {"condition": "有过高血压", "matched": False, "reason": "未找到高血压"}

    sync_condition_result(
        legacy,
        {
            "status": "NOT_MENTIONED",
            "reason_code": "NO_MATCHING_RECORD",
            "reason": "诊断数据源查询成功，但未找到高血压记录",
        },
    )

    assert legacy["matched"] is False
    assert legacy["status"] == "NOT_MENTIONED"
    assert legacy["判断状态"] == "未提及"
    assert legacy["可判定"] is True
    assert legacy["reason_code"] == "NO_MATCHING_RECORD"


def test_confidence_layer_respects_canonical_status_after_source_takeover():
    condition = {
        "condition": "target condition",
        "matched": False,
        "reason": "primary unavailable",
        "files": [
            _source_result(
                "primary",
                "UNKNOWN",
                "PRIMARY",
                "service-a",
                reason="外部数据源调用失败",
                quality="SOURCE_ERROR",
            ),
            _source_result("support", "NOT_MATCHED", "SUPPORTING", "service-b"),
        ],
    }
    unified = build_condition_result(condition, "c-confidence")
    sync_condition_result(condition, unified)

    condition_confidence = assess_condition_confidence(condition)
    patient_confidence = assess_patient_confidence(False, condition["reason"], {"c": condition})

    assert unified.status == EvidenceStatus.NOT_MATCHED
    assert condition_confidence["判断状态"] == "不符合"
    assert condition_confidence["可判定"] is True
    assert patient_confidence["判断状态"] == "不符合"
    assert patient_confidence["可判定"] is True
