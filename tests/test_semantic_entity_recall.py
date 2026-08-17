import json
from pathlib import Path

from microharness.medical.document_semantics import (
    MATCHED,
    NOT_MATCHED,
    NOT_MENTIONED,
    UNKNOWN,
)
from microharness.medical.semantic_entity_recall import (
    aggregate_semantic_entity_decisions,
    assess_semantic_entity_recall,
    candidate_needing_equivalence,
    parse_semantic_candidate_batch,
    semantic_candidate_retry_required,
    symptom_relation_review_required,
)
from microharness.ollama.model_profile import ModelProfile
from microharness.ollama.prompt_adapter import (
    build_judge_prompt,
    build_semantic_candidate_retry_prompt,
    build_semantic_equivalence_prompt,
    build_semantic_symptom_relation_prompt,
)


CLINICAL_ENTAILMENT = {
    "query_kind": "SYMPTOM_OR_SIGN",
    "source_kind": "SYMPTOM_OR_SIGN",
    "relation": "SAME_CONCEPT",
    "reason": "原文概念能够直接证明查询概念",
}
SAME_SYMPTOM = {
    "relation": "SAME_SYMPTOM",
    "reason": "两个短语描述同一症状",
}


def _candidate(entity, evidence):
    return {
        "candidate_found": True,
        "matched_entity": entity,
        "evidence_span": evidence,
    }


def _assess(
    payload,
    source_text,
    query_entity="发烧",
    candidates=None,
    equivalence=None,
    symptom_relation=None,
):
    return assess_semantic_entity_recall(
        payload,
        query_entity=query_entity,
        entity_candidates=candidates or [query_entity],
        source_text=source_text,
        equivalence_payload=equivalence,
        symptom_relation_payload=symptom_relation,
        condition=query_entity,
    )


def test_strict_equivalent_positive_expression_is_matched():
    result = _assess(
        _candidate("发热", "患者发热两天。"),
        "主诉: 患者发热两天。",
        equivalence=CLINICAL_ENTAILMENT,
        symptom_relation=SAME_SYMPTOM,
    )

    assert result.status == MATCHED
    assert result.reason_code == "DOCUMENT_POSITIVE_ASSERTION"
    assert "发热" in result.reason


def test_strict_equivalent_negated_expression_is_not_matched():
    result = _assess(
        _candidate("发热", "患者否认发热。"),
        "现病史: 患者否认发热。",
        equivalence=CLINICAL_ENTAILMENT,
        symptom_relation=SAME_SYMPTOM,
    )

    assert result.status == NOT_MATCHED
    assert result.reason_code == "DOCUMENT_EXPLICIT_NEGATION"


def test_complete_document_without_candidate_is_not_mentioned():
    result = _assess(
        {"candidate_found": False, "matched_entity": "", "evidence_span": ""},
        "现病史: 患者无胸痛及腹痛。",
    )

    assert result.status == NOT_MENTIONED
    assert result.reason_code == "SEMANTIC_ENTITY_NOT_MENTIONED"


def test_observation_does_not_prove_diagnosis_when_audit_rejects_it():
    result = _assess(
        _candidate("血压偏高", "本次测量血压偏高。"),
        "查体: 本次测量血压偏高。",
        query_entity="高血压",
        equivalence={
            "query_kind": "DIAGNOSIS",
            "source_kind": "OBSERVATION_OR_MEASUREMENT",
            "relation": "RELATED_ONLY",
            "reason": "观察值不能直接证明疾病诊断",
        },
    )

    assert result.status == NOT_MENTIONED
    assert result.reason_code == "SEMANTIC_ENTITY_NOT_MENTIONED"
    assert "观察值" in result.reason


def test_strict_equivalent_longer_symptom_expression_is_matched():
    result = _assess(
        _candidate("胸背部疼痛", "患者诉胸背部疼痛3天。"),
        "主诉: 患者诉胸背部疼痛3天。",
        query_entity="背痛",
        equivalence={**CLINICAL_ENTAILMENT, "relation": "SOURCE_MORE_SPECIFIC"},
        symptom_relation={
            "relation": "SOURCE_QUALIFIED_SAME_SYMPTOM",
            "reason": "原文增加了具体部位限定",
        },
    )

    assert result.status == MATCHED


def test_assertion_kind_mismatch_vetoes_positive_model_booleans():
    result = _assess(
        _candidate("血压偏高", "本次测量血压偏高。"),
        "查体: 本次测量血压偏高。",
        query_entity="高血压",
        equivalence={
            **CLINICAL_ENTAILMENT,
            "query_kind": "DIAGNOSIS",
            "source_kind": "OBSERVATION_OR_MEASUREMENT",
            "reason": "模型错误地认为观察值足以证明诊断",
        },
    )

    assert result.status == NOT_MENTIONED
    assert "断言层级不兼容" in result.reason


def test_known_lexical_candidate_does_not_require_second_audit():
    result = _assess(
        _candidate("发烧", "患者发烧两天。"),
        "主诉: 患者发烧两天。",
    )

    assert result.status == MATCHED


def test_candidate_helper_only_returns_valid_nonlexical_entity():
    payload = _candidate("发热", "患者发热两天。")

    assert candidate_needing_equivalence(
        payload,
        query_entity="发烧",
        entity_candidates=["发烧"],
        source_text="主诉: 患者发热两天。",
    ) == "发热"
    assert candidate_needing_equivalence(
        _candidate("发烧", "患者发烧两天。"),
        query_entity="发烧",
        entity_candidates=["发烧"],
        source_text="主诉: 患者发烧两天。",
    ) == ""


def test_non_verbatim_evidence_is_unknown():
    result = _assess(
        _candidate("发热", "患者持续高热。"),
        "现病史: 患者发热两天。",
        equivalence=CLINICAL_ENTAILMENT,
    )

    assert result.status == UNKNOWN
    assert result.reason_code == "SEMANTIC_RECALL_NON_VERBATIM_EVIDENCE"


def test_matched_entity_must_occur_inside_evidence_span():
    result = _assess(
        _candidate("发热", "患者体温升高。"),
        "现病史: 患者体温升高。",
        equivalence=CLINICAL_ENTAILMENT,
    )

    assert result.status == UNKNOWN
    assert result.reason_code == "SEMANTIC_RECALL_ENTITY_OUTSIDE_EVIDENCE"


def test_invalid_or_incomplete_extraction_output_is_unknown():
    result = _assess({}, "现病史: 患者发热两天。")

    assert result.status == UNKNOWN
    assert result.reason_code == "SEMANTIC_RECALL_MISSING_CANDIDATE_FLAG"


def test_nonlexical_candidate_without_second_audit_is_unknown():
    result = _assess(
        _candidate("发热", "患者发热两天。"),
        "现病史: 患者发热两天。",
    )

    assert result.status == UNKNOWN
    assert result.reason_code == "SEMANTIC_EQUIVALENCE_REVIEW_MISSING"


def test_incomplete_equivalence_audit_is_unknown():
    result = _assess(
        _candidate("发热", "患者发热两天。"),
        "现病史: 患者发热两天。",
        equivalence={"same_concept": True},
    )

    assert result.status == UNKNOWN
    assert result.reason_code == "SEMANTIC_EQUIVALENCE_REVIEW_INVALID"


def test_distinct_symptom_vetoes_positive_entailment_audit():
    result = _assess(
        _candidate("呕吐", "患者呕吐两次。"),
        "现病史: 患者呕吐两次。",
        query_entity="恶心",
        equivalence=CLINICAL_ENTAILMENT,
        symptom_relation={
            "relation": "DISTINCT_SYMPTOMS",
            "reason": "两者可以作为独立症状分别记录",
        },
    )

    assert result.status == NOT_MENTIONED
    assert result.reason_code == "SEMANTIC_ENTITY_NOT_MENTIONED"
    assert "不同的临床表现" in result.reason


def test_missing_required_symptom_review_is_unknown():
    result = _assess(
        _candidate("发热", "患者发热两天。"),
        "现病史: 患者发热两天。",
        equivalence=CLINICAL_ENTAILMENT,
    )

    assert result.status == UNKNOWN
    assert result.reason_code == "SEMANTIC_SYMPTOM_RELATION_REVIEW_MISSING"


def test_uncertain_symptom_review_is_unknown():
    result = _assess(
        _candidate("气促", "患者活动后气促。"),
        "现病史: 患者活动后气促。",
        query_entity="呼吸困难",
        equivalence={**CLINICAL_ENTAILMENT, "relation": "SOURCE_MORE_SPECIFIC"},
        symptom_relation={"relation": "UNCERTAIN", "reason": "无法可靠分类"},
    )

    assert result.status == UNKNOWN
    assert result.reason_code == "SEMANTIC_SYMPTOM_RELATION_UNCERTAIN"


def test_symptom_review_is_only_required_for_accepted_same_kind_symptoms():
    assert symptom_relation_review_required(CLINICAL_ENTAILMENT) is True
    assert symptom_relation_review_required({
        **CLINICAL_ENTAILMENT,
        "query_kind": "DIAGNOSIS",
        "source_kind": "DIAGNOSIS",
    }) is False
    assert symptom_relation_review_required({
        **CLINICAL_ENTAILMENT,
        "relation": "RELATED_ONLY",
    }) is False


def test_legacy_exact_relation_must_match_a_known_candidate():
    result = _assess(
        {
            "entity_mentioned": True,
            "semantic_relation": "EXACT",
            "matched_entity": "发热",
            "evidence_span": "患者发热两天。",
        },
        "现病史: 患者发热两天。",
        candidates=["发烧"],
    )

    assert result.status == UNKNOWN
    assert result.reason_code == "SEMANTIC_RECALL_EXACT_MISMATCH"


def test_multi_candidate_batch_preserves_distinct_source_spans_and_deduplicates():
    payload = {
        'candidate_found': True,
        'search_complete': True,
        'candidates': [
            _candidate('发热', '患者早期否认发热。'),
            _candidate('高热', '入院后出现持续高热。'),
            _candidate('高热', '入院后出现持续高热。'),
        ],
    }

    batch = parse_semantic_candidate_batch(payload)

    assert batch.complete is True
    assert batch.valid is True
    assert len(batch.candidates) == 2
    assert [item['matched_entity'] for item in batch.candidates] == ['发热', '高热']


def test_multi_candidate_positive_evidence_wins_over_explicit_negative():
    source = '现病史: 患者早期否认发热。入院后出现持续高热。'
    payload = {
        'candidate_found': True,
        'search_complete': True,
        'candidates': [
            _candidate('发热', '患者早期否认发热。'),
            _candidate('高热', '入院后出现持续高热。'),
        ],
    }
    batch = parse_semantic_candidate_batch(payload)
    negative = _assess(
        batch.candidates[0],
        source,
        equivalence=CLINICAL_ENTAILMENT,
        symptom_relation=SAME_SYMPTOM,
    )
    positive = _assess(
        batch.candidates[1],
        source,
        equivalence={**CLINICAL_ENTAILMENT, 'relation': 'SOURCE_MORE_SPECIFIC'},
        symptom_relation={
            'relation': 'SOURCE_QUALIFIED_SAME_SYMPTOM',
            'reason': '原文增加了程度限定',
        },
    )

    result = aggregate_semantic_entity_decisions(
        [negative, positive], query_entity='发烧', batch=batch
    )

    assert negative.status == NOT_MATCHED
    assert positive.status == MATCHED
    assert result.status == MATCHED
    assert result.trace[0]['status_counts'][MATCHED] == 1
    assert len([
        item for item in result.trace
        if item.get('stage') == 'semantic_candidate_result'
    ]) == 2


def test_multi_candidate_explicit_negative_wins_over_not_mentioned():
    batch = parse_semantic_candidate_batch({
        'candidate_found': True,
        'search_complete': True,
        'candidates': [
            _candidate('发热', '患者否认发热。'),
            _candidate('咳痰', '患者伴有咳痰。'),
        ],
    })
    negative = _assess(
        batch.candidates[0],
        '现病史: 患者否认发热。患者伴有咳痰。',
        equivalence=CLINICAL_ENTAILMENT,
        symptom_relation=SAME_SYMPTOM,
    )
    unrelated = _assess(
        batch.candidates[1],
        '现病史: 患者否认发热。患者伴有咳痰。',
        equivalence={
            **CLINICAL_ENTAILMENT,
            'relation': 'RELATED_ONLY',
            'reason': '相关但属于不同症状',
        },
    )

    result = aggregate_semantic_entity_decisions(
        [negative, unrelated], query_entity='发烧', batch=batch
    )

    assert result.status == NOT_MATCHED
    assert result.reason_code == 'DOCUMENT_EXPLICIT_NEGATION'


def test_empty_complete_multi_candidate_batch_is_not_mentioned():
    batch = parse_semantic_candidate_batch({
        'candidate_found': False,
        'search_complete': True,
        'candidates': [],
    })

    result = aggregate_semantic_entity_decisions(
        [], query_entity='发烧', batch=batch
    )

    assert result.status == NOT_MENTIONED
    assert result.reason_code == 'SEMANTIC_ENTITY_NOT_MENTIONED'


def test_candidate_overflow_without_positive_result_is_unknown():
    payload = {
        'candidate_found': True,
        'search_complete': True,
        'candidates': [
            _candidate(f'候选{index}', f'原文候选{index}。')
            for index in range(6)
        ],
    }
    batch = parse_semantic_candidate_batch(payload)
    decisions = [
        _assess(
            item,
            ''.join(candidate['evidence_span'] for candidate in batch.candidates),
            equivalence={
                **CLINICAL_ENTAILMENT,
                'relation': 'RELATED_ONLY',
                'reason': '不能单独证明查询症状',
            },
        )
        for item in batch.candidates
    ]

    result = aggregate_semantic_entity_decisions(
        decisions, query_entity='目标症状', batch=batch
    )

    assert batch.overflow is True
    assert result.status == UNKNOWN
    assert result.reason_code == 'SEMANTIC_CANDIDATE_SEARCH_INCOMPLETE'


def test_multi_candidate_unknown_wins_when_no_candidate_is_decisive():
    batch = parse_semantic_candidate_batch({
        'candidate_found': True,
        'search_complete': True,
        'candidates': [_candidate('发热', '患者可能发热。')],
    })
    uncertain = _assess(
        batch.candidates[0],
        '现病史: 患者可能发热。',
        equivalence=CLINICAL_ENTAILMENT,
        symptom_relation=SAME_SYMPTOM,
    )

    result = aggregate_semantic_entity_decisions(
        [uncertain], query_entity='发烧', batch=batch
    )

    assert uncertain.status == UNKNOWN
    assert result.status == UNKNOWN


def test_invalid_multi_candidate_item_is_not_silently_discarded():
    batch = parse_semantic_candidate_batch({
        'candidate_found': True,
        'search_complete': True,
        'candidates': ['not-an-object'],
    })

    result = aggregate_semantic_entity_decisions(
        [], query_entity='发烧', batch=batch
    )

    assert batch.valid is False
    assert result.status == UNKNOWN
    assert result.reason_code == 'SEMANTIC_CANDIDATE_BATCH_INVALID'


def test_semantic_candidate_prompt_requires_verbatim_entity_extraction():
    prompt = build_judge_prompt(
        ModelProfile(),
        "患者发烧",
        "",
        "现病史: 患者发热两天。",
        "",
        semantic_recall=True,
        query_entity="发烧",
        entity_candidates=["发烧"],
        entity_type="diagnosis",
    )

    assert "candidate_found" in prompt
    assert "matched_entity" in prompt
    assert "evidence_span" in prompt
    assert "连续子串" in prompt
    assert "不判断同义关系" in prompt
    assert "matched_entity 不得包含“否认、无、未见" in prompt
    assert "evidence_span 应保留“患者否认咳嗽”" in prompt
    assert "不得只抽取项目名称" in prompt


    assert 'search_complete' in prompt
    assert 'candidates' in prompt
    assert '最多返回5个' in prompt


def test_semantic_entailment_prompt_requires_four_independent_checks():
    prompt = build_semantic_equivalence_prompt("高血压", "血压偏高", "diagnosis")

    assert "query_kind" in prompt
    assert "source_kind" in prompt
    assert "OBSERVATION_OR_MEASUREMENT" in prompt
    assert "SOURCE_MORE_SPECIFIC" in prompt
    assert "RELATED_ONLY" in prompt
    assert "relation" in prompt
    assert "疾病诊断不能由症状" in prompt
    assert "不能返回布尔值" in prompt
    assert "查询类型提示" not in prompt


def test_semantic_candidate_retry_only_targets_missing_completeness_flag():
    batch = parse_semantic_candidate_batch({
        'candidate_found': True,
        'search_complete': False,
        'candidates': [_candidate('双肺结节灶', '双肺结节灶。')],
    })

    assert batch.valid is True
    assert batch.complete is False
    assert semantic_candidate_retry_required(batch, '双肺结节灶。') is True
    assert semantic_candidate_retry_required(
        batch, '双肺结节灶。', source_complete=False
    ) is False


def test_semantic_candidate_retry_does_not_relax_other_incomplete_cases():
    complete = parse_semantic_candidate_batch({
        'candidate_found': True,
        'search_complete': True,
        'candidates': [_candidate('肺炎', '肺炎。')],
    })
    overflow = parse_semantic_candidate_batch({
        'candidate_found': True,
        'search_complete': False,
        'candidates': [_candidate(str(index), str(index)) for index in range(6)],
    })
    invalid = parse_semantic_candidate_batch({
        'candidate_found': True,
        'search_complete': False,
        'candidates': ['invalid'],
    })

    assert semantic_candidate_retry_required(complete, '肺炎。') is False
    assert semantic_candidate_retry_required(overflow, '肺炎。') is False
    assert semantic_candidate_retry_required(invalid, '肺炎。') is False
    assert semantic_candidate_retry_required(
        parse_semantic_candidate_batch({
            'candidate_found': True,
            'search_complete': False,
            'candidates': [_candidate('肺炎', '肺炎。')],
        }),
        'x' * 1501,
    ) is False


def test_semantic_candidate_retry_prompt_requires_complete_json_scan():
    prompt = build_semantic_candidate_retry_prompt(
        '肺炎', '肺炎', '出院诊断：双肺结节灶。', '模型未确认候选搜索完整'
    )

    assert 'search_complete' in prompt
    assert '完整病历原文' in prompt
    assert 'candidates 为空而返回 false' in prompt
    assert '连续子串' in prompt
    assert '严禁补写原文不存在的实体' in prompt
    assert '出院诊断：双肺结节灶。' in prompt


def test_semantic_recall_gold_cases_follow_four_state_contract():
    gold_path = (
        Path(__file__).parents[1]
        / 'evaluation'
        / 'medical_filter'
        / 'semantic_recall_gold.json'
    )
    gold = json.loads(gold_path.read_text(encoding='utf-8'))

    for case in gold['cases']:
        raw_candidates = case['candidates']
        batch = parse_semantic_candidate_batch({
            'candidate_found': bool(raw_candidates),
            'search_complete': True,
            'candidates': [
                {
                    'matched_entity': item['matched_entity'],
                    'evidence_span': item['evidence_span'],
                }
                for item in raw_candidates
            ],
        })
        assert batch.valid is True, case['id']
        assert batch.complete is True, case['id']
        assert len(batch.candidates) == len(raw_candidates), case['id']
        decisions = [
            assess_semantic_entity_recall(
                candidate,
                query_entity=case['query_entity'],
                entity_candidates=[case['query_entity']],
                source_text=case['source_text'],
                equivalence_payload=raw_candidate.get('equivalence'),
                symptom_relation_payload=raw_candidate.get('symptom_relation'),
                condition=case['query_entity'],
            )
            for candidate, raw_candidate in zip(batch.candidates, raw_candidates)
        ]
        result = aggregate_semantic_entity_decisions(
            decisions,
            query_entity=case['query_entity'],
            batch=batch,
        )

        assert result.status == case['expected_status'], case['id']


def test_symptom_relation_prompt_uses_enum_instead_of_boolean():
    prompt = build_semantic_symptom_relation_prompt("恶心", "呕吐")

    assert "SAME_SYMPTOM" in prompt
    assert "SOURCE_QUALIFIED_SAME_SYMPTOM" in prompt
    assert "DISTINCT_SYMPTOMS" in prompt
    assert "UNCERTAIN" in prompt
    assert "不得返回布尔值" in prompt
    assert "经常伴随" in prompt
