from microharness.medical.semantic_rules import split_compound_clauses


def test_split_mixed_and_connector_and_punctuation():
    parts, connector = split_compound_clauses("40岁以上并且背痛，住院期间血红蛋白指标异常")

    assert connector == "and"
    assert parts == ["40岁以上", "背痛", "住院期间血红蛋白指标异常"]
