import base64

import pytest

from microharness.template_binding.persistence import (
    TemplateBindingCommitError,
    TemplateBindingCommitService,
)


def _encoded_html():
    html = '<div Tag="$Single#UName:入院记录.主诉.内容#TYPE:Simple#SCODE:S001">{入院记录.主诉.内容}</div>'
    return base64.b64encode(html.encode("utf-8")).decode("ascii")


class FixedIdProvider:
    def next_id(self):
        return 123456789


class FakeRepository:
    def __init__(self):
        self.saved = None
        self.template_mappings = []

    def get_html_template(self, template_id, category_id, include_html=False):
        return {
            "template_id": template_id,
            "print_template_category_id": category_id,
            "html_name": "入院记录",
            "html_info": _encoded_html(),
        }

    def get_standard_template(self, template_id):
        return {
            "id": template_id,
            "category_id": "sc1",
            "category_name": "入院记录",
            "category_type": "3",
            "name": "V1",
            "status": 1,
        }

    def list_standard_nodes(self, template_id):
        return [{
            "id": "n1",
            "standard_xml_id": template_id,
            "node_cn": "主诉",
            "node_en": "chiefComplaint",
            "pid": None,
            "pid_new": None,
            "seq_no": 1,
        }]

    def get_existing_mappings(self, html_template_id):
        return {"template_mappings": self.template_mappings, "node_mappings": []}

    def save_reviewed_bindings(self, **kwargs):
        self.saved = kwargs
        return {
            "mapping_id": kwargs["mapping_id"],
            "template_created": True,
            "node_inserted": 1,
            "node_updated": 0,
            "node_unchanged": 0,
            "node_submitted": 1,
        }


def _service(repository=None):
    return TemplateBindingCommitService(repository or FakeRepository(), id_provider=FixedIdProvider())


def test_commit_rebuilds_persisted_selectors_from_server_html():
    repository = FakeRepository()
    result = _service(repository).commit(
        html_template_id="h1",
        html_category_id="hc1",
        standard_template_id="s1",
        node_mappings=[{"standard_node_id": "n1", "html_node_keys": ["html-node-1"]}],
    )

    assert result["saved"] is True
    saved_node = repository.saved["node_mappings"][0]
    assert saved_node["standard_node_id"] == "n1"
    assert saved_node["html_node_id"] == "code:S001"
    assert saved_node["html_node_code"] == "code:S001"
    assert saved_node["mapping_values"] == "{入院记录.主诉.内容}"


def test_legacy_raw_selectors_are_normalized_and_null_is_discarded():
    assert TemplateBindingCommitService._canonical_code_selectors(
        ["S010", "scode:S005", "code:S010", "null", ""]
    ) == ["code:S010", "code:S005"]


def test_section_mapping_does_not_absorb_a_different_html_section():
    standard = {"path_text": "docBody/ 辅助检查 /text"}
    selected = [
        {
            "section": "病历摘要",
            "scope_selectors": ["code:S016_V036"],
            "scope_mapping_value": "病历摘要;辅助检查：[辅助检查]",
        },
        {
            "section": "辅助检查",
            "scope_selectors": ["code:S015"],
            "scope_mapping_value": "辅助检查：",
        },
    ]

    result = TemplateBindingCommitService._section_scope_mapping(
        standard, selected, selected
    )

    assert result == {
        "selectors": ["code:S015"],
        "mapping_values": ["辅助检查："],
    }
def test_commit_stores_only_canonical_code_for_binding_metadata_node():
    repository = FakeRepository()
    repository.get_html_template = lambda template_id, category_id, include_html=False: {
        "template_id": template_id,
        "print_template_category_id": category_id,
        "html_name": "入院记录",
        "html_info": base64.b64encode(
            '<div Tag="$Multiple#TYPE:Simple#TID:6#SCODE:S0032#VTYPE:V|#SITE:R4C4">'
            "{入院记录.主诉.内容}</div>".encode("utf-8")
        ).decode("ascii"),
    }

    _service(repository).commit(
        html_template_id="h1",
        html_category_id="hc1",
        standard_template_id="s1",
        node_mappings=[{"standard_node_id": "n1", "html_node_keys": ["html-node-1"]}],
    )

    saved_node = repository.saved["node_mappings"][0]
    assert saved_node["html_node_id"] == "code:S0032"
    assert saved_node["html_node_code"] == "code:S0032"


def test_commit_uses_deepest_group_selector_for_unannotated_value_node():
    repository = FakeRepository()
    html = (
        '<a name="S001" usage="1" type="start"></a>'
        '<a name="S001_V006" usage="2" type="start"></a>'
        '<span>[admissionDateTime]</span>'
        '<a name="S001_V006" usage="2" type="end"></a>'
        '<a name="S001" usage="1" type="end"></a>'
    )
    repository.get_html_template = lambda template_id, category_id, include_html=False: {
        "template_id": template_id,
        "print_template_category_id": category_id,
        "html_name": "Admission",
        "html_info": base64.b64encode(html.encode("utf-8")).decode("ascii"),
    }

    _service(repository).commit(
        html_template_id="h1",
        html_category_id="hc1",
        standard_template_id="s1",
        node_mappings=[{"standard_node_id": "n1", "html_node_keys": ["html-node-3"]}],
    )

    saved_node = repository.saved["node_mappings"][0]
    assert saved_node["html_node_id"] == "code:S001_V006"
    assert saved_node["html_node_code"] == "code:S001_V006"
    assert saved_node["mapping_values"] == "[admissionDateTime]"


def test_commit_stores_static_text_instead_of_code_as_mapping_value():
    repository = FakeRepository()
    repository.get_html_template = lambda template_id, category_id, include_html=False: {
        "template_id": template_id,
        "print_template_category_id": category_id,
        "html_name": "出院记录",
        "html_info": base64.b64encode(
            '<a name="S005" usage="1" type="start"></a>'
            '<span style="code:S005">诊疗经过：</span>'
            '<a name="S005" usage="1" type="end"></a>'.encode("utf-8")
        ).decode("ascii"),
    }

    _service(repository).commit(
        html_template_id="h1",
        html_category_id="hc1",
        standard_template_id="s1",
        node_mappings=[{"standard_node_id": "n1", "html_node_keys": ["html-node-2"]}],
    )

    assert repository.saved["node_mappings"][0]["mapping_values"] == "诊疗经过："


def test_commit_keeps_merged_mapping_values_aligned_with_source_codes():
    repository = FakeRepository()
    html = """
    <a name="S004" usage="1" type="start"></a>
    <span style="code:S004">术中诊断：</span>
    <a name="S004_V006" usage="2" type="start"></a>
    <span style="code:S004_V006_I0009">[术中诊断]</span>
    <a name="S004_V006" usage="2" type="end"></a>
    <a name="S004" usage="1" type="end"></a>
    """
    repository.get_html_template = lambda template_id, category_id, include_html=False: {
        "template_id": template_id,
        "print_template_category_id": category_id,
        "html_name": "手术记录",
        "html_info": base64.b64encode(html.encode("utf-8")).decode("ascii"),
    }

    _service(repository).commit(
        html_template_id="h1",
        html_category_id="hc1",
        standard_template_id="s1",
        node_mappings=[{"standard_node_id": "n1", "html_node_keys": ["html-node-2"]}],
    )

    saved_node = repository.saved["node_mappings"][0]
    assert saved_node["html_node_id"] == "code:S004;code:S004_V006_I0009"
    assert saved_node["html_node_code"] == "code:S004;code:S004_V006_I0009"
    assert saved_node["mapping_values"] == "术中诊断：;[术中诊断]"


def test_commit_uses_complete_anchor_scope_for_section_text_node():
    repository = FakeRepository()
    html = """
    <a name="S005" usage="1" type="start"></a>
    <span style="code:S005">个人史：</span>
    <span>吸烟史：</span><span style="code:S005_V001">[吸烟时间]</span>
    <span>年，目前已戒烟</span>
    <a name="S005" usage="1" type="end"></a>
    """
    repository.get_html_template = lambda template_id, category_id, include_html=False: {
        "template_id": template_id,
        "print_template_category_id": category_id,
        "html_name": "入院记录",
        "html_info": base64.b64encode(html.encode("utf-8")).decode("ascii"),
    }
    repository.list_standard_nodes = lambda template_id: [
        {
            "id": "body",
            "standard_xml_id": template_id,
            "node_en": "docBody",
            "pid": None,
            "seq_no": 1,
        },
        {
            "id": "history",
            "standard_xml_id": template_id,
            "node_cn": "个人史",
            "pid": "body",
            "seq_no": 2,
        },
        {
            "id": "n1",
            "standard_xml_id": template_id,
            "node_en": "text",
            "pid": "history",
            "seq_no": 3,
        },
    ]

    _service(repository).commit(
        html_template_id="h1",
        html_category_id="hc1",
        standard_template_id="s1",
        node_mappings=[{"standard_node_id": "n1", "html_node_keys": ["html-node-2"]}],
    )

    saved_node = repository.saved["node_mappings"][0]
    assert saved_node["html_node_id"] == "code:S005"
    assert saved_node["html_node_code"] == "code:S005"
    assert saved_node["mapping_values"] == "个人史：;吸烟史：;[吸烟时间];年，目前已戒烟"


def test_commit_keeps_past_history_separate_from_nested_personal_history_scope():
    repository = FakeRepository()
    html = """
    <a name="S004" usage="1" type="start"></a>
    <span style="code:S004">既往史：</span><span>否认慢性病史</span>
    <a name="S004" usage="1" type="end"></a>
    <a name="S005" usage="1" type="start"></a>
    <span style="code:S005">个人史：</span>
    <a name="S005_V024" usage="2" type="start"></a>
    <span style="code:S005_V024_L001">[吸烟时间]</span><span>年</span>
    <a name="S005_V024" usage="2" type="end"></a>
    <a name="S005" usage="1" type="end"></a>
    """
    repository.get_html_template = lambda template_id, category_id, include_html=False: {
        "template_id": template_id,
        "print_template_category_id": category_id,
        "html_name": "入院记录",
        "html_info": base64.b64encode(html.encode("utf-8")).decode("ascii"),
    }
    repository.list_standard_nodes = lambda template_id: [
        {"id": "body", "standard_xml_id": template_id, "node_en": "docBody", "pid": None},
        {"id": "history", "standard_xml_id": template_id, "node_cn": "既往史", "pid": "body"},
        {"id": "n1", "standard_xml_id": template_id, "node_en": "text", "pid": "history"},
    ]

    _service(repository).commit(
        html_template_id="h1",
        html_category_id="hc1",
        standard_template_id="s1",
        node_mappings=[{"standard_node_id": "n1", "html_node_keys": ["html-node-2"]}],
    )

    saved_node = repository.saved["node_mappings"][0]
    assert saved_node["html_node_id"] == "code:S004"
    assert saved_node["mapping_values"] == "既往史：;否认慢性病史"


def test_commit_stores_dynamic_field_param_as_placeholder():
    repository = FakeRepository()
    repository.get_html_template = lambda template_id, category_id, include_html=False: {
        "template_id": template_id,
        "print_template_category_id": category_id,
        "html_name": "出院记录",
        "html_info": base64.b64encode(
            '<field type="dynamic_field" param="当前科室" style="code:Header_V001_L0004"/>'.encode("utf-8")
        ).decode("ascii"),
    }

    _service(repository).commit(
        html_template_id="h1",
        html_category_id="hc1",
        standard_template_id="s1",
        node_mappings=[{"standard_node_id": "n1", "html_node_keys": ["html-node-1"]}],
    )

    assert repository.saved["node_mappings"][0]["mapping_values"] == "[当前科室]"


def test_commit_rejects_unknown_html_node():
    with pytest.raises(TemplateBindingCommitError, match="unknown HTML nodes"):
        _service().commit(
            html_template_id="h1",
            html_category_id="hc1",
            standard_template_id="s1",
            node_mappings=[{"standard_node_id": "n1", "html_node_keys": ["invented"]}],
        )


def test_commit_rejects_unknown_standard_node():
    with pytest.raises(TemplateBindingCommitError, match="unknown or cross-template"):
        _service().commit(
            html_template_id="h1",
            html_category_id="hc1",
            standard_template_id="s1",
            node_mappings=[{"standard_node_id": "other", "html_node_keys": ["html-node-1"]}],
        )


def test_commit_rejects_existing_template_conflict():
    repository = FakeRepository()
    repository.template_mappings = [{"standard_xml_id": "s2"}]

    with pytest.raises(TemplateBindingCommitError, match="different standard template"):
        _service(repository).commit(
            html_template_id="h1",
            html_category_id="hc1",
            standard_template_id="s1",
            node_mappings=[{"standard_node_id": "n1", "html_node_keys": ["html-node-1"]}],
        )


def test_commit_can_replace_mapping_to_inactive_standard_template():
    repository = FakeRepository()
    repository.template_mappings = [
        {"mapping_id": "old", "standard_xml_id": "s2", "standard_template_status": 0}
    ]

    result = _service(repository).commit(
        html_template_id="h1",
        html_category_id="hc1",
        standard_template_id="s1",
        node_mappings=[{"standard_node_id": "n1", "html_node_keys": ["html-node-1"]}],
    )

    assert result["saved"] is True
