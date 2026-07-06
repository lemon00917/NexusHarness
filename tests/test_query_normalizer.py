from microharness.agent.query_normalizer import (
    _safe_accept_llm,
    normalize_query_text,
)


def _normalized(text: str) -> str:
    return normalize_query_text(text)[0]


def test_llm_normalization_cannot_flip_numeric_comparison_direction():
    original = "术前48小时内白细胞＞1.5×10⁹/L"
    deterministic = _normalized(original)
    candidate = _normalized("术前48小时内白细胞<1.5x10⁹/L")

    assert deterministic == "术前48小时内白细胞>1.5x10⁹/L"
    assert not _safe_accept_llm(original, deterministic, candidate, 0.9)


def test_llm_normalization_must_preserve_scientific_threshold():
    original = "术前48小时内白细胞＞1.5×10⁹/L"
    deterministic = _normalized(original)
    candidate = _normalized("术前48小时内白细胞>1.5")

    assert not _safe_accept_llm(original, deterministic, candidate, 0.9)


def test_llm_normalization_accepts_equivalent_comparison_symbols():
    original = "住院天数大于1天"
    deterministic = _normalized(original)
    candidate = _normalized("住院天数>1天")

    assert _safe_accept_llm(original, deterministic, candidate, 0.9)

