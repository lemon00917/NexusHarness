from microharness.template_binding.node_matcher import NodeMatcher


def test_exact_field_name_produces_automatic_node_recommendation():
    result = NodeMatcher().match(
        standard_nodes=[
            {
                "id": "n1",
                "template_id": "s1",
                "node_cn": "主诉",
                "node_en": "chiefComplaint",
                "path_text": "入院记录/主诉",
                "bindable": True,
                "order": 1,
            }
        ],
        html_nodes=[
            {
                "node_key": "html-node-1",
                "display_text": "主诉",
                "placeholder": "主诉",
                "mapping_value": "主诉",
                "section": "入院记录",
                "selectors": ["chiefComplaint"],
                "order": 1,
            }
        ],
    )

    assert result["status"] == "COMPLETED"
    assert result["mappings"][0]["status"] == "AUTO"
    assert result["mappings"][0]["html_node_keys"] == ["html-node-1"]


def test_existing_mapping_can_use_html_node_code_selector():
    result = NodeMatcher().match(
        standard_nodes=[
            {"id": "n1", "template_id": "s1", "node_cn": "主诉", "bindable": True, "order": 1}
        ],
        html_nodes=[
            {"node_key": "html-node-1", "display_text": "主诉", "selectors": ["chiefComplaint"], "order": 1}
        ],
        existing_node_mappings=[
            {"id": "m1", "standard_node_id": "n1", "html_node_id": "", "html_node_code": "chiefComplaint"}
        ],
        existing_mapping_policy="authoritative",
    )

    assert result["mappings"][0]["status"] == "EXISTING"


def test_existing_mapping_resolves_legacy_raw_anchor_selector():
    result = NodeMatcher().match(
        standard_nodes=[
            {"id": "n1", "template_id": "s1", "node_cn": "家族史", "bindable": True, "order": 1}
        ],
        html_nodes=[
            {
                "node_key": "html-node-1",
                "display_text": "家族史",
                "selectors": ["code:S010"],
                "group_selectors": ["S010", "code:S010"],
                "order": 1,
            }
        ],
        existing_node_mappings=[
            {
                "id": "m1",
                "standard_node_id": "n1",
                "html_node_id": "S010",
                "html_node_code": "null",
                "mapping_values": "家族史：;父亲健在",
            }
        ],
        existing_mapping_policy="authoritative",
    )

    assert result["mappings"][0]["status"] == "EXISTING"
    assert result["mappings"][0]["html_node_keys"] == ["html-node-1"]


def test_top_level_section_candidates_are_limited_to_matching_html_section():
    result = NodeMatcher().match(
        standard_nodes=[
            {
                "id": "aux",
                "node_en": "text",
                "path_text": "docBody/ \u8f85\u52a9\u68c0\u67e5 /text",
                "node_value": "\u5165\u9662\u524d\u7684\u5b9e\u9a8c\u5ba4\u548c\u5f71\u50cf\u5b66\u68c0\u67e5\u7ed3\u679c",
                "bindable": True,
                "order": 1,
            }
        ],
        html_nodes=[
            {
                "node_key": "summary-value",
                "display_text": "[\u8f85\u52a9\u68c0\u67e5]",
                "section": "\u75c5\u5386\u6458\u8981",
                "mapping_value": "[\u8f85\u52a9\u68c0\u67e5]",
                "order": 1,
            },
            {
                "node_key": "aux-section",
                "display_text": "\u8f85\u52a9\u68c0\u67e5",
                "section": "\u8f85\u52a9\u68c0\u67e5",
                "mapping_value": "\u8f85\u52a9\u68c0\u67e5\uff1a",
                "scope_selectors": ["code:S015"],
                "order": 2,
            },
        ],
    )

    assert result["mappings"][0]["html_node_keys"] == ["aux-section"]
    assert [item["html_node_key"] for item in result["mappings"][0]["candidates"]] == ["aux-section"]


def test_existing_node_mapping_is_reference_and_does_not_skip_auto_matching():
    result = NodeMatcher().match(
        standard_nodes=[
            {"id": "n1", "template_id": "s1", "node_cn": "chief complaint", "bindable": True, "order": 1}
        ],
        html_nodes=[
            {"node_key": "html-node-1", "display_text": "chief complaint", "selectors": ["chiefComplaint"], "order": 1}
        ],
        existing_node_mappings=[
            {"id": "m1", "standard_node_id": "n1", "html_node_id": "chiefComplaint", "html_node_code": "chiefComplaint"}
        ],
    )

    assert result["reference_existing_count"] == 1
    assert result["mappings"]
    assert result["mappings"][0]["status"] in {"AUTO", "REVIEW_REQUIRED"}
    assert result["mappings"][0]["status"] != "EXISTING"


def test_ambiguous_candidates_are_semantically_reranked_by_llm():
    class FakeClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, temperature=0.0):
            self.calls += 1
            return '{"mappings":[{"standard_node_id":"n1","html_node_key":"html-node-1","confidence":0.96,"reason":"semantic match"}]}'

    client = FakeClient()
    result = NodeMatcher(auto_threshold=1.1, review_threshold=0.0).match(
        standard_nodes=[
            {
                "id": "n1",
                "node_cn": "chief complaint",
                "node_en": "chiefComplaint",
                "path_text": "admission/chief complaint",
                "bindable": True,
                "order": 1,
            }
        ],
        html_nodes=[
            {
                "node_key": "html-node-1",
                "display_text": "chief complaint",
                "placeholder": "chief complaint",
                "mapping_value": "chief complaint",
                "section": "admission",
                "selectors": ["chiefComplaint"],
                "order": 1,
            }
        ],
        llm_client=client,
    )

    assert client.calls == 1
    assert result["llm"] == {
        "enabled": True,
        "attempted": 1,
        "selected": 1,
        "used": True,
        "error": False,
        "mode": "semantic_rerank",
    }
    assert result["mappings"][0]["source"] == "rule+llm"


def test_low_score_but_valid_llm_choice_is_kept_for_review():
    class FakeClient:
        def chat(self, messages, temperature=0.0):
            return '{"mappings":[{"standard_node_id":"n1","html_node_key":"html-node-1","confidence":0.5,"reason":"业务语义可疑似对应"}]}'

    result = NodeMatcher(
        auto_threshold=1.1,
        review_threshold=0.8,
        semantic_min_score=0.0,
        llm_review_floor=0.45,
    ).match(
        standard_nodes=[
            {"id": "n1", "node_cn": "alpha target", "bindable": True, "order": 1}
        ],
        html_nodes=[
            {
                "node_key": "html-node-1",
                "display_text": "beta candidate",
                "mapping_value": "beta candidate",
                "order": 1,
            }
        ],
        llm_client=FakeClient(),
    )

    assert result["unmatched_count"] == 0
    assert result["review_count"] == 1
    assert result["mappings"][0]["status"] == "REVIEW_REQUIRED"


def test_llm_self_declared_non_match_is_rejected():
    assert NodeMatcher._semantic_reason_rejects_selection("候选不包含目标含义") is True
    assert NodeMatcher._semantic_reason_rejects_selection("章节和字段语义一致") is False


def test_malformed_json_with_missing_comma_is_repaired_before_retry():
    class FakeClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, temperature=0.0):
            self.calls += 1
            return '{"mappings":[{"standard_node_id":"n1" "html_node_key":"html-node-1","confidence":0.96,"reason":"semantic match"}]}'

    client = FakeClient()
    result = NodeMatcher(auto_threshold=1.1, review_threshold=0.0).match(
        standard_nodes=[
            {
                "id": "n1",
                "node_cn": "chief complaint",
                "node_en": "chiefComplaint",
                "path_text": "admission/chief complaint",
                "bindable": True,
                "order": 1,
            }
        ],
        html_nodes=[
            {
                "node_key": "html-node-1",
                "display_text": "chief complaint",
                "placeholder": "chief complaint",
                "mapping_value": "chief complaint",
                "section": "admission",
                "selectors": ["chiefComplaint"],
                "order": 1,
            }
        ],
        llm_client=client,
    )

    assert client.calls == 1
    assert result["llm"]["used"] is True
    assert result["llm"]["error"] is False


def test_invalid_json_is_retried_once_before_rule_fallback_or_acceptance():
    class FakeClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, temperature=0.0):
            self.calls += 1
            if self.calls == 1:
                return '{"mappings":[{"standard_node_id":"n1"'
            return '{"mappings":[{"standard_node_id":"n1","html_node_key":"html-node-1","confidence":0.96,"reason":"semantic match"}]}'

    client = FakeClient()
    result = NodeMatcher(auto_threshold=1.1, review_threshold=0.0).match(
        standard_nodes=[
            {
                "id": "n1",
                "node_cn": "chief complaint",
                "node_en": "chiefComplaint",
                "path_text": "admission/chief complaint",
                "bindable": True,
                "order": 1,
            }
        ],
        html_nodes=[
            {
                "node_key": "html-node-1",
                "display_text": "chief complaint",
                "placeholder": "chief complaint",
                "mapping_value": "chief complaint",
                "section": "admission",
                "selectors": ["chiefComplaint"],
                "order": 1,
            }
        ],
        llm_client=client,
    )

    assert client.calls == 2
    assert result["llm"]["used"] is True
    assert result["llm"]["error"] is False
    assert result["mappings"][0]["source"] == "rule+llm"


def test_malformed_batch_json_falls_back_to_single_node_retries():
    import json

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, temperature=0.0):
            self.calls += 1
            if self.calls == 1:
                # Simulate a long qwen batch response missing a delimiter.
                return '{"mappings":[{"standard_node_id":"n1"'
            payload = json.loads(messages[1]["content"].split("\n", 1)[1])
            standard_id = payload[0]["standard_node_id"]
            candidate_key = "candidate-a" if standard_id == "n1" else "candidate-b"
            return json.dumps(
                {
                    "mappings": [
                        {
                            "standard_node_id": standard_id,
                            "html_node_key": candidate_key,
                            "confidence": 0.9,
                            "reason": "单节点重试成功",
                        }
                    ]
                },
                ensure_ascii=False,
            )

    client = FakeClient()
    matcher = NodeMatcher(auto_threshold=1.1, review_threshold=0.58)

    def score_pair(standard_node, html_node, standard_count, html_count):
        return {
            "html_node_key": html_node["node_key"],
            "html_node_name": html_node["display_text"],
            "html_selectors": [],
            "mapping_values": "",
            "score": 0.32,
            "reason": "规则低分候选",
        }

    matcher._score_pair = score_pair
    result = matcher.match(
        standard_nodes=[
            {"id": "n1", "node_cn": "标准字段一", "bindable": True, "order": 1},
            {"id": "n2", "node_cn": "标准字段二", "bindable": True, "order": 2},
        ],
        html_nodes=[
            {"node_key": "candidate-a", "display_text": "候选一", "order": 1},
            {"node_key": "candidate-b", "display_text": "候选二", "order": 2},
        ],
        llm_client=client,
    )

    assert client.calls == 3
    assert result["llm"]["used"] is True
    assert result["llm"]["error"] is False
    assert not result["warnings"]
    assert {item["standard_node_id"] for item in result["mappings"]} == {"n1", "n2"}


def test_llm_can_select_a_wider_candidate_pool_and_promote_low_rule_score():
    class FakeClient:
        def chat(self, messages, temperature=0.0):
            return '{"mappings":[{"standard_node_id":"n1","html_node_key":"html-node-2","confidence":0.9,"reason":"语义一致"}]}'

    matcher = NodeMatcher(
        top_k=1,
        semantic_candidate_k=3,
        auto_threshold=1.1,
        review_threshold=0.58,
    )

    def score_pair(standard_node, html_node, standard_count, html_count):
        scores = {"html-node-1": 0.2, "html-node-2": 0.3, "html-node-3": 0.1}
        return {
            "html_node_key": html_node["node_key"],
            "html_node_name": html_node["display_text"],
            "html_selectors": [],
            "mapping_values": "",
            "score": scores[html_node["node_key"]],
            "reason": "规则低分候选",
        }

    matcher._score_pair = score_pair
    result = matcher.match(
        standard_nodes=[{"id": "n1", "node_cn": "标准字段", "bindable": True, "order": 1}],
        html_nodes=[
            {"node_key": "html-node-1", "display_text": "候选一", "order": 1},
            {"node_key": "html-node-2", "display_text": "候选二", "order": 2},
            {"node_key": "html-node-3", "display_text": "候选三", "order": 3},
        ],
        llm_client=FakeClient(),
    )

    assert result["mappings"][0]["html_node_keys"] == ["html-node-2"]
    assert result["mappings"][0]["source"] == "rule+llm"
    assert result["mappings"][0]["status"] == "REVIEW_REQUIRED"
    assert result["diagnostics"]["llm_applied_count"] == 1


def test_all_semantic_batches_are_processed_without_a_fixed_thirty_node_cap():
    class FakeClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, temperature=0.0):
            self.calls += 1
            payload = __import__("json").loads(messages[1]["content"].split("\n", 1)[1])
            mappings = [
                {
                    "standard_node_id": row["standard_node_id"],
                    "html_node_key": next(
                        candidate["html_node_key"]
                        for candidate in row["candidates"]
                        if candidate["name"] == row["standard_name"]
                    ),
                    "confidence": 0.9,
                    "reason": "名称一致",
                }
                for row in payload
            ]
            return __import__("json").dumps({"mappings": mappings}, ensure_ascii=False)

    client = FakeClient()
    standard_nodes = [
        {"id": f"n{i}", "node_cn": f"字段{i}", "bindable": True, "order": i}
        for i in range(25)
    ]
    html_nodes = [
        {
            "node_key": f"html-node-{i}",
            "display_text": f"字段{i}",
            "placeholder": f"字段{i}",
            "mapping_value": f"字段{i}",
            "order": i,
        }
        for i in range(25)
    ]

    result = NodeMatcher(
        auto_threshold=1.1,
        semantic_batch_size=10,
        semantic_candidate_k=8,
        review_threshold=0.0,
    ).match(standard_nodes=standard_nodes, html_nodes=html_nodes, llm_client=client)

    assert client.calls == 3
    assert result["llm"]["attempted"] == 25
    assert result["llm"]["selected"] == 25
    assert result["diagnostics"]["semantic_batch_count"] == 3
    assert result["mapping_count"] == 25


def test_large_html_pool_prefilters_full_scoring_and_keeps_exact_match():
    matcher = NodeMatcher(candidate_scan_k=24)
    original_score_pair = matcher._score_pair
    scored_keys = []

    def tracking_score_pair(standard_node, html_node, standard_count, html_count):
        scored_keys.append(html_node["node_key"])
        return original_score_pair(standard_node, html_node, standard_count, html_count)

    matcher._score_pair = tracking_score_pair
    html_nodes = [
        {
            "node_key": f"html-node-{index}",
            "display_text": f"unrelated field {index}",
            "section": "other section",
            "order": index,
        }
        for index in range(199)
    ]
    html_nodes.append(
        {
            "node_key": "html-node-target",
            "display_text": "chief complaint",
            "section": "admission",
            "order": 200,
        }
    )

    result = matcher.match(
        standard_nodes=[
            {
                "id": "n1",
                "node_cn": "chief complaint",
                "path_text": "admission/chief complaint",
                "bindable": True,
                "order": 1,
            }
        ],
        html_nodes=html_nodes,
    )

    assert len(scored_keys) == 24
    assert "html-node-target" in scored_keys
    assert result["mappings"][0]["html_node_keys"] == ["html-node-target"]
    assert result["diagnostics"]["possible_pair_count"] == 200
    assert result["diagnostics"]["full_score_pair_count"] == 24


def test_equivalent_label_survives_large_candidate_prefilter():
    matcher = NodeMatcher(candidate_scan_k=24)
    html_nodes = [
        {
            'node_key': f'html-node-{index}',
            'display_text': f'无关字段{index}',
            'section': '其他章节',
            'order': index,
        }
        for index in range(199)
    ]
    html_nodes.append(
        {
            'node_key': 'admit-time-target',
            'display_text': '入院时间',
            'section': '出院记录',
            'order': 200,
        }
    )

    result = matcher.match(
        standard_nodes=[
            {
                'id': 'admit-time',
                'node_cn': '入院日期时间',
                'bindable': True,
                'order': 1,
            }
        ],
        html_nodes=html_nodes,
    )

    assert result['mappings'][0]['html_node_keys'] == ['admit-time-target']
    assert result['diagnostics']['full_score_pair_count'] == 24


def test_unambiguous_field_aliases_do_not_require_llm_selection():
    result = NodeMatcher().match(
        standard_nodes=[
            {'id': 'marital', 'node_cn': '婚姻状况', 'bindable': True, 'order': 1},
            {'id': 'nation', 'node_en': 'nation', 'bindable': True, 'order': 2},
            {'id': 'course', 'node_cn': '诊疗经过', 'bindable': True, 'order': 3},
        ],
        html_nodes=[
            {'node_key': 'marital-value', 'display_text': '婚姻状态', 'order': 1},
            {'node_key': 'nation-value', 'display_text': '民族', 'order': 2},
            {'node_key': 'course-value', 'display_text': '诊治经过', 'order': 3},
        ],
    )

    by_standard_id = {item['standard_node_id']: item for item in result['mappings']}
    assert by_standard_id['marital']['html_node_keys'] == ['marital-value']
    assert by_standard_id['nation']['html_node_keys'] == ['nation-value']
    assert by_standard_id['course']['html_node_keys'] == ['course-value']
    assert all(item['status'] == 'AUTO' for item in by_standard_id.values())


def test_generic_text_leaf_inherits_parent_business_name_and_skips_structural_anchors():
    result = NodeMatcher().match(
        standard_nodes=[
            {
                "id": "n1",
                "node_cn": "",
                "node_en": "text",
                "node_value": "出院时最终诊断结论",
                "description": "出院时最终诊断结论",
                "path_text": "docBody/出院诊断/text",
                "bindable": True,
                "order": 1,
            }
        ],
        html_nodes=[
            {
                "node_key": "anchor",
                "display_text": "S007",
                "selectors": ["S007"],
                "structural": True,
                "order": 1,
            },
            {
                "node_key": "section-value",
                "display_text": "出院诊断",
                "section": "出院诊断",
                "group_labels": ["出院诊断"],
                "selectors": ["code:S007"],
                "mapping_value": "[出院诊断1]",
                "order": 2,
            },
        ],
    )

    assert result["mapping_count"] == 1
    assert result["mappings"][0]["standard_node_name"] == "出院诊断"
    assert result["mappings"][0]["html_node_keys"] == ["section-value"]


def test_clinical_synonyms_are_selected_by_generic_llm_review():
    import json

    class SemanticClient:
        def chat(self, messages, temperature=0.0):
            payload = json.loads(messages[1]['content'].split('\n', 1)[1])
            mappings = []
            expected = {
                'encounter': 'inpatient-no',
                'course': 'treatment-course',
            }
            for item in payload:
                html_node_key = expected.get(item['standard_node_id'])
                if html_node_key:
                    mappings.append(
                        {
                            'standard_node_id': item['standard_node_id'],
                            'html_node_key': html_node_key,
                            'confidence': 0.95,
                            'reason': '临床语义一致',
                        }
                    )
            return json.dumps({'mappings': mappings}, ensure_ascii=False)

    result = NodeMatcher().match(
        standard_nodes=[
            {'id': 'admit', 'node_cn': '入院日期时间', 'bindable': True, 'order': 1},
            {'id': 'discharge', 'node_cn': '出院日期时间', 'bindable': True, 'order': 2},
            {'id': 'encounter', 'node_cn': '就诊号', 'bindable': True, 'order': 3},
            {'id': 'course', 'node_cn': '诊疗经过', 'bindable': True, 'order': 4},
        ],
        html_nodes=[
            {'node_key': 'admit-time', 'display_text': '入院时间', 'order': 1},
            {'node_key': 'discharge-time', 'display_text': '出院时间', 'order': 2},
            {'node_key': 'inpatient-no', 'display_text': '住院号', 'order': 3},
            {'node_key': 'treatment-course', 'display_text': '诊治经过', 'order': 4},
        ],
        llm_client=SemanticClient(),
    )

    by_standard_id = {item['standard_node_id']: item for item in result['mappings']}
    assert by_standard_id['admit']['html_node_keys'] == ['admit-time']
    assert by_standard_id['discharge']['html_node_keys'] == ['discharge-time']
    assert by_standard_id['encounter']['html_node_keys'] == ['inpatient-no']
    assert by_standard_id['course']['html_node_keys'] == ['treatment-course']
    assert all(item['status'] == 'AUTO' for item in by_standard_id.values())
    assert by_standard_id['encounter']['source'] == 'rule+llm'
    assert by_standard_id['course']['source'] == 'rule'


def test_date_only_html_fields_match_standard_datetime_nodes():
    result = NodeMatcher().match(
        standard_nodes=[
            {'id': 'admit', 'node_cn': '入院日期时间', 'bindable': True, 'order': 1},
            {'id': 'discharge', 'node_cn': '出院日期时间', 'bindable': True, 'order': 2},
        ],
        html_nodes=[
            {
                'node_key': 'admit-date',
                'display_text': '[入院日期]',
                'placeholder': '[入院日期]',
                'mapping_value': '[入院日期]',
                'order': 1,
            },
            {
                'node_key': 'discharge-date',
                'display_text': '[出院日期]',
                'placeholder': '[出院日期]',
                'mapping_value': '[出院日期]',
                'order': 2,
            },
        ],
    )

    by_standard_id = {item['standard_node_id']: item for item in result['mappings']}
    assert by_standard_id['admit']['html_node_keys'] == ['admit-date']
    assert by_standard_id['discharge']['html_node_keys'] == ['discharge-date']
    assert by_standard_id['admit']['status'] == 'AUTO'
    assert by_standard_id['discharge']['status'] == 'AUTO'


def test_generic_representation_suffix_variants_apply_to_unseen_fields():
    result = NodeMatcher().match(
        standard_nodes=[
            {
                'id': 'operation-start',
                'node_cn': '手术开始日期时间',
                'bindable': True,
                'order': 1,
            },
            {
                'id': 'allergy-history',
                'node_cn': '过敏史内容',
                'bindable': True,
                'order': 2,
            },
        ],
        html_nodes=[
            {
                'node_key': 'operation-start-date',
                'display_text': '[手术开始日期]',
                'placeholder': '[手术开始日期]',
                'order': 1,
            },
            {
                'node_key': 'allergy-history-text',
                'display_text': '[过敏史]',
                'placeholder': '[过敏史]',
                'order': 2,
            },
        ],
    )

    by_standard_id = {item['standard_node_id']: item for item in result['mappings']}
    assert by_standard_id['operation-start']['html_node_keys'] == ['operation-start-date']
    assert by_standard_id['allergy-history']['html_node_keys'] == ['allergy-history-text']
    assert all(item['status'] == 'AUTO' for item in by_standard_id.values())


def test_unlisted_clinical_synonym_is_confirmed_by_controlled_llm_review():
    import json

    class SemanticClient:
        def __init__(self):
            self.payload = None

        def chat(self, messages, temperature=0.0):
            self.payload = json.loads(messages[1]['content'].split('\n', 1)[1])
            return json.dumps(
                {
                    'mappings': [
                        {
                            'standard_node_id': 'treatment-result',
                            'html_node_key': 'treatment-outcome',
                            'confidence': 0.95,
                            'reason': '临床含义一致',
                        }
                    ]
                },
                ensure_ascii=False,
            )

    client = SemanticClient()
    result = NodeMatcher().match(
        standard_nodes=[
            {
                'id': 'treatment-result',
                'node_cn': '治疗结果',
                'path_text': '出院记录/治疗结果',
                'bindable': True,
                'order': 1,
            }
        ],
        html_nodes=[
            {
                'node_key': 'treatment-outcome',
                'display_text': '治疗结局',
                'section': '出院记录',
                'context_text': '治疗结局：[治疗结局]',
                'order': 1,
            }
        ],
        llm_client=client,
    )

    assert result['mappings'][0]['html_node_keys'] == ['treatment-outcome']
    assert result['mappings'][0]['source'] == 'rule+llm'
    assert result['mappings'][0]['status'] == 'AUTO'
    assert client.payload[0]['standard_name_forms'] == ['治疗结果']
    assert client.payload[0]['candidates'][0]['name_forms'] == ['治疗结局']


def test_related_clinical_fields_are_reviewable_and_leaf_placeholders_win():
    import json

    class SemanticClient:
        def chat(self, messages, temperature=0.0):
            payload = json.loads(messages[1]['content'].split('\n', 1)[1])
            expected = {
                'doctor-signature': 'doctor-value',
                'admission-summary': 'admission-value',
                'discharge-dept': 'department',
            }
            mappings = []
            for item in payload:
                html_node_key = expected.get(item['standard_node_id'])
                candidate_keys = {candidate['html_node_key'] for candidate in item['candidates']}
                if html_node_key in candidate_keys:
                    mappings.append({
                        'standard_node_id': item['standard_node_id'],
                        'html_node_key': html_node_key,
                        'confidence': 0.88,
                        'reason': 'Semantic context matches the leaf value node',
                    })
            return json.dumps({'mappings': mappings})

    result = NodeMatcher().match(
        standard_nodes=[
            {'id': 'doctor-signature', 'node_cn': '医师签名', 'bindable': True, 'order': 1},
            {
                'id': 'admission-summary',
                'node_cn': '',
                'node_en': 'text',
                'path_text': 'docBody/入院情况/text',
                'node_value': '入院时主要症状和体征的简要概述',
                'bindable': True,
                'order': 2,
            },
            {
                'id': 'discharge-dept',
                'node_cn': '',
                'node_en': 'text',
                'path_text': 'docBody/就诊信息/出院科室/text',
                'bindable': True,
                'order': 3,
            },
        ],
        html_nodes=[
            {
                'node_key': 'doctor-section',
                'display_text': '经治医师',
                'mapping_value': '[经治医师];[进修实习医师]',
                'order': 1,
            },
            {'node_key': 'doctor-value', 'placeholder': '[经治医师]', 'display_text': '[经治医师]', 'order': 2},
            {
                'node_key': 'admission-section',
                'display_text': '入院病情及诊治经过',
                'section': '入院病情及诊治经过',
                'order': 3,
            },
            {
                'node_key': 'admission-value',
                'placeholder': '[病历摘要内容]',
                'display_text': '[病历摘要内容]',
                'section': '入院病情及诊治经过',
                'order': 4,
            },
            {'node_key': 'department', 'placeholder': '[科室1]', 'local_label': '科室', 'order': 5},
        ],
        llm_client=SemanticClient(),
    )

    by_standard_id = {item['standard_node_id']: item for item in result['mappings']}
    assert by_standard_id['doctor-signature']['html_node_keys'] == ['doctor-value']
    assert by_standard_id['admission-summary']['html_node_keys'] == ['admission-value']
    assert by_standard_id['discharge-dept']['html_node_keys'] == ['department']
    assert all(item['status'] == 'REVIEW_REQUIRED' for item in by_standard_id.values())


def test_related_clinical_fields_are_included_in_llm_payload():
    import json

    class InspectingClient:
        def __init__(self):
            self.messages = None

        def chat(self, messages, temperature=0.0):
            self.messages = messages
            return '{"mappings":[]}'

    client = InspectingClient()
    NodeMatcher().match(
        standard_nodes=[
            {
                'id': 'admission-summary',
                'node_cn': '',
                'node_en': 'text',
                'path_text': 'docBody/入院情况/text',
                'node_value': '入院时主要症状和体征的简要概述',
                'bindable': True,
                'order': 1,
            }
        ],
        html_nodes=[
            {
                'node_key': 'admission-value',
                'placeholder': '[病历摘要内容]',
                'display_text': '[病历摘要内容]',
                'section': '入院病情及诊治经过',
                'order': 1,
            }
        ],
        llm_client=client,
    )

    payload = json.loads(client.messages[1]['content'].split('\n', 1)[1])
    assert "broad parent" in client.messages[0]["content"]
    assert "Never invent IDs" in client.messages[0]["content"]
    assert payload[0]['candidates'][0]['html_node_key'] == 'admission-value'
    assert payload[0]['standard_name_forms']
    assert payload[0]['candidates'][0]['name_forms']


def test_empty_llm_batch_result_retries_each_standard_node():
    import json

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, temperature=0.0):
            self.calls += 1
            if self.calls == 1:
                return '{"mappings":[]}'
            return json.dumps(
                {
                    'mappings': [
                        {
                            'standard_node_id': 'course',
                            'html_node_key': 'candidate-b',
                            'confidence': 0.9,
                            'reason': '临床同义字段',
                        }
                    ]
                },
                ensure_ascii=False,
            )

    client = FakeClient()
    matcher = NodeMatcher(auto_threshold=1.1, review_threshold=0.58)

    def score_pair(standard_node, html_node, standard_count, html_count):
        scores = {'candidate-a': 0.32, 'candidate-b': 0.31}
        return {
            'html_node_key': html_node['node_key'],
            'html_node_name': html_node['display_text'],
            'html_selectors': [],
            'mapping_values': '',
            'score': scores[html_node['node_key']],
            'reason': '规则低分候选',
        }

    matcher._score_pair = score_pair
    result = matcher.match(
        standard_nodes=[
            {
                'id': 'course',
                'node_cn': '标准字段',
                'bindable': True,
                'order': 1,
            }
        ],
        html_nodes=[
            {'node_key': 'candidate-a', 'display_text': '候选一', 'order': 1},
            {'node_key': 'candidate-b', 'display_text': '候选二', 'order': 2},
        ],
        llm_client=client,
    )

    assert client.calls == 2
    assert result['mapping_count'] == 1


def test_partial_llm_batch_result_retries_only_unresolved_nodes():
    import json

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, temperature=0.0):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(
                    {
                        'mappings': [
                            {
                                'standard_node_id': 'n1',
                                'html_node_key': 'candidate-a',
                                'confidence': 0.9,
                                'reason': '首批选择',
                            }
                        ]
                    }
                )
            return json.dumps(
                {
                    'mappings': [
                        {
                            'standard_node_id': 'n2',
                            'html_node_key': 'candidate-b',
                            'confidence': 0.9,
                            'reason': '单项重试',
                        }
                    ]
                }
            )

    client = FakeClient()
    matcher = NodeMatcher(auto_threshold=1.1, review_threshold=0.58)

    def score_pair(standard_node, html_node, standard_count, html_count):
        return {
            'html_node_key': html_node['node_key'],
            'html_node_name': html_node['display_text'],
            'html_selectors': [],
            'mapping_values': '',
            'score': 0.32,
            'reason': '规则低分候选',
        }

    matcher._score_pair = score_pair
    result = matcher.match(
        standard_nodes=[
            {'id': 'n1', 'node_cn': '标准字段一', 'bindable': True, 'order': 1},
            {'id': 'n2', 'node_cn': '标准字段二', 'bindable': True, 'order': 2},
        ],
        html_nodes=[
            {'node_key': 'candidate-a', 'display_text': '候选一', 'order': 1},
            {'node_key': 'candidate-b', 'display_text': '候选二', 'order': 2},
        ],
        llm_client=client,
    )

    assert client.calls == 2
    assert {item['standard_node_id'] for item in result['mappings']} == {'n1', 'n2'}


def test_admission_discharge_direction_conflict_is_not_recommended():
    result = NodeMatcher(auto_threshold=1.1, review_threshold=0.58).match(
        standard_nodes=[
            {
                'id': 'admission-status',
                'node_cn': '入院情况',
                'path_text': '入院记录/入院情况',
                'bindable': True,
                'order': 1,
            }
        ],
        html_nodes=[
            {
                'node_key': 'discharge-status',
                'display_text': '出院情况',
                'section': '出院记录',
                'order': 1,
            }
        ],
    )

    assert result['mapping_count'] == 0


def test_llm_cannot_promote_admission_discharge_direction_conflict():
    class FakeClient:
        def chat(self, messages, temperature=0.0):
            return __import__('json').dumps({
                'mappings': [{
                    'standard_node_id': 'admission-status',
                    'html_node_key': 'discharge-status',
                    'confidence': 1.0,
                    'reason': '模型选择',
                }],
            }, ensure_ascii=False)

    result = NodeMatcher(auto_threshold=1.1, review_threshold=0.0).match(
        standard_nodes=[
            {
                'id': 'admission-status',
                'node_cn': '入院情况',
                'path_text': '入院记录/入院情况',
                'bindable': True,
                'order': 1,
            }
        ],
        html_nodes=[
            {
                'node_key': 'discharge-status',
                'display_text': '出院情况',
                'section': '出院记录',
                'order': 1,
            }
        ],
        llm_client=FakeClient(),
    )

    assert result['mapping_count'] == 0


def test_semantic_prompt_contains_standard_metadata_and_html_group_context():
    import json

    class InspectingClient:
        def __init__(self):
            self.messages = None

        def chat(self, messages, temperature=0.0):
            self.messages = messages
            return '{"mappings":[]}'

    client = InspectingClient()
    NodeMatcher(auto_threshold=1.1, review_threshold=0.0).match(
        standard_nodes=[
            {
                "id": "n1",
                "node_cn": "",
                "node_en": "text",
                "node_value": "出院时最终诊断结论",
                "description": "主要诊断、次要诊断和合并症",
                "mapping_value": "diagnosisText",
                "node_role": "value",
                "path_text": "docBody/出院诊断/text",
                "bindable": True,
                "order": 1,
            }
        ],
        html_nodes=[
            {
                "node_key": "html-node-1",
                "display_text": "出院诊断",
                "placeholder": "[出院诊断1]",
                "local_label": "出院诊断",
                "section": "出院诊断",
                "group_labels": ["出院诊断"],
                "anchor_path": ["S007", "S007_V008"],
                "selectors": ["code:S007"],
                "context_text": "出院诊断：[出院诊断1]",
                "order": 1,
            }
        ],
        llm_client=client,
    )

    payload = json.loads(client.messages[1]["content"].split("\n", 1)[1])
    standard = payload[0]
    candidate = standard["candidates"][0]
    assert standard["standard_name"] == "出院诊断"
    assert standard["node_value"] == "出院时最终诊断结论"
    assert standard["description"] == "主要诊断、次要诊断和合并症"
    assert standard["node_role"] == "value"
    assert candidate["group_labels"] == ["出院诊断"]
    assert candidate["anchor_path"] == ["S007", "S007_V008"]
    assert candidate["selectors"] == ["code:S007"]
