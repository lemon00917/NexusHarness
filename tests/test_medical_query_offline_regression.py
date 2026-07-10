from microharness.medical.query_ir import build_query_ir
from microharness.medical.query_structure import repair_analysis_structure


def _build(text: str):
    seed = {
        "type": "simple",
        "connector": None,
        "conditions": [{"text": text, "keyword": text}],
    }
    analysis = repair_analysis_structure(seed, text)
    return analysis, build_query_ir(analysis, text)


def test_drug_and_qualitative_lab_condition_regression():
    text = "\u672f\u524d24\u5c0f\u65f6\u4f7f\u7528\u8fc7\u963f\u53f8\u5339\u6797\u4e14\u672f\u524d48\u5c0f\u65f6\u5185\u4e2d\u6027\u7c92\u7ec6\u80de\u6570\u504f\u4f4e\u7684\u60a3\u8005"
    analysis, query_ir = _build(text)

    assert analysis["type"] == "compound"
    assert analysis["connector"] == "and"
    assert len(query_ir.conditions) == 2
    assert query_ir.conditions[0].target_services == ["drug-interaction"]
    assert query_ir.conditions[1].target_services == ["lab-results"]
    assert query_ir.conditions[1].numeric_comparison is None


def test_drug_and_numeric_lab_condition_regression():
    text = "\u672f\u524d24\u5c0f\u65f6\u4f7f\u7528\u8fc7\u963f\u53f8\u5339\u6797\u4e14\u672f\u524d48\u5c0f\u65f6\u5185\u4e2d\u6027\u7c92\u7ec6\u80de\u6570\uff1e1.5\u00d710\u2079/L\u7684\u60a3\u8005"
    analysis, query_ir = _build(text)

    assert analysis["type"] == "compound"
    assert analysis["connector"] == "and"
    comparison = query_ir.conditions[1].numeric_comparison
    assert query_ir.conditions[1].target_services == ["lab-results"]
    assert comparison["operator"] == ">"
    assert comparison["threshold"] == 1_500_000_000.0


def test_age_symptom_and_lab_condition_regression():
    text = "40\u5c81\u4ee5\u4e0a\u5e76\u4e14\u80cc\u75db\uff0c\u4f4f\u9662\u671f\u95f4\u8840\u7ea2\u86cb\u767d\u6307\u6807\u5f02\u5e38"
    analysis, query_ir = _build(text)

    assert analysis["type"] == "compound"
    assert analysis["connector"] == "and"
    assert [item.text for item in query_ir.conditions] == [
        "40\u5c81\u4ee5\u4e0a",
        "\u80cc\u75db",
        "\u4f4f\u9662\u671f\u95f4\u8840\u7ea2\u86cb\u767d\u6307\u6807\u5f02\u5e38",
    ]
    assert "\u5165\u9662\u8bb0\u5f55" in query_ir.conditions[0].target_docs
    assert query_ir.conditions[2].target_services == ["lab-results", "encounter-info"]
    assert query_ir.conditions[2].numeric_comparison is None


def test_length_of_stay_and_burn_condition_regression():
    text = "\u4f4f\u9662\u5929\u6570\u5c0f\u4e8e5\u5929\u5e76\u4e14\u70e7\u4f24\u7684\u60a3\u8005"
    analysis, query_ir = _build(text)

    assert analysis["type"] == "compound"
    assert analysis["connector"] == "and"
    assert [item.text for item in query_ir.conditions] == [
        "\u4f4f\u9662\u5929\u6570\u5c0f\u4e8e5\u5929",
        "\u70e7\u4f24",
    ]
    duration = query_ir.conditions[0]
    assert duration.target_services == ["encounter-info"]
    assert duration.numeric_comparison["threshold"] == 5.0
    assert duration.numeric_comparison["unit"] == "\u5929"


def test_single_qualitative_lab_condition_regression():
    text = "\u4f4f\u9662\u671f\u95f4\u8840\u7ea2\u86cb\u767d\u6307\u6807\u504f\u9ad8\u7684\u60a3\u8005"
    analysis, query_ir = _build(text)

    assert analysis["type"] == "simple"
    assert analysis["connector"] is None
    assert len(query_ir.conditions) == 1
    condition = query_ir.conditions[0]
    assert condition.target_services == ["lab-results", "encounter-info"]
    assert condition.predicate == "high"
    assert condition.numeric_comparison is None
