from pathlib import Path


_TEMPLATE = Path(__file__).parents[1] / "web" / "templates" / "medical_filter.html"


def _function_source(text: str, name: str, next_name: str) -> str:
    start = text.index(f"function {name}(")
    end = text.index(f"function {next_name}(", start)
    return text[start:end]


def test_candidate_rendering_uses_machine_metadata_not_skill_display_names():
    text = _TEMPLATE.read_text(encoding="utf-8")
    source = _function_source(text, "candidateRecordType", "renderLabCandidateRecords")

    assert "record_type" in source
    assert "entity_type" in source
    assert "domain" in source
    assert "evidence_types" in source
    assert "用药医嘱查询" not in source
    assert "诊断查询" not in source
    assert "检验指标查询" not in source


def test_evidence_role_uses_canonical_source_role_not_skill_display_names():
    text = _TEMPLATE.read_text(encoding="utf-8")
    source = _function_source(text, "evidenceRole", "roleClass")

    assert "source_role" in source
    assert "evidence_role" in source
    assert "PRIMARY" in source
    assert "TIME_ANCHOR" in source
    assert "检验指标查询" not in source
    assert "就诊信息查询" not in source


def test_candidate_tables_render_business_record_identity_for_supported_domains():
    text = _TEMPLATE.read_text(encoding="utf-8")

    assert "function candidateRecordIdentity(" in text
    assert "function candidateRecordIdentityLabel(" in text
    assert "function renderLabCandidateRecords(" in text
    assert "function renderDrugCandidateRecords(" in text
    assert "function renderDiagnosisCandidateRecords(" in text
    assert "function renderEncounterCandidateRecords(" in text
    assert "'检验报告号'" in text
    assert "'医嘱号'" in text
    assert "'诊断ID'" in text
    assert "'就诊号'" in text
    assert "row['记录ID']" in text
    assert "row['记录序号']" in text


def test_generic_candidate_table_hides_record_identity_metadata_columns():
    text = _TEMPLATE.read_text(encoding="utf-8")
    source = _function_source(text, "renderGenericCandidateRecords", "candidateValue")

    assert "记录序号" in source
    assert "记录标识名称" in source
    assert "记录标识字段" in source
