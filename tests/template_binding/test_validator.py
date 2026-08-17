from microharness.template_binding.validator import BindingRecommendationValidator


def test_validator_rejects_unknown_html_node_and_cross_template_node():
    result = BindingRecommendationValidator().validate(
        selected_template={"id": "s1", "category_type": "3"},
        standard_nodes=[{"id": "n1", "template_id": "s2"}],
        html_nodes=[{"node_key": "h1"}],
        node_match={"mappings": [{"standard_node_id": "n1", "html_node_keys": ["missing"]}]},
    )

    assert result["status"] == "CONFLICT"
    assert result["valid"] is False
    assert result["mapping_count"] == 0


def test_validator_allows_overlap_only_for_authoritative_existing_mappings():
    base = {
        "selected_template": {"id": "s1", "category_type": "3"},
        "standard_nodes": [
            {"id": "past", "template_id": "s1"},
            {"id": "personal", "template_id": "s1"},
        ],
        "html_nodes": [{"node_key": "shared"}],
    }
    mappings = [
        {
            "standard_node_id": "past",
            "html_node_keys": ["shared"],
            "source": "existing",
            "status": "EXISTING",
        },
        {
            "standard_node_id": "personal",
            "html_node_keys": ["shared"],
            "source": "existing",
            "status": "EXISTING",
        },
    ]

    result = BindingRecommendationValidator().validate(
        **base,
        node_match={"mappings": mappings},
    )

    assert result["status"] == "VALID"
    assert result["mapping_count"] == 2


def test_validator_still_rejects_overlap_for_new_recommendations():
    result = BindingRecommendationValidator().validate(
        selected_template={"id": "s1", "category_type": "3"},
        standard_nodes=[
            {"id": "past", "template_id": "s1"},
            {"id": "personal", "template_id": "s1"},
        ],
        html_nodes=[{"node_key": "shared"}],
        node_match={
            "mappings": [
                {"standard_node_id": "past", "html_node_keys": ["shared"], "source": "rule"},
                {
                    "standard_node_id": "personal",
                    "html_node_keys": ["shared"],
                    "source": "rule+llm",
                },
            ]
        },
    )

    assert result["status"] == "CONFLICT"
    assert result["mapping_count"] == 1
