import pytest

from microharness.template_binding.standard_tree import StandardNodeTreeError, build_standard_tree


def test_tree_uses_pid_new_then_falls_back_to_pid():
    rows = [
        {"id": "root", "standard_xml_id": "10", "node_cn": "文档", "seq_no": 1},
        {"id": "a", "standard_xml_id": "10", "pid_new": "root", "node_cn": "章节", "seq_no": 2},
        {"id": "b", "standard_xml_id": "10", "pid": "a", "node_cn": "字段", "seq_no": 3},
    ]

    tree = build_standard_tree(rows, "10")
    by_id = {node["id"]: node for node in tree["flat_nodes"]}

    assert tree["root_count"] == 1
    assert by_id["b"]["parent_id"] == "a"
    assert by_id["b"]["path_text"] == "文档/章节/字段"


def test_tree_rejects_orphan_cross_template_and_cycle():
    with pytest.raises(StandardNodeTreeError, match="orphan"):
        build_standard_tree([{"id": "a", "standard_xml_id": "10", "pid": "missing"}], "10")
    with pytest.raises(StandardNodeTreeError, match="belongs"):
        build_standard_tree([{"id": "a", "standard_xml_id": "11"}], "10")
    with pytest.raises(StandardNodeTreeError, match="cycle"):
        build_standard_tree(
            [
                {"id": "a", "standard_xml_id": "10", "pid": "b"},
                {"id": "b", "standard_xml_id": "10", "pid": "a"},
            ],
            "10",
        )


def test_tree_accepts_common_root_sentinel_values():
    tree = build_standard_tree(
        [
            {"id": "a", "standard_xml_id": "10", "pid": "0", "node_cn": "root-a"},
            {"id": "b", "standard_xml_id": "10", "pid_new": "-1", "node_cn": "root-b"},
        ],
        "10",
    )

    assert tree["root_count"] == 2
    assert all(node["parent_id"] is None for node in tree["flat_nodes"])


def test_tree_keeps_zero_parent_when_zero_is_a_real_node():
    tree = build_standard_tree(
        [
            {"id": "0", "standard_xml_id": "10", "node_cn": "root"},
            {"id": "a", "standard_xml_id": "10", "pid": "0", "node_cn": "child"},
        ],
        "10",
    )

    by_id = {node["id"]: node for node in tree["flat_nodes"]}
    assert by_id["a"]["parent_id"] == "0"
    assert by_id["a"]["path_text"] == "root/child"
