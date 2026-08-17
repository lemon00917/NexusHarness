from scripts.evaluate_template_binding_batch import _aggregate


def _summary(*, elapsed_ms, stages=None):
    return {
        'template_id': f'h-{elapsed_ms}',
        'status': 'REVIEW_REQUIRED',
        'template_match_status': 'REVIEW_REQUIRED',
        'llm': {'attempted': 0, 'selected': 0},
        'node_diagnostics': {'llm_api_call_count': 0, 'semantic_batch_count': 0},
        'mapping_count': 0,
        'html_node_count': 1,
        'standard_node_count': 1,
        'standard_bindable_count': 1,
        'unmatched_reason_counts': {},
        'elapsed_ms': elapsed_ms,
        'performance': {'stages_ms': stages or {}},
    }


def test_stage_average_uses_rows_that_contain_that_stage():
    aggregate = _aggregate([
        {'ok': True, 'summary': _summary(elapsed_ms=100, stages={'node_match_ms': 80})},
        {'ok': True, 'summary': _summary(elapsed_ms=200, stages={})},
    ])

    assert aggregate['stage_totals_ms'] == {'node_match_ms': 80}
    assert aggregate['stage_sample_counts'] == {'node_match_ms': 1}
    assert aggregate['stage_average_ms'] == {'node_match_ms': 80}
