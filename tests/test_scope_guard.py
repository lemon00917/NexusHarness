import pytest

from microharness.medical.scope_guard import (
    build_scope_rejection_response,
    evaluate_medical_filter_scope,
)


@pytest.mark.parametrize(
    ("condition", "code"),
    [
        ("\u5e2e\u6211\u67e5\u4e00\u4e0b\u8fd9\u4e2a\u60a3\u8005", "ambiguous_request"),
        ("\u4eca\u5929\u4e0a\u6d77\u5929\u6c14\u600e\u4e48\u6837", "unrelated_request"),
        ("\u5e2e\u6211\u5199Python\u4ee3\u7801", "unrelated_request"),
        ("\u6839\u636eCT\u539f\u56fe\u7b5b\u9009\u80ba\u7ed3\u8282\u60a3\u8005", "unsupported_data_source"),
        ("\u6839\u636e\u57fa\u56e0\u6d4b\u5e8f\u7ed3\u679c\u7b5b\u9009\u60a3\u8005", "unsupported_data_source"),
        ("\u9ad8\u8840\u538b\u5e94\u8be5\u600e\u4e48\u6cbb\u7597", "non_filter_medical_request"),
    ],
)
def test_scope_guard_rejects_clear_out_of_scope_requests(condition, code):
    decision = evaluate_medical_filter_scope(condition)

    assert not decision.allowed
    assert decision.code == code
    assert decision.reason
    assert decision.signals


@pytest.mark.parametrize(
    "condition",
    [
        "\u80cc\u75db",
        "\u70e7\u4f24",
        "\u53d1\u751f\u836f\u7269\u526f\u4f5c\u7528\u7684\u60a3\u8005",
        "\u672f\u524d24\u5c0f\u65f6\u4f7f\u7528\u8fc7\u963f\u53f8\u5339\u6797\u4e14\u672f\u524d48\u5c0f\u65f6\u5185\u4e2d\u6027\u7c92\u7ec6\u80de\u6570\u504f\u4f4e\u7684\u60a3\u8005",
        "\u672f\u524d24\u5c0f\u65f6\u4f7f\u7528\u8fc7\u963f\u53f8\u5339\u6797\u4e14\u672f\u524d48\u5c0f\u65f6\u5185\u4e2d\u6027\u7c92\u7ec6\u80de\u6570\uff1e1.5\u00d710\u2079/L\u7684\u60a3\u8005",
        "40\u5c81\u4ee5\u4e0a\u5e76\u4e14\u80cc\u75db\uff0c\u4f4f\u9662\u671f\u95f4\u8840\u7ea2\u86cb\u767d\u6307\u6807\u5f02\u5e38",
        "\u4f4f\u9662\u5929\u6570\u5c0f\u4e8e5\u5929\u5e76\u4e14\u70e7\u4f24\u7684\u60a3\u8005",
        "\u4f4f\u9662\u671f\u95f4\u8840\u7ea2\u86cb\u767d\u6307\u6807\u504f\u9ad8\u7684\u60a3\u8005",
    ],
)
def test_scope_guard_allows_medical_filter_conditions(condition):
    decision = evaluate_medical_filter_scope(condition)

    assert decision.allowed
    assert decision.code == "allowed"


def test_scope_rejection_response_preserves_frontend_contract():
    condition = "\u5e2e\u6211\u67e5\u4e00\u4e0b\u8fd9\u4e2a\u60a3\u8005"
    decision = evaluate_medical_filter_scope(condition)

    response = build_scope_rejection_response(
        condition,
        decision,
        original_condition=condition,
        query_ir={"type": "simple"},
    )

    assert response["matched_count"] == 0
    assert response["\u5224\u65ad\u72b6\u6001"] == "\u65e0\u6cd5\u6267\u884c"
    assert response["\u53ef\u5224\u5b9a"] is False
    assert response["results"]
    assert response["results"][0]["reason"] == decision.reason
    assert response["results"][0]["\u7528\u6237\u89e3\u91ca"] == decision.reason
    assert response["scope_guard"]["code"] == "ambiguous_request"
    assert response["\u67e5\u8be2IR"] == {"type": "simple"}


def test_medical_query_rejects_before_router_execution(monkeypatch):
    from microharness.agent import query_normalizer, query_understanding
    from microharness.medical import query_router
    from web.app import _run_medical_query

    class Normalization:
        normalized = "\u5e2e\u6211\u5199Python\u4ee3\u7801"
        source = "test"
        confidence = 1.0

        def to_dict(self):
            return {"source": self.source, "confidence": self.confidence}

    class RouterMustNotRun:
        def __init__(self, *args, **kwargs):
            raise AssertionError("query router executed before scope rejection")

    monkeypatch.setattr(query_normalizer, "normalize_query", lambda *args, **kwargs: Normalization())
    monkeypatch.setattr(
        query_understanding,
        "understand_query",
        lambda condition, model=None: {
            "type": "simple",
            "connector": None,
            "conditions": [{"text": condition, "keyword": condition}],
        },
    )
    monkeypatch.setattr(query_router, "QueryRouter", RouterMustNotRun)

    result = _run_medical_query(
        "\u5e2e\u6211\u5199Python\u4ee3\u7801",
        "",
        "",
        "",
        "",
        "test-model",
        "test-model",
        None,
    )

    assert result["\u5224\u65ad\u72b6\u6001"] == "\u65e0\u6cd5\u6267\u884c"
    assert result["scope_guard"]["code"] == "unrelated_request"
