import base64

from microharness.template_binding.service import TemplateBindingAnalysisService


def _encoded_html(label="主诉"):
    html = f'<h2>入院记录</h2><span value="[{label}]"></span>'
    return base64.b64encode(html.encode("utf-8")).decode("ascii")


class FakeRepository:
    def __init__(self, variant_html=None):
        self.html_info = _encoded_html()
        self.variant_html = variant_html or self.html_info

    def get_html_template(self, template_id, category_id, include_html=False):
        return {
            "template_id": template_id,
            "print_template_category_id": category_id,
            "category_name": "入院记录",
            "html_name": "入院记录",
            "html_info": self.html_info,
        }

    def list_html_template_variants(self, template_id):
        return [
            {
                "template_id": template_id,
                "print_template_category_id": "hc1",
                "category_name": "入院记录",
                "html_name": "入院记录",
                "html_info": self.html_info,
            },
            {
                "template_id": template_id,
                "print_template_category_id": "hc2",
                "category_name": "入院记录",
                "html_name": "入院记录",
                "html_info": self.variant_html,
            },
        ]

    def list_standard_templates(self, page=1, page_size=200):
        return {
            "items": [{"id": "s1", "category_id": "sc1", "category_name": "入院记录", "name": "V1.0", "status": 1}],
            "total": 1,
        }

    def get_existing_mappings(self, html_template_id):
        return {"template_mappings": [], "node_mappings": [], "node_mapping_count": 0}

    def get_standard_template(self, template_id):
        return {
            "id": template_id,
            "category_id": "sc1",
            "category_name": "入院记录",
            "category_type": "3",
            "name": "V1.0",
            "status": 1,
        }

    def list_standard_nodes(self, template_id):
        return [
            {
                "id": "n1",
                "standard_xml_id": template_id,
                "node_cn": "主诉",
                "node_en": "chiefComplaint",
                "pid": None,
                "pid_new": None,
                "seq_no": 1,
            }
        ]


def test_service_builds_read_only_template_and_node_recommendations():
    result = TemplateBindingAnalysisService(FakeRepository()).analyze(
        html_template_id="h1",
        html_category_id="hc1",
        use_llm=False,
    )

    assert result["read_only"] is True
    assert result["status"] == "COMPLETED"
    assert result["template_match"]["selected_template_id"] == "s1"
    assert result["node_match"]["mapping_count"] == 1
    assert result["validation"]["valid"] is True
    assert result["existing_mapping_policy"] == "reference"
    assert result["llm_models"]["enabled"] is False


def test_service_excludes_inactive_standard_templates_from_candidates():
    class MixedStatusRepository(FakeRepository):
        def list_standard_templates(self, page=1, page_size=200):
            if page > 1:
                return {"items": [], "total": 2}
            return {
                "items": [
                    {
                        "id": "s1",
                        "category_id": "sc1",
                        "category_name": "入院记录",
                        "name": "V1.0",
                        "status": 1,
                    },
                    {
                        "id": "inactive",
                        "category_id": "sc1",
                        "category_name": "入院记录",
                        "name": "V0.9",
                        "status": 0,
                    },
                ],
                "total": 2,
            }

    result = TemplateBindingAnalysisService(MixedStatusRepository()).analyze(
        html_template_id="h1",
        html_category_id="hc1",
        use_llm=False,
    )

    assert [item["template_id"] for item in result["template_match"]["candidates"]] == ["s1"]


def test_service_unknown_existing_mapping_policy_falls_back_to_reference():
    result = TemplateBindingAnalysisService(FakeRepository()).analyze(
        html_template_id="h1",
        html_category_id="hc1",
        use_llm=False,
        existing_mapping_policy="future-policy",
    )

    assert result["existing_mapping_policy"] == "reference"
    assert any("降级为 reference" in warning for warning in result["warnings"])


def test_service_reports_requested_llm_models_without_calling_model_when_disabled():
    result = TemplateBindingAnalysisService(FakeRepository()).analyze(
        html_template_id="h1",
        html_category_id="hc1",
        use_llm=False,
        template_match_model="model-a",
        node_match_model="model-b",
    )

    assert result["llm_models"] == {
        "enabled": False,
        "template_match_model": None,
        "node_match_model": None,
    }


def test_service_uses_separate_template_and_node_match_models():
    created_models = []

    class FakeChat:
        def chat(self, messages, temperature=0.1):
            raise AssertionError("single-candidate fixture should not call chat")

    def fake_llm_factory(model):
        created_models.append(model)
        return FakeChat()

    result = TemplateBindingAnalysisService(
        FakeRepository(), llm_factory=fake_llm_factory
    ).analyze(
        html_template_id="h1",
        html_category_id="hc1",
        use_llm=True,
        template_match_model="model-a",
        node_match_model="model-b",
    )

    assert result["llm_models"] == {
        "enabled": True,
        "template_match_model": "model-a",
        "node_match_model": "model-b",
    }
    assert created_models == ["model-a", "model-b"]


def test_service_blocks_same_html_id_with_different_content():
    result = TemplateBindingAnalysisService(
        FakeRepository(variant_html=_encoded_html("现病史"))
    ).analyze(html_template_id="h1", html_category_id="hc1", use_llm=False)

    assert result["status"] == "CONFLICT"
    assert result["identity_check"]["content_hash_count"] == 2
    assert result["node_match"] is None
