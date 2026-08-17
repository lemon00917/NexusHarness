from microharness.template_binding.similarity import normalize_text, text_similarity


def test_normalize_text_removes_version_suffix_and_punctuation():
    assert normalize_text("入院记录 - V1.0") == "入院记录"


def test_normalize_text_preserves_unmarked_field_ordinals():
    assert normalize_text("\u51fa\u9662\u8bca\u65ad1") != normalize_text("\u51fa\u9662\u8bca\u65ad2")


def test_text_similarity_recognizes_containment():
    assert text_similarity("\u51fa\u9662\u8bb0\u5f55", "\u51fa\u9662\u8bb0\u5f55\u6a21\u677f") >= 0.7
