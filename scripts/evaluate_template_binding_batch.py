"""Run a read-only batch evaluation for HTML/clinical-template binding.

This script deliberately calls ``TemplateBindingAnalysisService.analyze`` only.
It never calls the repository commit method and does not modify DMP mapping
tables.  The report keeps summaries rather than embedding complete HTML or
node payloads so it is safe to inspect after a large batch.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from microharness.template_binding.repository import TemplateBindingRepository
from microharness.template_binding.service import (
    TemplateBindingAnalysisError,
    TemplateBindingAnalysisService,
)


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _summarize(result: dict[str, Any], source: dict[str, Any], elapsed_ms: int) -> dict[str, Any]:
    node_match = result.get("node_match") or {}
    standard = result.get("standard") or {}
    template_match = result.get("template_match") or {}
    validation = result.get("validation") or {}
    html = result.get("html") or {}
    return {
        "template_id": str(source.get("template_id") or ""),
        "html_category_id": str(source.get("print_template_category_id") or ""),
        "html_category_name": str(source.get("category_name") or ""),
        "html_name": str(source.get("html_name") or ""),
        "html_info_length": int(source.get("html_info_length") or 0),
        "html_node_count": int(html.get("node_count") or 0),
        "status": result.get("status"),
        "selected_standard_template_id": template_match.get("selected_template_id"),
        "selected_standard_template_name": (standard.get("template") or {}).get("name"),
        "standard_node_count": int(standard.get("node_count") or 0),
        "standard_bindable_count": int(standard.get("bindable_count") or 0),
        "standard_container_count": int(standard.get("container_count") or 0),
        "standard_root_count": int(standard.get("root_count") or 0),
        "mapping_count": int(node_match.get("mapping_count") or 0),
        "auto_count": int(node_match.get("auto_count") or 0),
        "review_count": int(node_match.get("review_count") or 0),
        "unmatched_count": int(node_match.get("unmatched_count") or 0),
        "unmatched_reason_counts": dict(
            node_match.get("diagnostics", {}).get("unmatched_reason_counts") or {}
        ),
        "llm": node_match.get("llm") or {},
        "node_diagnostics": node_match.get("diagnostics") or {},
        "performance": result.get("performance") or {},
        "template_match_status": template_match.get("status"),
        "validation_status": validation.get("status"),
        "warning_count": len(result.get("warnings") or []),
        "warnings": list(result.get("warnings") or [])[:20],
        "elapsed_ms": int(result.get("elapsed_ms") or elapsed_ms),
    }


def _run_one(
    service: TemplateBindingAnalysisService,
    source: dict[str, Any],
    *,
    use_llm: bool,
    template_match_model: str | None,
    node_match_model: str | None,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = service.analyze(
            html_template_id=str(source["template_id"]),
            html_category_id=str(source["print_template_category_id"]),
            use_llm=use_llm,
            template_match_model=template_match_model,
            node_match_model=node_match_model,
        )
        if result.get("read_only") is not True:
            raise RuntimeError("analysis result did not declare read_only=true")
        return {
            "ok": True,
            "summary": _summarize(
                result,
                source,
                round((time.perf_counter() - started) * 1000),
            ),
        }
    except Exception as exc:  # Keep the batch running after one bad template.
        return {
            "ok": False,
            "template_id": str(source.get("template_id") or ""),
            "html_category_id": str(source.get("print_template_category_id") or ""),
            "html_name": str(source.get("html_name") or ""),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "error_type": exc.__class__.__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(limit=8),
        }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [row["summary"] for row in rows if row.get("ok")]
    failed = [row for row in rows if not row.get("ok")]
    reason_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    template_status_counts: Counter[str] = Counter()
    llm_attempted = 0
    llm_selected = 0
    llm_api_calls = 0
    semantic_batches = 0
    mappings = 0
    html_nodes = 0
    standard_nodes = 0
    bindable_nodes = 0
    zero_node_selected: list[dict[str, Any]] = []
    elapsed_values: list[int] = []
    cache_stats: dict[str, int] = {}
    stage_totals: Counter[str] = Counter()
    stage_sample_counts: Counter[str] = Counter()
    for row in successful:
        status_counts[str(row.get("status") or "UNKNOWN")] += 1
        template_status_counts[str(row.get("template_match_status") or "UNKNOWN")] += 1
        reason_counts.update(row.get("unmatched_reason_counts") or {})
        llm = row.get("llm") or {}
        llm_attempted += int(llm.get("attempted") or 0)
        llm_selected += int(llm.get("selected") or 0)
        diagnostics = row.get("node_diagnostics") or {}
        llm_api_calls += int(diagnostics.get("llm_api_call_count") or 0)
        semantic_batches += int(diagnostics.get("semantic_batch_count") or 0)
        mappings += int(row.get("mapping_count") or 0)
        html_nodes += int(row.get("html_node_count") or 0)
        standard_nodes += int(row.get("standard_node_count") or 0)
        bindable_nodes += int(row.get("standard_bindable_count") or 0)
        elapsed_values.append(int(row.get("elapsed_ms") or 0))
        for key, value in (row.get("performance") or {}).get("stages_ms", {}).items():
            stage_totals[key] += int(value or 0)
            stage_sample_counts[key] += 1
        if row.get("selected_standard_template_id") and int(row.get("standard_node_count") or 0) == 0:
            zero_node_selected.append(
                {
                    "template_id": row.get("template_id"),
                    "html_category_id": row.get("html_category_id"),
                    "html_name": row.get("html_name"),
                    "selected_standard_template_id": row.get("selected_standard_template_id"),
                }
            )
        for key, value in ((row.get("performance") or {}).get("template_catalog_cache") or {}).items():
            # Service cache counters are cumulative within the batch service;
            # report the largest snapshot rather than summing every row.
            cache_stats[key] = max(cache_stats.get(key, 0), int(value or 0))
    slowest = sorted(
        (
            {
                "template_id": row.get("template_id"),
                "html_category_id": row.get("html_category_id"),
                "html_name": row.get("html_name"),
                "elapsed_ms": int(row.get("elapsed_ms") or 0),
            }
            for row in successful
        ),
        key=lambda item: item["elapsed_ms"],
        reverse=True,
    )[:10]
    return {
        "requested": len(rows),
        "succeeded": len(successful),
        "failed": len(failed),
        "status_counts": dict(status_counts),
        "template_match_status_counts": dict(template_status_counts),
        "unmatched_reason_counts": dict(reason_counts),
        "html_node_total": html_nodes,
        "standard_node_total": standard_nodes,
        "standard_bindable_node_total": bindable_nodes,
        "mapping_total": mappings,
        "mapping_rate_over_standard_nodes": round(mappings / standard_nodes, 4)
        if standard_nodes
        else 0.0,
        "llm_attempted_total": llm_attempted,
        "llm_selected_total": llm_selected,
        "llm_api_call_total": llm_api_calls,
        "semantic_batch_total": semantic_batches,
        "elapsed_ms_total": sum(
            int(row.get("summary", {}).get("elapsed_ms") or row.get("elapsed_ms") or 0)
            for row in rows
        ),
        "elapsed_ms_average_success": round(
            sum(elapsed_values) / len(elapsed_values)
        )
        if successful
        else 0,
        "elapsed_ms_p95_success": (
            sorted(elapsed_values)[min(len(elapsed_values) - 1, max(0, round(len(elapsed_values) * 0.95) - 1))]
            if elapsed_values
            else 0
        ),
        "zero_node_selected_count": len(zero_node_selected),
        "zero_node_selected": zero_node_selected[:20],
        "slowest_templates": slowest,
        "cache_stats": dict(cache_stats),
        "stage_totals_ms": dict(stage_totals),
        "stage_average_ms": {
            key: round(value / stage_sample_counts[key])
            for key, value in stage_totals.items()
            if stage_sample_counts[key]
        }
        if successful
        else {},
        "stage_sample_counts": dict(stage_sample_counts),
        "failed_templates": [
            {
                "template_id": row.get("template_id"),
                "html_category_id": row.get("html_category_id"),
                "html_name": row.get("html_name"),
                "error_type": row.get("error_type"),
                "error": row.get("error"),
            }
            for row in failed
        ],
    }


def _row_key(row: dict[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("template_id") or ""),
        str(row.get("html_category_id") or row.get("print_template_category_id") or ""),
    )


def _ordered_rows(
    sources: list[dict[str, Any]], rows_by_key: dict[tuple[str, str], dict[str, Any]]
) -> list[dict[str, Any]]:
    return [rows_by_key[key] for source in sources if (key := _row_key(source)) in rows_by_key]


def _build_report(
    *,
    status: str,
    started_at: str,
    sources: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    config: dict[str, Any],
    error: str | None = None,
) -> dict[str, Any]:
    aggregate = _aggregate(rows)
    report = {
        "schema_version": 2,
        "read_only": True,
        "database_operations": ["SELECT only"],
        "commit_called": False,
        "status": status,
        "started_at": started_at,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "config": config,
        "progress": {
            "requested": len(sources),
            "completed": len(rows),
            "succeeded": aggregate["succeeded"],
            "failed": aggregate["failed"],
            "remaining": max(0, len(sources) - len(rows)),
        },
        "aggregate": aggregate,
        "rows": rows,
    }
    if error:
        report["error"] = error
    return report


def _write_report(output: Path, report: dict[str, Any]) -> None:
    """Write a checkpoint atomically so an interrupted batch is inspectable."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    os.replace(temporary, output)


def _load_resume_rows(output: Path, config: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    if not output.exists():
        return {}
    try:
        previous = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if previous.get("schema_version") != 2 or previous.get("read_only") is not True:
        return {}
    previous_config = previous.get("config") or {}
    for key in ("page", "use_llm", "template_model", "node_model"):
        if previous_config.get(key) != config.get(key):
            return {}
    resumed: dict[tuple[str, str], dict[str, Any]] = {}
    for row in previous.get("rows") or []:
        if row.get("ok") is True:
            resumed[_row_key(row.get("summary") or row)] = row
    return resumed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument("--template-model", default="qwen2.5:3b")
    parser.add_argument("--node-model", default="qwen2.5:3b")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=1,
        help="checkpoint after this many completed templates (default: 1)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reuse successful rows from an existing schema-v2 report",
    )
    args = parser.parse_args()
    if args.count <= 0:
        raise SystemExit("--count must be positive")
    if args.checkpoint_every <= 0:
        raise SystemExit("--checkpoint-every must be positive")

    repository = TemplateBindingRepository()
    started_at = datetime.now().isoformat(timespec="seconds")
    status = "running"
    exit_code = 0
    fatal_error: str | None = None
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        page = repository.list_html_templates(
            page=max(1, args.page),
            page_size=min(200, max(args.count, args.page_size)),
        )
        sources = (page.get("items") or [])[: args.count]
        if len(sources) < args.count:
            print(f"warning: requested {args.count}, got {len(sources)}")
        config = {
            "count": len(sources),
            "requested_count": args.count,
            "page": args.page,
            "page_size": args.page_size,
            "use_llm": args.use_llm,
            "template_model": args.template_model if args.use_llm else None,
            "node_model": args.node_model if args.use_llm else None,
        }
        if args.resume:
            rows_by_key.update(_load_resume_rows(args.output, config))
            if rows_by_key:
                print(f"resume successful_rows={len(rows_by_key)}", flush=True)
        print(
            f"read-only batch start count={len(sources)} use_llm={args.use_llm} "
            f"models={args.template_model}/{args.node_model}",
            flush=True,
        )
        _write_report(
            args.output,
            _build_report(
                status=status,
                started_at=started_at,
                sources=sources,
                rows=_ordered_rows(sources, rows_by_key),
                config=config,
            ),
        )
        service = TemplateBindingAnalysisService(repository)
        for index, source in enumerate(sources, start=1):
            key = _row_key(source)
            if key in rows_by_key:
                summary = rows_by_key[key].get("summary") or {}
                print(
                    f"[{index}/{len(sources)}] {source.get('template_id')} "
                    f"{source.get('html_name') or ''} resume=skip "
                    f"status={summary.get('status')}",
                    flush=True,
                )
                continue
            print(
                f"[{index}/{len(sources)}] {source.get('template_id')} "
                f"{source.get('html_name') or ''}",
                flush=True,
            )
            row = _run_one(
                service,
                source,
                use_llm=args.use_llm,
                template_match_model=args.template_model if args.use_llm else None,
                node_match_model=args.node_model if args.use_llm else None,
            )
            rows_by_key[key] = row
            if row.get("ok"):
                summary = row["summary"]
                print(
                    f"  status={summary['status']} template={summary['selected_standard_template_id']} "
                    f"html_nodes={summary['html_node_count']} standard_nodes={summary['standard_node_count']} "
                    f"mappings={summary['mapping_count']} unmatched={summary['unmatched_count']} "
                    f"elapsed_ms={summary['elapsed_ms']}",
                    flush=True,
                )
                cache = (summary.get("performance") or {}).get("template_catalog_cache") or {}
                if cache:
                    print(
                        f"  cache=catalog_queries:{cache.get('standard_template_catalog_queries', 0)} "
                        f"catalog_hits:{cache.get('standard_template_catalog_cache_hits', 0)} "
                        f"node_queries:{cache.get('standard_node_queries', 0)} "
                        f"node_hits:{cache.get('standard_node_cache_hits', 0)}",
                        flush=True,
                    )
            else:
                print(
                    f"  ERROR {row.get('error_type')}: {row.get('error')}",
                    flush=True,
                )
            completed = len(rows_by_key)
            if completed % args.checkpoint_every == 0:
                _write_report(
                    args.output,
                    _build_report(
                        status=status,
                        started_at=started_at,
                        sources=sources,
                        rows=_ordered_rows(sources, rows_by_key),
                        config=config,
                    ),
                )
        status = "completed"
    except KeyboardInterrupt:
        status = "interrupted"
        exit_code = 130
        print("batch interrupted; checkpoint retained", flush=True)
    except Exception as exc:
        status = "failed"
        exit_code = 1
        fatal_error = f"{exc.__class__.__name__}: {exc}"
        traceback.print_exc()
    finally:
        try:
            # Always leave an inspectable report, including on Ctrl+C or a
            # database-level failure before the normal completion path.
            if "sources" in locals() and "config" in locals():
                _write_report(
                    args.output,
                    _build_report(
                        status=status,
                        started_at=started_at,
                        sources=sources,
                        rows=_ordered_rows(sources, rows_by_key),
                        config=config,
                        error=fatal_error,
                    ),
                )
        finally:
            repository.close()
    report = json.loads(args.output.read_text(encoding="utf-8"))
    print(json.dumps(report["aggregate"], ensure_ascii=False, indent=2), flush=True)
    print(f"status={report['status']} report={args.output}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
