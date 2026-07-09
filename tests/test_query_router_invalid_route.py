from microharness.medical.query_router import QueryRouter


class FakeClient:
    def __init__(self, response: str):
        self.response = response

    def chat(self, *args, **kwargs):
        return self.response


def test_llm_invalid_doc_is_preserved_when_keyword_fallback_exists():
    router = QueryRouter()
    router._client = FakeClient('{"targets":{"文档名":["章节1"]},"match_reason":{"文档名.章节1":"占位符输出"}}')
    kw_result = {
        "target_medical_doc": ["入院记录"],
        "target_sections": ["现病史"],
        "target_xml_paths": [],
        "targets": {"入院记录": ["现病史"]},
        "confidence": 0.9,
        "judge_reason": "关键词兜底",
        "source": "keyword_match",
    }

    result = router._route_llm("入院时发热的患者", kw_result)

    assert result["target_medical_doc"] == ["入院记录"]
    assert result["targets"] == {"入院记录": ["现病史"]}
    assert result["llm_invalid_targets"][0]["doc"] == "文档名"
    assert "无效文档名" in result["route_warnings"][0]
    assert "llm_invalid_route_fallback" in result["source"]


def test_llm_invalid_doc_is_reported_without_fallback():
    router = QueryRouter()
    router._client = FakeClient('{"targets":{"文档名":["章节1"]},"match_reason":{"文档名.章节1":"占位符输出"}}')

    result = router._route_llm("无法匹配目录的新表达", None)

    assert result["target_medical_doc"] == []
    assert result["target_sections"] == []
    assert result["confidence"] == 0
    assert result["llm_invalid_targets"][0]["doc"] == "文档名"
    assert "LLM路由未给出有效文档" in result["judge_reason"]


def test_llm_doc_label_variant_is_normalized_not_invalidated():
    router = QueryRouter()
    router._client = FakeClient(
        '{"targets":{"《入院记录文档》":["现病史章节"]},"match_reason":{"入院记录.现病史":"名称带后缀"}}'
    )

    result = router._route_llm("入院时发热的患者", None)

    assert result["target_medical_doc"] == ["入院记录"]
    assert result["target_sections"] == ["现病史"]
    assert result["targets"] == {"入院记录": ["现病史"]}
    assert result["llm_invalid_targets"] == []
    assert result["route_repairs"][0]["to"] == "入院记录"
    assert result["route_repairs"][1]["to"] == "现病史"


def test_catalog_service_match_survives_when_document_catalog_has_no_hit(monkeypatch):
    router = QueryRouter()
    monkeypatch.setattr(router, "_extract_concepts", lambda condition: ["烧伤"])
    monkeypatch.setattr(router, "_match_services", lambda concepts, condition="": ["diagnosis-query"])

    result = router._match_catalog("烧伤")

    assert "入院记录" in result["target_medical_doc"]
    assert "主诉" in result["target_sections"]
    assert "现病史" in result["target_sections"]
    assert "初步诊断" in result["targets"]["入院记录"]
    assert result["target_services"] == ["diagnosis-query"]
    assert result["source"] == "concept_service_match"
