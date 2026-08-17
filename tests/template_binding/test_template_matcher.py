from microharness.template_binding.template_matcher import TemplateMatcher


def _templates():
    return [
        {"id": "s1", "category_id": "c1", "category_name": "入院记录", "name": "V1.0"},
        {"id": "s2", "category_id": "c2", "category_name": "出院记录", "name": "V1.0"},
    ]


def test_existing_template_mapping_has_highest_priority():
    result = TemplateMatcher().match(
        html_template={"html_name": "出院记录", "category_name": "出院记录"},
        html_nodes=[],
        standard_templates=_templates(),
        existing_template_mappings=[{"standard_xml_id": "s1"}],
        existing_mapping_policy="authoritative",
    )

    assert result["status"] == "MATCHED"
    assert result["selected_template_id"] == "s1"
    assert result["candidates"][0]["source"] == "existing"


def test_manual_template_must_belong_to_server_candidate_set():
    result = TemplateMatcher().match(
        html_template={"html_name": "入院记录", "category_name": "入院记录"},
        html_nodes=[],
        standard_templates=_templates(),
        requested_standard_template_id="unknown",
    )

    assert result["status"] == "FAILED"
    assert result["selected_template_id"] is None


def test_multiple_existing_template_mappings_cannot_be_bypassed_manually():
    result = TemplateMatcher().match(
        html_template={"html_name": "入院记录", "category_name": "入院记录"},
        html_nodes=[],
        standard_templates=_templates(),
        existing_template_mappings=[
            {"standard_xml_id": "s1"},
            {"standard_xml_id": "s2"},
        ],
        requested_standard_template_id="s1",
        existing_mapping_policy="authoritative",
    )

    assert result["status"] == "CONFLICT"
    assert result["selected_template_id"] is None


def test_existing_template_mapping_is_only_reference_by_default():
    result = TemplateMatcher().match(
        html_template={"html_name": "鍑洪櫌璁板綍", "category_name": "鍑洪櫌璁板綍"},
        html_nodes=[],
        standard_templates=_templates(),
        existing_template_mappings=[{"standard_xml_id": "s1"}],
    )

    assert result["candidates"][0]["source"] == "rule"
    assert result["selected_template_id"] != ""


class _SequenceClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, temperature=0.0):
        self.calls.append(messages)
        return self.responses.pop(0)


def test_template_llm_retries_when_first_selected_id_is_outside_candidates():
    client = _SequenceClient(
        '{"selected_template_id":"invented-id","confidence":0.99,"reason":"bad"}',
        '{"selected_template_id":"`s2`","confidence":0.91,"reason":"出院记录更匹配"}',
    )

    result = TemplateMatcher().match(
        html_template={"html_name": "临床记录", "category_name": "临床记录"},
        html_nodes=[],
        standard_templates=_templates(),
        llm_client=client,
    )

    assert len(client.calls) == 2
    assert result["selected_template_id"] == "s2"
    assert result["candidates"][0]["template_id"] == "s2"
    assert not any("模板 LLM 重排失败" in warning for warning in result["warnings"])
    retry_prompt = client.calls[1][1]["content"]
    assert "allowed_standard_template_ids" in retry_prompt
    assert "s1" in retry_prompt
    assert "s2" in retry_prompt


def test_template_llm_keeps_rule_result_when_retry_also_returns_unknown_id():
    client = _SequenceClient(
        '{"selected_template_id":"invented-id-1","confidence":0.99}',
        '{"selected_template_id":"入院记录","confidence":0.99}',
    )

    result = TemplateMatcher().match(
        html_template={"html_name": "临床记录", "category_name": "临床记录"},
        html_nodes=[],
        standard_templates=_templates(),
        llm_client=client,
    )

    assert len(client.calls) == 2
    assert result["selected_template_id"] == "s1"
    assert result["candidates"][0]["template_id"] == "s1"
    assert any("模板 LLM 重排失败" in warning for warning in result["warnings"])
    assert "候选 ID 纠正重试仍失败" in result["warnings"][0]


def test_inpatient_record_alias_limits_llm_to_admission_template_family():
    client = _SequenceClient(
        '{"selected_template_id":"admission-v2","confidence":0.96,"reason":"版本更匹配"}'
    )
    templates = [
        {
            "id": "admission-v1",
            "category_id": "admission",
            "category_name": "入院记录",
            "name": "V1.0",
        },
        {
            "id": "admission-v2",
            "category_id": "admission",
            "category_name": "入院记录",
            "name": "V2.0",
        },
        {
            "id": "discharge-v2",
            "category_id": "discharge",
            "category_name": "出院记录",
            "name": "V2.0",
        },
    ]

    result = TemplateMatcher().match(
        html_template={
            "html_name": "全科医学科住院志",
            "category_name": "全科医学科住院志",
        },
        html_nodes=[],
        standard_templates=templates,
        llm_client=client,
    )

    assert result["selected_template_id"] == "admission-v2"
    assert {item["template_id"] for item in result["candidates"]} == {
        "admission-v1",
        "admission-v2",
    }
    assert all(
        item["features"]["document_family_match"] is True
        for item in result["candidates"]
    )
    assert "discharge-v2" not in client.calls[0][1]["content"]
