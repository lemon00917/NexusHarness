from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_database_config_page_contains_independent_dmp_controls():
    html = (ROOT / "web" / "templates" / "database_config.html").read_text(encoding="utf-8")

    assert "DMP 数据源" in html
    assert "/api/template-binding/database/config" in html
    assert "/api/template-binding/database/test" in html
    assert "saveDmpDatabase" in html


def test_workbench_uses_composite_key_and_reviewed_commit_flow():
    html = (ROOT / "web" / "templates" / "template_binding_db.html").read_text(encoding="utf-8")

    assert "print_template_category_id" in html
    assert "category_id=${query(categoryId)}" in html
    assert "Stage 4 · 自动推荐 · 人工确认后保存" in html
    assert "id=\"analyzeButton\"" in html
    assert "/api/template-binding/analyze" in html
    assert "/api/template-binding/commit" in html
    assert "id=\"saveBindingButton\"" in html
    assert "expected_update_time" in html
    assert "templateCandidateRows" in html
    assert "nodeRecommendationRows" in html
    assert "use_llm" in html
    assert "id=\"templateMatchModel\"" in html
    assert "id=\"nodeMatchModel\"" in html
    assert "/api/ollama/models" in html
    assert "renderModelSelect" in html
    assert "modelLoadStatus" in html
    assert "template_match_model" in html
    assert "node_match_model" in html
    assert "/api/template-binding/standard/templates" in html
    assert "tb-node-group" in html
    assert "toggleNodeGroups('htmlNodes', true)" in html
    assert "renderGroupedNodes" in html
    assert "height: clamp(360px, 44vh, 480px)" in html
    assert "node.anchor_name, node.tag ?" in html
    assert "selector, node.tag" not in html
    assert "template_mapping_count" in html
    assert "mapped_html_count" in html
    assert "当前 HTML 已绑定" in html
    assert "node_mapping_count" in html
    assert "doc_template_mapping" not in html
