"""
Live regression checks for /api/medical/query.

This script calls the same blocking function used by the API. It requires the
configured DB/external services and local Ollama models to be available.

Run:
  python tests/medical_query_regression_cases.py --live
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


CASES = [
    {"condition": "出院时间大于5天并且背痛没有好转的患者"},
    {"condition": "出院时间大于1天并且背痛好转的患者"},
    {"condition": "手术1天后开了维生素的患者"},
    {"condition": "入院120天内注射过葡萄糖的患者"},
    {"condition": "出院后7天开了阿司匹林的患者"},
    {"condition": "手术前2天服用了华法林的患者"},
    {"condition": "术后5天诊断为感染的患者"},
    {"condition": "入院前就有高血压的患者"},
    {"condition": "手术中输过血的患者"},
    {"condition": "术后24小时内做过CT检查的患者"},
    {"condition": "手术3天后开了维生素且诊断为背痛的患者"},
    {"condition": "入院前有糖尿病且住院期间用了胰岛素的患者"},
    {"condition": "术前24小时使用过阿司匹林且术后48小时白细胞计数＞15×10⁹/L的患者"},
    {"condition": "40岁以上有10年以上高血压病史，住院期间白细胞计数指标偏高"},
    {"condition": "术前当天开了阿司匹林"},
    {"condition": "术后当天开了阿司匹林"},
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="run against live DB/external services")
    parser.add_argument("--output", default="", help="write JSON report to this path")
    args = parser.parse_args()
    if not args.live:
        print("Skipped. Use --live to run DB/external-service regression checks.")
        return 0

    from web.app import _run_medical_query

    results = []
    for case in CASES:
        condition = case["condition"]
        res = _run_medical_query(
            condition,
            "0000000120",
            "174",
            "00001_120",
            "00001_174",
            "qwen2.5:3b",
            "qwen2.5:3b",
            "deepseek-r1:1.5b",
        )
        first = res.get("results", [{}])[0]
        per_condition = first.get("per_condition", {}) or {}
        route_conditions = [
            c.get("text", "")
            for c in (res.get("route", {}) or {}).get("conditions", [])
            if isinstance(c, dict)
        ]
        evidence_sources = []
        for cond in per_condition.values():
            if not isinstance(cond, dict):
                continue
            for f in cond.get("files", []) or []:
                if isinstance(f, dict) and f.get("file"):
                    evidence_sources.append(f.get("file"))
        item = {
            "condition": condition,
            "matched": bool(first.get("matched", False)),
            "判断状态": first.get("判断状态", res.get("判断状态", "")),
            "可判定": first.get("可判定", res.get("可判定", False)),
            "置信度": first.get("置信度", res.get("置信度", 0)),
            "置信等级": first.get("置信等级", res.get("置信等级", "")),
            "依据等级": first.get("依据等级", res.get("依据等级", "")),
            "reason": first.get("reason", ""),
            "route_conditions": route_conditions,
            "per_condition": {
                key: {
                    "matched": val.get("matched"),
                    "reason": val.get("reason"),
                    "置信度": val.get("置信度"),
                    "置信等级": val.get("置信等级"),
                    "docs": val.get("docs", []),
                    "sections": val.get("sections", []),
                    "evidence_files": [f.get("file") for f in val.get("files", []) if isinstance(f, dict)],
                }
                for key, val in per_condition.items()
                if isinstance(val, dict)
            },
            "evidence_sources": list(dict.fromkeys(evidence_sources)),
            "has_query_ir": "查询IR" in res,
        }
        results.append(item)

    summary = {
        "total": len(results),
        "可判定": sum(1 for r in results if r.get("可判定")),
        "无法判断": sum(1 for r in results if r.get("判断状态") == "无法判断"),
        "高置信": sum(1 for r in results if r.get("置信等级") == "高"),
        "中置信": sum(1 for r in results if r.get("置信等级") == "中"),
        "低置信": sum(1 for r in results if r.get("置信等级") == "低"),
    }
    report = {"summary": summary, "results": results}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
