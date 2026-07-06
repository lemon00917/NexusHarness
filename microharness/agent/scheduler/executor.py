"""
Execution Engine
================
Executes a structured plan step by step, respecting dependencies.

The engine is deterministic — it does NOT make decisions. It follows
the LLM-generated plan, calling registered action handlers in order.
"""

from __future__ import annotations
import time
from typing import Any, Dict, List, Optional

from microharness.agent.scheduler.tools import (
    ExecutionContext, get_handler, list_actions,
)


class ExecutionEngine:
    """Deterministic plan executor.

    Usage:
        ctx = ExecutionContext(condition=..., register_no=..., ...)
        engine = ExecutionEngine(ctx)
        result = engine.execute(plan["plan"])
    """

    def __init__(self, context: ExecutionContext):
        self.context = context
        self.state: Dict[str, Any] = {}  # step output_var → result

    def execute(self, plan: List[dict]) -> dict:
        """Execute a plan, returning a result dict compatible with the API.

        Args:
            plan: List of step dicts, each with step_id, action, params, output_var, depends_on

        Returns:
            Dict with {matched, reason, results, total_ms} matching _run_medical_query format
        """
        t0 = time.time()

        # Topological sort by depends_on
        ordered = self._topological_sort(plan)

        for step in ordered:
            sid = step.get("step_id", "?")
            action_name = step.get("action", "")
            desc = step.get("description", action_name)

            print(f"  [Scheduler] Step{sid}: {desc} ({action_name})", flush=True)

            handler = get_handler(action_name)
            if not handler:
                print(f"  [Scheduler] Step{sid}: 未知动作'{action_name}' → 跳过", flush=True)
                continue

            # Resolve $var references in params
            params = self._resolve_params(step.get("params", {}))
            params = {**params, "_step_desc": desc}

            try:
                result = handler(params, self.state, self.context)
                if result.get("ok"):
                    output_var = step.get("output_var", "")
                    if output_var:
                        self.state[output_var] = result
                    print(f"  [Scheduler] Step{sid}: ✓ {desc[:50]} → {output_var}", flush=True)
                else:
                    error = result.get("error", "未知错误")
                    print(f"  [Scheduler] Step{sid}: ✗ {error}", flush=True)
                    # On step failure, fall back to legacy pipeline
                    return self._fallback_to_legacy(error)
            except Exception as e:
                print(f"  [Scheduler] Step{sid}: ✗ 异常({e})", flush=True)
                return self._fallback_to_legacy(str(e))

        total_ms = int((time.time() - t0) * 1000)

        # ── Build rich result matching existing pipeline format ──
        final_var = ordered[-1].get("output_var", "") if ordered else ""
        final = self.state.get(final_var, {})
        matched = final.get("matched", False)
        reason = final.get("reason", "执行完成")

        # ── per_condition: inherit from legacy_pipeline + time constraint ──
        per_condition = {}
        _med_docs = []
        _med_sections = []
        for step in ordered:
            action = step.get("action", "?")
            out_var = step.get("output_var", "")
            step_result = self.state.get(out_var, {})
            if action == "legacy_pipeline" and step_result.get("per_condition"):
                for k, v in step_result["per_condition"].items():
                    per_condition[k] = v
                    if v.get("docs"):
                        _med_docs.extend(v["docs"])
                    if v.get("sections"):
                        _med_sections.extend(v["sections"])

        # ── Build time-constraint per_condition (same format as existing pipeline) ──
        _plan_params = {s.get("action",""): s.get("params",{}) for s in ordered}
        _qp = _plan_params.get("query_db", {})
        _tp = _plan_params.get("temporal_filter", {})
        _db = self.state.get("db_results", {})
        _tf = self.state.get("filtered_data", {})
        _anchor = self.state.get("anchor_date", {})

        if _tp:
            # Determine label from plan context (not hardcoded)
            _anchor_doc = "、".join(_qp.get("documents", []))
            _anchor_section = "、".join(_qp.get("sections", []))
            _rel = _tp.get("relation", "")
            _val = _tp.get("value", "")
            _val_display = int(_val) if isinstance(_val, float) and _val.is_integer() else _val
            _unit = _tp.get("unit", "hours")
            _unit_cn = {"hours": "小时", "days": "天", "weeks": "周", "minutes": "分钟"}.get(_unit, _unit)
            _rel_cn = {"within": "内", "before": "前", "after": "后"}.get(_rel, _rel)

            _tf_count = len(_tf.get("data", [])) if _tf.get("ok") else 0
            _anchor_date = ""
            if _anchor.get("dates"):
                _anchor_date = _anchor["dates"][0].get("date", "")

            # ── Evidence: minimal, just date + matched count ──
            _evidence = {}
            if _anchor_date:
                _evidence["anchor_date"] = _anchor_date
            _api = self.state.get("api_results", {})
            _api_data = _api.get("data", []) if _api.get("ok") else []
            _tf_count = len(_tf.get("data", [])) if _tf.get("ok") else 0
            _time_filtered = self.state.get("time_filtered_data", {})
            _time_data = _time_filtered.get("data", []) if isinstance(_time_filtered, dict) else []
            # Extract matched record prefixes (shared between evidence and files)
            _matched_prefixes = set()
            for item in (_tf.get("data", []) or []):
                if isinstance(item, dict):
                    field = item.get("html_field", "")
                    m = __import__('re').match(r'(\[[^]]+\])', field)
                    if m:
                        _matched_prefixes.add(m.group(1))
            _candidate_prefixes = set()
            for item in (_tf.get("candidate_data", []) or []):
                if isinstance(item, dict):
                    field = item.get("html_field", "")
                    m = __import__('re').match(r'(\[[^]]+\])', field)
                    if m:
                        _candidate_prefixes.add(m.group(1))
            _time_prefixes = set()
            for item in (_time_data or []):
                if isinstance(item, dict):
                    field = item.get("html_field", "")
                    m = __import__('re').match(r'(\[[^]]+\])', field)
                    if m:
                        _time_prefixes.add(m.group(1))
            if _tf_count > 0 and _matched_prefixes:
                _key_fields = []
                for api_item in (_api_data or []):
                    if isinstance(api_item, dict):
                        for b in api_item.get("bindings", []):
                            field = b.get("html_field", "")
                            eng_field = b.get("eng_field", "")
                            m = __import__('re').match(r'(\[[^]]+\])', field)
                            if m and m.group(1) in _matched_prefixes:
                                if "orderName" in field or "diagnoseName" in field or "名称" in field:
                                    val = str(b.get("value", ""))[:120]
                                    _key_fields.append(f"{field}: {val}")
                if _key_fields:
                    _evidence["matched_records"] = "\\n".join(_key_fields[:10])
                _evidence["matched_count"] = f"{_tf_count}条/{len(_matched_prefixes)}组"
            elif _candidate_prefixes:
                _candidate_lines = []
                for prefix in list(_candidate_prefixes)[:10]:
                    parts = []
                    diff_h = None
                    for item in (_tf.get("candidate_data", []) or []):
                        if isinstance(item, dict) and item.get("html_field", "").startswith(prefix):
                            diff_h = item.get("_diff_hours")
                            break
                    for api_item in (_api_data or []):
                        if not isinstance(api_item, dict):
                            continue
                        for b in api_item.get("bindings", []):
                            field = b.get("html_field", "")
                            if not field.startswith(prefix):
                                continue
                            val = str(b.get("value", ""))[:120]
                            if ("名称" in field or "日期" in field or "时间" in field) and val:
                                parts.append(f"{field}: {val}")
                    if parts:
                        suffix = f"（与参考时间差{diff_h:.1f}小时）" if isinstance(diff_h, (int, float)) else ""
                        _candidate_lines.append("; ".join(parts[:4]) + suffix)
                if _candidate_lines:
                    _evidence["candidate_records"] = "\\n".join(_candidate_lines)
                _evidence["candidate_count"] = _tf.get("candidate_count", len(_candidate_lines))

            # ── Files: DB source + service source + matched ──
            _tf_files = []
            # DB file
            for item in (_db.get("data", []) or []):
                if isinstance(item, dict) and item.get("file"):
                    _tf_files.append({"file": item["file"], "matched": bool(_anchor_date),
                                     "reason": f"{_anchor_section}: {_anchor_date}" if _anchor_date else f"{_anchor_section}未找到",
                                     "fields": "", "cot_response": ""})
            # Service file — only include matched record data, not all 84 records
            _concept_miss = _tf_count == 0 and "但匹配字段中未找到" in str(_tf.get("reason", ""))
            _display_prefixes = _matched_prefixes or (_time_prefixes if _concept_miss else _candidate_prefixes)
            if _display_prefixes:
                _match_fields = []
                for api_item in (_api_data or []):
                    if isinstance(api_item, dict):
                        for b in api_item.get("bindings", []):
                            field = b.get("html_field", "")
                            m = __import__('re').match(r'(\[[^]]+\])', field)
                            if m and m.group(1) in _display_prefixes:
                                lbl = field
                                val = str(b.get("value", ""))[:200]
                                if lbl and val.strip():
                                    _match_fields.append(f"{lbl}: {val}")
                _api_file = (_api_data[0] or {}).get("file", "服务") if _api_data else "服务"
                _file_reason = f"时间窗口内{_tf_count}条匹配" if _tf_count > 0 else f"时间窗口内无匹配；找到{len(_display_prefixes)}组时间不符合的候选记录"
                if _concept_miss:
                    _file_reason = f"时间窗口内有{len(_display_prefixes)}组候选记录，但目标字段未命中"
                _tf_files.append({"file": _api_file, "matched": _tf_count > 0,
                                 "reason": _file_reason,
                                 "fields": "\\n".join(_match_fields[:30])[:1500], "cot_response": ""})
            # Matched records
            for item in (_tf.get("data", [])[:10] or []):
                if isinstance(item, dict):
                    _dh = item.get("_diff_hours")
                    _reason = f"与参考时间差{_dh:.0f}h" if isinstance(_dh, (int, float)) else "时间窗口内匹配"
                    _field_label = item.get("html_field", "记录")
                    _tf_files.append({"file": _field_label, "matched": True, "reason": _reason,
                                     "fields": "", "cot_response": ""})

            # Label: prefer plan-supplied, otherwise construct from params
            _ref_field = (_qp.get("sections", [""]) or [""])[0]
            if _tp.get("label"):
                _tf_label = _tp["label"]
            elif _val >= 365 and _unit == "days":
                _tf_label = f"{_ref_field}{_rel_cn}{_val_display}{_unit_cn}"
            else:
                _tf_label = f"{_ref_field}{_rel_cn}{_val_display}{_unit_cn}"

            # Compute elapsed time
            _cs = _plan_params.get("call_service", {})
            _svc_name = _cs.get("service", "external")
            _tf_elapsed = sum(
                s.get("elapsed_ms", 0) for s in [
                    self.state.get("db_results", {}),
                    self.state.get("api_results", {}),
                    self.state.get("anchor_date", {}),
                    self.state.get("filtered_data", {}),
                ] if isinstance(s, dict)
            )

            per_condition[_tf_label] = {
                "condition": _tf_label,
                "matched": _tf_count > 0,
                "reason": _tf.get("reason", ""),
                "files": _tf_files,
                "docs": _qp.get("documents", []),
                "sections": _qp.get("sections", []),
                "evidence": _evidence,
                "elapsed_ms": _tf_elapsed,
            }

        # Collect all_files from steps that produced data
        try:
            from microharness.medical.evidence import assess_condition_confidence
            for _cond in per_condition.values():
                if isinstance(_cond, dict):
                    _cond["置信评估"] = assess_condition_confidence(_cond)
                    _cond.update(_cond["置信评估"])
        except Exception:
            pass

        # Collect all_files from steps that produced data
        all_files = []
        for key, val in self.state.items():
            if isinstance(val, dict):
                # legacy_pipeline returns all_files list
                af = val.get("all_files", [])
                if isinstance(af, list):
                    for f in af:
                        if f not in all_files:
                            all_files.append(f)
                # Other steps may have data with file names
                data = val.get("data", [])
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("file"):
                            fname = item["file"]
                            if fname not in all_files:
                                all_files.append(fname)

        # ── Build route matching existing pipeline format ──
        route_info = {
            "complexity": "COMPLEX",
            "target_medical_doc": list(set(_med_docs)),
            "target_sections": list(set(_med_sections)),
            "source": "scheduler",
            "sub_queries": list(per_condition.keys()),
            "plan_steps": [{"step_id": s.get("step_id"), "action": s.get("action")}
                          for s in ordered],
            "total_steps": len(ordered),
        }

        from microharness.medical.evidence import assess_patient_confidence
        patient_confidence = assess_patient_confidence(matched, reason, per_condition)
        status = patient_confidence["判断状态"]
        conclusive = patient_confidence["可判定"]
        return {
            "condition": self.context.condition,
            "register_no": self.context.register_no,
            "route": route_info,
            "results": [{
                "register_no": self.context.register_no,
                "matched": matched,
                "reason": reason,
                "判断状态": status,
                "可判定": conclusive,
                "置信度": patient_confidence["置信度"],
                "置信等级": patient_confidence["置信等级"],
                "依据等级": patient_confidence["依据等级"],
                "per_condition": per_condition,
                "all_files": all_files,
            }],
            "matched_count": 1 if matched else 0,
            "判断状态": status,
            "可判定": conclusive,
            "置信度": patient_confidence["置信度"],
            "置信等级": patient_confidence["置信等级"],
            "依据等级": patient_confidence["依据等级"],
            "total_ms": total_ms,
        }

    def _fallback_to_legacy(self, error: str) -> dict:
        """Fall back to legacy pipeline on any step failure."""
        print(f"  [Scheduler] 步骤失败 → 回退 legacy pipeline ({error})", flush=True)
        try:
            from microharness.agent.scheduler.tools import handle_legacy_pipeline
            result = handle_legacy_pipeline({}, self.state, self.context)
            if result.get("ok"):
                matched = bool(result.get("matched", False))
                reason = f"[调度回退] {result.get('reason','')}"
                from microharness.medical.evidence import assess_patient_confidence
                patient_confidence = assess_patient_confidence(matched, reason, result.get("per_condition", {}))
                status = patient_confidence["判断状态"]
                conclusive = patient_confidence["可判定"]
                return {
                    "condition": self.context.condition,
                    "register_no": self.context.register_no,
                    "results": [{
                        "register_no": self.context.register_no,
                        "matched": matched,
                        "reason": reason,
                        "判断状态": status,
                        "可判定": conclusive,
                        "置信度": patient_confidence["置信度"],
                        "置信等级": patient_confidence["置信等级"],
                        "依据等级": patient_confidence["依据等级"],
                        "per_condition": {},
                    }],
                    "matched_count": 1 if matched else 0,
                    "判断状态": status,
                    "可判定": conclusive,
                    "置信度": patient_confidence["置信度"],
                    "置信等级": patient_confidence["置信等级"],
                    "依据等级": patient_confidence["依据等级"],
                    "total_ms": 0,
                }
        except Exception as e:
            print(f"  [Scheduler] legacy回退也失败: {e}", flush=True)

        return {
            "condition": self.context.condition,
            "register_no": self.context.register_no,
            "results": [{
                "register_no": self.context.register_no,
                "matched": False,
                "reason": f"调度层回退失败: {error}",
                "判断状态": "无法判断",
                "可判定": False,
                "per_condition": {},
            }],
            "matched_count": 0,
            "判断状态": "无法判断",
            "可判定": False,
            "total_ms": 0,
        }

    @staticmethod
    def _judgment_status(matched: bool, reason: str, per_condition: dict = None) -> tuple[str, bool]:
        texts = [str(reason or "")]
        for item in (per_condition or {}).values():
            if isinstance(item, dict):
                texts.append(str(item.get("reason", "")))
                for f in item.get("files", []) or []:
                    if isinstance(f, dict):
                        texts.append(str(f.get("reason", "")))
        joined = "；".join(texts)
        unknown_markers = (
            "无法判断", "接口失败", "请求超时", "外部数据源调用失败",
            "DB不可用", "数据库不可用", "未找到日期字段", "文件中无相关日期/数值字段",
        )
        if any(marker in joined for marker in unknown_markers):
            return "无法判断", False
        return ("符合" if matched else "不符合"), True

    def _resolve_params(self, params: dict) -> dict:
        """Replace $var references with actual state values."""
        resolved = {}
        for key, val in params.items():
            if isinstance(val, str) and val.startswith("$"):
                var_name = val[1:]
                if var_name in self.state:
                    resolved[key] = self.state[var_name].get("data", self.state[var_name])
                else:
                    resolved[key] = val  # keep as-is if var not found
            else:
                resolved[key] = val
        return resolved

    def _topological_sort(self, plan: List[dict]) -> List[dict]:
        """Topological sort by depends_on. Steps with no dependencies come first."""
        if not plan:
            return []

        # Build dependency graph
        remaining = list(plan)
        sorted_steps = []

        while remaining:
            # Find steps whose dependencies are all satisfied
            ready = []
            not_ready = []
            for step in remaining:
                deps = step.get("depends_on", [])
                if not isinstance(deps, list):
                    deps = [deps] if isinstance(deps, int) else []
                sorted_ids = {s["step_id"] for s in sorted_steps}
                if all(d in sorted_ids for d in deps):
                    ready.append(step)
                else:
                    not_ready.append(step)

            if not ready:
                # No progress → circular dependency or invalid plan
                # Execute remaining in order
                sorted_steps.extend(not_ready)
                break

            sorted_steps.extend(ready)
            remaining = not_ready

        return sorted_steps
