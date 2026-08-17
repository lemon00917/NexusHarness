import base64

import pytest

from microharness.template_binding.html_parser import HtmlTemplateDecodeError, parse_html_info


def _encoded(html: str) -> str:
    value = base64.b64encode(html.encode("utf-8")).decode("ascii")
    return "\r\n".join(value[index:index + 24] for index in range(0, len(value), 24))


def test_parser_accepts_mime_base64_and_extracts_supported_nodes():
    html = """
    <h2>入院信息</h2>
    <input style="width:10px; code:S001_V001" value="[姓名]">
    <a name="S002" usage="section" type="start"></a>
    <span>[就诊号]</span>
    <a name="S002" usage="section" type="end"></a>
    """

    result = parse_html_info(_encoded(html))

    assert result["node_count"] >= 4
    assert any("code:S001_V001" in node["selectors"] for node in result["nodes"])
    assert any(node["placeholder"] == "[姓名]" for node in result["nodes"])
    assert [node["pair_status"] for node in result["nodes"] if node["anchor_name"] == "S002"] == [
        "paired",
        "paired",
    ]


def test_parser_reports_unpaired_anchor():
    result = parse_html_info(_encoded('<a name="S001" type="start"></a>'))

    assert result["nodes"][0]["pair_status"] == "missing_end"
    assert result["warnings"]


def test_parser_extracts_generic_tag_metadata_without_duplicate_nodes():
    uname = "2012新住院首页.健康卡号：.值"
    html = f'''
    <td Location="4-5"
        Tag="$Single#UName:{uname}#TYPE:Simple#TID:281#SCODE:S0321#SITE:R4C5"
        title="{{{uname}}}">
        {{{uname}}}
    </td>
    '''

    result = parse_html_info(_encoded(html))

    assert result["node_count"] == 1
    node = result["nodes"][0]
    assert node["display_text"] == "健康卡号"
    assert node["placeholder"] == f"{{{uname}}}"
    assert node["mapping_value"] == f"{{{uname}}}"
    assert node["section"] == "2012新住院首页"
    assert node["usage"] == "Simple"
    assert {"S0321", "code:S0321", "site:R4C5", "binding:Single"}.issubset(node["selectors"])


def test_parser_ignores_script_style_and_numeric_square_placeholders():
    html = """
    <script>const first = values[0]; const fake = '[姓名]';</script>
    <style>.item[data-index="[1]"] { color: red; }</style>
    <div>[0]</div>
    """

    result = parse_html_info(_encoded(html))

    assert result["node_count"] == 0


def test_parser_extracts_unannotated_brace_placeholder():
    result = parse_html_info(_encoded("<section>{入院诊断.名称}</section>"))

    assert result["node_count"] == 1
    node = result["nodes"][0]
    assert node["display_text"] == "名称"
    assert node["section"] == "入院诊断"
    assert node["placeholder"] == "{入院诊断.名称}"


def test_parser_rejects_invalid_base64_and_non_utf8():
    with pytest.raises(HtmlTemplateDecodeError):
        parse_html_info("not-base64")
    with pytest.raises(HtmlTemplateDecodeError):
        parse_html_info(base64.b64encode(b"\xff\xfe"))


def test_parser_builds_generic_anchor_context_and_merges_code_placeholders():
    html = """
    <a name="S007" usage="1" type="start"></a>
    <span style="code:S007">出院诊断：</span>
    <a name="S007_V008" usage="2" type="start"></a>
    <span style="code:S007_V008_I0011">[出院诊断1]</span>
    <span style="code:S007_V008_I0012">[新建I单元]</span>
    <a name="S007_V008" usage="2" type="end"></a>
    <a name="S007" usage="1" type="end"></a>
    """

    result = parse_html_info(_encoded(html))
    value_nodes = [node for node in result["nodes"] if not node["structural"]]

    assert len(value_nodes) == 3
    root = next(node for node in value_nodes if "code:S007" in node["selectors"])
    first_value = next(
        node for node in value_nodes if "code:S007_V008_I0011" in node["selectors"]
    )
    assert root["display_text"] == "出院诊断"
    assert root["mapping_value"].startswith("出院诊断：;")
    assert "[出院诊断1]" in root["mapping_value"]
    assert first_value["placeholder"] == "[出院诊断1]"
    assert first_value["section"] == "出院诊断"
    assert first_value["group_labels"] == ["出院诊断"]
    assert first_value["anchor_path"] == ["S007", "S007_V008"]


def test_parser_attaches_preceding_static_field_label_to_value_node():
    html = """
    <a name="S001" usage="1" type="start"></a>
    <span style="code:S001">患者基本信息</span>
    <span>姓名：</span><span style="code:S001_V001">[姓名]</span>
    <a name="S001" usage="1" type="end"></a>
    """

    result = parse_html_info(_encoded(html))
    value = next(node for node in result["nodes"] if node["placeholder"] == "[姓名]")

    assert value["local_label"] == "姓名"
    assert value["section"] == "患者基本信息"


def test_parser_uses_static_anchor_text_instead_of_code_as_mapping_value():
    html = """
    <a name="S003" usage="1" type="start"></a>
    <span style="code:S003">入院情况：</span>
    <a name="S003" usage="1" type="end"></a>
    """

    result = parse_html_info(_encoded(html))
    root = next(node for node in result["nodes"] if not node["structural"])

    assert root["display_text"] == "入院情况"
    assert root["mapping_value"] == "入院情况："


def test_parser_converts_dynamic_field_param_to_square_placeholder():
    html = '<field type="dynamic_field" param="病案号" style="code:Header_V001_L0003"/>'

    result = parse_html_info(_encoded(html))
    node = result["nodes"][0]

    assert node["placeholder"] == "[病案号]"
    assert node["mapping_value"] == "[病案号]"


def test_parser_prefers_dynamic_placeholder_when_anchor_root_is_dynamic_field():
    html = """
    <a name="S001" usage="1" type="start"></a>
    <span style="code:S001">病案号：</span>
    <field type="dynamic_field" param="病案号" style="code:S001"/>
    <a name="S001" usage="1" type="end"></a>
    """

    result = parse_html_info(_encoded(html))
    root = next(
        node
        for node in result["nodes"]
        if not node["structural"] and node["tag"] == "field"
    )

    assert root["mapping_value"] == "[病案号]"


def test_parser_preserves_complete_anchor_scope_text():
    html = """
    <a name="S005" usage="1" type="start"></a>
    <span style="code:S005">个人史：</span>
    <span>吸烟史：</span><span style="code:S005_V001">[吸烟时间]</span>
    <span>年，目前已戒烟</span>
    <a name="S005" usage="1" type="end"></a>
    """

    result = parse_html_info(_encoded(html))
    root = next(node for node in result["nodes"] if "code:S005" in node["selectors"])

    assert root["scope_selectors"] == ["code:S005"]
    assert root["scope_mapping_value"] == "个人史：;吸烟史：;[吸烟时间];年，目前已戒烟"


def test_parser_combines_top_level_anchor_code_family_into_one_scope():
    html = """
    <a name="S011019" usage="1" type="start"></a>
    <span style="code:S011019">体格检查</span>
    <a name="S011019" usage="1" type="end"></a>
    <a name="S011_S011001" usage="1" type="start"></a>
    <span style="code:S011_S011001">生命体征：</span><span>[体温]</span>
    <a name="S011_S011001" usage="1" type="end"></a>
    """

    result = parse_html_info(_encoded(html))
    root = next(node for node in result["nodes"] if "code:S011019" in node["selectors"])

    assert root["scope_selectors"] == ["code:S011019", "code:S011_S011001"]
    assert root["scope_mapping_value"] == "体格检查;生命体征：;[体温]"
