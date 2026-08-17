from microharness.medical import catalog_source
from microharness.medical import query_router
from microharness.medical.query_router import format_catalog_source_log


def test_merge_external_metadata_retains_local_semantics():
    local = {
        "手术记录": {
            "used_for": ["术前术后查询"],
            "sections": [
                {
                    "name": "手术日期",
                    "anchor_field": True,
                    "time_role": "range",
                }
            ],
        }
    }
    external = {
        "手术记录": {
            "purpose": "外部用途",
            "tableName": "emr_surgical_record",
            "sections": [
                {
                    "name": "手术日期",
                    "purpose": "外部章节用途",
                    "path": "ClinicalDocument/docBody/surgeryDate/text",
                }
            ],
        }
    }

    merged = catalog_source.merge_external_with_local(external, local)
    document = merged["手术记录"]
    section = document["sections"][0]

    assert document["purpose"] == "外部用途"
    assert document["used_for"] == ["术前术后查询"]
    assert section["path"].endswith("surgeryDate/text")
    assert section["anchor_field"] is True
    assert section["time_role"] == "range"


def test_merge_matches_date_time_and_decorated_section_names():
    local = {
        "出院记录": {
            "sections": [{"name": "入院日期", "time_role": "point"}],
        },
        "手术记录": {
            "sections": [{"name": "科室", "info_type": "科室"}],
        },
    }
    external = {
        "出院记录": {"sections": [{"name": "入院日期时间"}]},
        "手术记录": {"sections": [{"name": "科室：手术时所在科室"}]},
    }

    merged = catalog_source.merge_external_with_local(external, local)

    admission = merged["出院记录"]["sections"][0]
    department = merged["手术记录"]["sections"][0]
    assert admission["time_role"] == "point"
    assert admission["aliases"] == ["入院日期"]
    assert department["info_type"] == "科室"
    assert department["aliases"] == ["科室"]


def test_external_failure_falls_back_to_local(monkeypatch):
    local = {"入院记录": {"purpose": "本地配置", "sections": []}}
    monkeypatch.setattr(
        catalog_source,
        "load_source_config",
        lambda: {"source": "external", "external_url": "http://invalid"},
    )
    monkeypatch.setattr(
        catalog_source,
        "load_local_catalog",
        lambda fallback_catalog=None: local,
    )
    monkeypatch.setattr(
        catalog_source,
        "fetch_external_catalog",
        lambda url: (_ for _ in ()).throw(RuntimeError("连接失败")),
    )

    catalog, status = catalog_source.load_effective_catalog()

    assert catalog == local
    assert status["configured_source"] == "external"
    assert status["effective_source"] == "local"
    assert status["fallback"] is True
    assert status["error"] == "连接失败"


def test_catalog_source_log_shows_configured_and_effective_source():
    message = format_catalog_source_log(
        "[病历筛选元数据]",
        {
            "configured_source": "external",
            "effective_source": "local",
            "external_url": "http://metadata.example/api",
            "fallback": True,
            "error": "连接失败",
            "document_count": 6,
        },
    )

    assert "配置来源=外部接口" in message
    assert "实际来源=本地配置" in message
    assert "是否回退=是" in message
    assert "外部URL=http://metadata.example/api" in message
    assert "原因=连接失败" in message


def test_realtime_reload_fetches_each_time_and_returns_isolated_snapshot(monkeypatch):
    calls = []

    def fake_load_effective_catalog(fallback_catalog=None):
        calls.append(len(calls) + 1)
        sequence = calls[-1]
        return (
            {
                "入院记录": {
                    "purpose": f"第{sequence}次加载",
                    "sections": [],
                }
            },
            {
                "configured_source": "external",
                "effective_source": "external",
                "external_url": "http://metadata.example/api",
                "fallback": False,
                "error": "",
                "document_count": 1,
            },
        )

    monkeypatch.setattr(catalog_source, "load_effective_catalog", fake_load_effective_catalog)
    monkeypatch.setattr(query_router, "DOCUMENT_CATALOG", query_router.DOCUMENT_CATALOG)
    monkeypatch.setattr(query_router, "SECTION_PURPOSE_LOOKUP", query_router.SECTION_PURPOSE_LOOKUP)
    monkeypatch.setattr(query_router, "CATALOG_SOURCE_STATUS", query_router.CATALOG_SOURCE_STATUS)

    first_catalog, _ = query_router.reload_document_catalog_snapshot()
    second_catalog, _ = query_router.reload_document_catalog_snapshot()
    first_catalog["入院记录"]["purpose"] = "已修改快照"

    assert calls == [1, 2]
    assert second_catalog["入院记录"]["purpose"] == "第2次加载"
    assert query_router.DOCUMENT_CATALOG["入院记录"]["purpose"] == "第2次加载"
