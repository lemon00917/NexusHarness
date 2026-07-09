from microharness.medical.condition_summary import summarize_condition_structure


def test_condition_summary_does_not_show_time_window_as_lab_threshold():
    summary = summarize_condition_structure("术前48小时内中性粒细胞数偏低")

    assert summary["限定"] == "术前48小时内"
    assert summary["判断"] == "偏低"


def test_condition_summary_keeps_explicit_lab_numeric_threshold_after_time_window():
    summary = summarize_condition_structure("术前48小时内中性粒细胞数>1.5x10^9/L")

    assert summary["限定"] == "术前48小时内"
    assert summary["判断"] == ">1.5e+09"
