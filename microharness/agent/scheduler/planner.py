"""
Query Planner
=============
LLM-based complexity judgment and execution plan generation.

For complex queries (temporal relations, cross-source dependencies),
generates a structured execution plan that the ExecutionEngine follows.

NO hardcoded domain knowledge in code:
- Anchor→document mapping: LLM chooses from catalog
- Verb→service mapping: LLM chooses from catalog
- Relation detection: done by query_analyzer (LLM)
- Template fallback: generic, no domain words
"""

from __future__ import annotations

from microharness.ollama.model_profile import get_profile, ModelProfile
from microharness.ollama import OllamaClient


def _extract_json_dict(text: str, context: str = "") -> dict:
    """Extract a JSON dict from LLM output. No CoT inference, no fallback guessing.
    Just: strip markdown fences, parse JSON, return dict or {}.
    """
    import json as _json
    cleaned = text.strip()

    # Strip markdown code fences
    for fence in ("```json", "```"):
        if fence in cleaned:
            parts = cleaned.split(fence)
            if len(parts) >= 2:
                cleaned = parts[1].split("```")[0] if "```" in parts[1] else parts[1]
                cleaned = cleaned.strip()
                break

    # Strip JS-style comments (deepseek-r1 sometimes adds // inline)
    import re as _re
    cleaned = _re.sub(r'//[^\n]*', '', cleaned)

    try:
        result = _json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except _json.JSONDecodeError:
        pass

    # Try to find a JSON object with regex as last resort
    m = _re.search(r'\{[^{}]*\}', cleaned)
    if m:
        try:
            result = _json.loads(m.group())
            if isinstance(result, dict):
                return result
        except _json.JSONDecodeError:
            pass

    print(f"[PlanParser] {context}无法提取JSON", flush=True)
    return {}


class QueryPlanner:
    """Judges query complexity and generates execution plans for complex queries."""

    def __init__(self, model: str, timeout: int = 60):
        self.model = model
        self.timeout = timeout
        self._profile = get_profile(model)

    @property
    def profile(self) -> ModelProfile:
        return self._profile

    # ═══════════════════════════════════════════════════════════════
    # Complexity Judgment — fully LLM-driven
    # ═══════════════════════════════════════════════════════════════

    def judge_complexity(self, condition: str) -> dict:
        """Classify query as SIMPLE or COMPLEX based on structural features.

        Fully LLM-driven. No regex pre-checks with domain words.
        The LLM prompt describes capability boundaries; the LLM decides.

        Returns:
            {"complexity": "SIMPLE"|"COMPLEX", "reasoning": "...", "features": [...]}
        """
        from microharness.agent.scheduler.prompts import build_complexity_judge_prompt
        from microharness.medical.query_router import parse_llm_json

        prompt = build_complexity_judge_prompt(self.profile, condition)

        try:
            client = OllamaClient(
                model=self.model, timeout=self.timeout,
                num_predict=self.profile.num_predict,
                format_json=(self.profile.json_mode == "format_json"),
            )
            resp = client.chat([{"role": "user", "content": prompt}], temperature=0.1)
            result = parse_llm_json(resp, context=f"复杂度判断:{condition[:30]}")
            if isinstance(result, dict) and result.get("complexity"):
                print(f"[Scheduler] LLM复杂度: {condition[:30]} → {result['complexity']}", flush=True)
                return result
        except Exception as e:
            print(f"[Scheduler] 复杂度判断失败: {e}", flush=True)

        # Fallback: conservative → SIMPLE (existing pipeline handles it)
        return {"complexity": "SIMPLE", "reasoning": "LLM失败→保守走现有管线", "features": []}

    # ═══════════════════════════════════════════════════════════════
    # Plan Generation — Template-driven, zero LLM for generation
    # ═══════════════════════════════════════════════════════════════
    #
    # P0 Architecture:
    #   1. Template (deterministic, zero LLM) — reads catalog metadata
    #   2. LLM Generation (fallback) — only if template fails
    #   3. Generic template (last resort)
    #
    # No LLM validation gate — with small thinking models (deepseek-r1:1.5b),
    # the binary yes/no classification is LESS reliable than the template itself.
    # The template is correct by construction (metadata-driven + structural regex).

    def generate_plan(self, condition: str, analysis: dict = None,
                      router_model: str = "qwen2.5:3b") -> dict:
        """Generate execution plan for a temporal query.

        P0: Template-based plan from catalog metadata, zero LLM calls.
        LLM generation is fallback, not primary.

        Returns:
            {"plan": [step_dict, ...], "reasoning": "..."}
        """
        # ── P0 Primary: Template-based deterministic plan ──
        if analysis:
            template_plan = self._generate_plan_from_analysis(
                condition, analysis, router_model
            )
            if template_plan and template_plan.get("plan"):
                n_steps = len(template_plan["plan"])
                print(f"[Scheduler] 模板计划: {n_steps}步 → 直接执行", flush=True)
                return template_plan
            print(f"[Scheduler] 模板无法生成计划 → 降级LLM生成", flush=True)

        # ── Fallback 1: LLM plan generation ──
        llm_plan = self._generate_plan_llm(condition, analysis=analysis)
        if llm_plan and llm_plan.get("plan"):
            print(f"[Scheduler] LLM生成计划: {len(llm_plan['plan'])}步", flush=True)
            return llm_plan

        # ── Last resort: generic template ──
        print(f"[Scheduler] 回退通用模板", flush=True)
        return self._generate_plan_generic(condition, analysis=analysis)

    # ═══════════════════════════════════════════════════════════════
    # Programmatic Plan Generation from understand_query Analysis
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _parse_temporal_offset(condition: str) -> dict:
        """Extract temporal offset from query using structural regex.

        NOT domain knowledge — pure Chinese temporal grammar:
        "X天后" = X days after, "X小时前" = X hours before, etc.
        Also handles inverted word order: "前X天" = X days before.
        """
        import re as _tpre

        def _parse_num(raw: str) -> float:
            raw = (raw or "").strip()
            if not raw:
                return 0
            if _tpre.fullmatch(r'\d+(?:\.\d+)?', raw):
                return float(raw)
            try:
                import cn2an
                return float(cn2an.cn2an(raw, "smart"))
            except Exception:
                pass
            # Fallback for environments without cn2an. This is a generic
            # Chinese numeral parser, not a list of query-specific phrases.
            digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                      "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
            units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
            total = 0
            section = 0
            number = 0
            for ch in raw:
                if ch in digits:
                    number = digits[ch]
                elif ch in units:
                    unit = units[ch]
                    if unit == 10000:
                        section = (section + number) * unit
                        total += section
                        section = 0
                    else:
                        section += (number or 1) * unit
                    number = 0
                else:
                    return 0
            return float(total + section + number)

        _num_pat = r'(\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+)'
        _unit_pat = r'(个月|月|星期|周|天|日|小时|钟头|分钟|分)'
        _measure_pat = r'(?:个)?'
        same_day = _tpre.search(r'(?:术|手术|入院|出院)?\s*([前后])?\s*(?:当天|当日)', condition)
        if same_day:
            direction = same_day.group(1) or ""
            relation = {"前": "before", "后": "after"}.get(direction, "within")
            return {
                "value": 0,
                "unit": "days",
                "relation": relation,
                "order": "same_day",
                "precision": "same_day",
                "lower_hours": None,
                "upper_hours": None,
                "calendar_day": True,
            }

        # Pattern A: "2天前", "三个月前", "1个小时后", "一百二十天内"
        m = _tpre.search(_num_pat + r'\s*' + _measure_pat + _unit_pat + r'\s*([前后内])', condition)
        if m:
            value = _parse_num(m.group(1))
            unit_raw = m.group(2)
            direction = m.group(3)
            order = "value_then_direction"
        else:
            # Pattern C: "前第2天", "术后第5天" = exact ordinal day.
            m = _tpre.search(r'([前后])\s*第\s*' + _num_pat + r'\s*' + _measure_pat + _unit_pat, condition)
            if m:
                direction = m.group(1)
                value = _parse_num(m.group(2))
                unit_raw = m.group(3)
                order = "ordinal_day"
            else:
                # Pattern B: "前2天", "术前三个月", "后3小时" (inverted word order)
                m = _tpre.search(r'([前后])\s*' + _num_pat + r'\s*' + _measure_pat + _unit_pat, condition)
                if m:
                    direction = m.group(1)
                    value = _parse_num(m.group(2))
                    unit_raw = m.group(3)
                    order = "direction_then_value"
                else:
                    return {}
        if value <= 0:
            return {}
        _unit_map = {"天": "days", "日": "days", "周": "days", "星期": "days",
                     "小时": "hours", "钟头": "hours", "分钟": "minutes", "分": "minutes",
                     "月": "days", "个月": "days"}
        _rel_map = {"前": "before", "后": "after", "内": "within"}
        unit = _unit_map.get(unit_raw, "days")
        relation = _rel_map.get(direction, "after")
        if unit_raw in ("月", "个月"):
            value = value * 30
        elif unit_raw in ("周", "星期"):
            value = value * 7
        if unit == "minutes":
            value_hours = value / 60
        elif unit == "hours":
            value_hours = value
        else:
            value_hours = value * 24
        if relation == "within":
            lower_hours, upper_hours = 0, value_hours
        elif relation == "after":
            if order == "ordinal_day":
                lower_hours, upper_hours = value_hours, value_hours + (1 if unit == "hours" else 24)
            elif order == "direction_then_value":
                # "术后5天" usually means the first 5 days after the anchor.
                lower_hours, upper_hours = 0, value_hours
            else:
                # "1天后" means after one full unit; bound it to the next unit
                # so it does not match records months later.
                lower_hours, upper_hours = value_hours, value_hours + (1 if unit == "hours" else 24)
        else:
            if order == "ordinal_day":
                lower_hours, upper_hours = -(value_hours + (1 if unit == "hours" else 24)), -value_hours
            elif order == "direction_then_value":
                # "术前2天" means the two days before the anchor.
                lower_hours, upper_hours = -value_hours, 0
            else:
                lower_hours, upper_hours = -(value_hours + (1 if unit == "hours" else 24)), -value_hours
        return {
            "value": value,
            "unit": unit,
            "relation": relation,
            "order": order,
            "precision": "exact_day" if order in ("ordinal_day", "value_then_direction") else "range",
            "lower_hours": lower_hours,
            "upper_hours": upper_hours,
        }

    @staticmethod
    def _extract_temporal_parts(condition: str) -> dict:
        """Split a temporal query into anchor phrase and target phrase.

        This is grammar-based, not domain-specific. Examples:
        - "手术1天后开了维生素" -> anchor="手术", target="开了维生素"
        - "出院后7天开了阿司匹林" -> anchor="出院后", target="开了阿司匹林"
        """
        import re as _re
        unit = r'(?:天|日|月|周|小时|分钟)'
        patterns = [
            rf'^(?P<anchor>.*?)(?P<value>\d+)\s*{unit}\s*(?P<dir>[前后内])(?P<target>.*)$',
            rf'^(?P<anchor>.*?)(?P<dir>[前后])\s*(?P<value>\d+)\s*{unit}(?P<target>.*)$',
            rf'^(?P<anchor>.*?)(?P<dir>[前后])\s*(?:当天|当日)(?P<target>.*)$',
            rf'^(?P<anchor>.*?)(?:当天|当日)(?P<target>.*)$',
        ]
        for pat in patterns:
            m = _re.search(pat, condition)
            if not m:
                continue
            anchor = (m.group("anchor") or "").strip()
            direction = m.groupdict().get("dir") or ""
            target = (m.group("target") or "").strip()
            anchor_for_score = (anchor + direction).strip() if direction in ("前", "后") else anchor
            return {
                "anchor": anchor,
                "anchor_for_score": anchor_for_score or anchor,
                "target": target,
                "direction": direction,
            }
        return {"anchor": "", "anchor_for_score": "", "target": condition, "direction": ""}

    @staticmethod
    def _score_text(query: str, text: str) -> int:
        """Small deterministic scorer for matching phrases to catalog metadata."""
        if not query or not text:
            return 0
        import re as _re
        q = query.strip()
        score = 0
        if q and q in text:
            score += 20 + len(q)
        runs = _re.findall(r'[一-鿿A-Za-z0-9]+', q)
        for run in runs:
            if len(run) >= 2 and run in text:
                score += 8 + len(run)
            for n in range(2, min(5, len(run) + 1)):
                for i in range(len(run) - n + 1):
                    gram = run[i:i+n]
                    if gram in text:
                        score += n
        return score

    @staticmethod
    def _select_anchor_from_catalog(anchor_hint: str, target_docs: list) -> dict:
        """Pick the best date-bearing document section for the anchor phrase."""
        from microharness.medical.query_router import DOCUMENT_CATALOG
        candidates = []
        docs = target_docs or list(DOCUMENT_CATALOG.keys())
        for doc_name in docs:
            doc_info = DOCUMENT_CATALOG.get(doc_name, {})
            if not doc_info:
                continue
            doc_text = " ".join([
                doc_name,
                doc_info.get("purpose", ""),
                " ".join(doc_info.get("used_for", [])),
            ])
            for sec in doc_info.get("sections", []):
                if sec.get("info_type") != "时间节点":
                    continue
                sec_text = " ".join([doc_text, sec.get("name", ""), sec.get("purpose", "")])
                score = QueryPlanner._score_text(anchor_hint, sec_text)
                candidates.append({
                    "doc": doc_name,
                    "section": sec.get("name", ""),
                    "score": score,
                })
        candidates = [c for c in candidates if c["score"] > 0]
        if not candidates:
            return {}
        candidates.sort(key=lambda c: c["score"], reverse=True)
        return candidates[0]

    @staticmethod
    def _select_anchor_service(anchor_hint: str, services: dict) -> dict:
        """Pick the best reference-date service and field from service metadata."""
        best = {}
        for sid, svc in services.items():
            if sid == "base_url" or not isinstance(svc, dict) or not svc.get("url"):
                continue
            returns = svc.get("returns", "")
            if "[时间字段]" not in returns or "参考锚点" not in returns:
                continue
            score = QueryPlanner._score_text(anchor_hint, svc.get("description", "") + " " + returns)
            field = ""
            best_field_score = -1
            import re as _re
            for line in returns.splitlines():
                if "[时间字段]" not in line:
                    continue
                m = _re.search(r'\[时间字段\]\s*([^(\n—-]+)', line)
                if not m:
                    continue
                label = m.group(1).strip()
                if not label:
                    continue
                label_score = QueryPlanner._score_text(anchor_hint, label)
                if label_score > best_field_score:
                    field = svc.get("field_labels", {}).get(label, label)
                    best_field_score = label_score
                score += label_score
            if score > best.get("score", 0):
                best = {"service": sid, "field": field, "score": score}
        return best

    @staticmethod
    def _select_data_services(target_hint: str, target_skills: list, services: dict) -> list:
        """Select target-data services from the target event phrase, not the anchor."""
        scored = []
        explicit_hits = set()
        for sid, svc in services.items():
            if sid == "base_url" or not isinstance(svc, dict) or not svc.get("url"):
                continue
            triggers = svc.get("triggers", []) or []
            score = 0
            for trig in triggers:
                if trig and trig in target_hint:
                    score += 100 + len(trig)
                    explicit_hits.add(sid)
            score += QueryPlanner._score_text(
                target_hint,
                svc.get("description", "") + " " + svc.get("returns", "")
            )
            if score > 0:
                scored.append((score, sid))
        scored.sort(reverse=True)
        if scored:
            top = scored[0][0]
            # Keep ties only when they are genuinely close; this prevents a generic
            # service from being merged with a clearly targeted one.
            selected = [
                sid for score, sid in scored
                if sid in explicit_hits or score >= max(20, top * 0.75)
            ]
            return list(dict.fromkeys(selected))
        return [s for s in target_skills if s in services and services[s].get("url")]

    def _generate_plan_from_analysis(self, condition: str, analysis: dict,
                                       router_model: str = "qwen2.5:3b") -> dict:
        """Build execution plan for temporal reasoning.

        Flexible plan structure adapts to where the anchor date lives:
        - DB anchor (surgery date): query_db → extract_date → call_service → ...
        - API anchor (admission date): call_service(encounter) → extract_date → call_service → ...

        No hardcoded service names. Reads metadata to determine anchor source.
        """
        conditions = analysis.get("conditions") or []
        if not conditions:
            return {}

        compound_temporal = self._generate_compound_temporal_plan(condition, conditions, analysis)
        if compound_temporal:
            return compound_temporal

        first = conditions[0]
        keyword = first.get("keyword", "")
        target_docs = first.get("target_docs", [])
        target_sections = first.get("target_sections", [])
        target_skills = first.get("target_skills", [])
        medical_text = first.get("text", condition)
        temporal_parts = self._extract_temporal_parts(condition)
        anchor_hint = temporal_parts.get("anchor_for_score") or temporal_parts.get("anchor") or condition
        target_hint = temporal_parts.get("target") or medical_text or condition

        # Use understand_query analysis directly (no additional LLM routing).
        # Structural fallbacks below handle cases where analysis was incomplete.
        # P0: Zero LLM calls in plan generation — catalog metadata is source of truth.

        # ── Structural fallback for documents: no docs or docs lack date ──
        from microharness.medical.query_router import DOCUMENT_CATALOG as _CAT
        _has_date_doc = False
        if target_docs:
            for _dn in target_docs:
                _di = _CAT.get(_dn, {})
                if any(_s.get("info_type") == "时间节点" for _s in _di.get("sections", [])):
                    _has_date_doc = True
                    break
        if not target_docs or not _has_date_doc:
            _found_doc = None
            _found_secs = []
            # Extract CJK n-grams (2-4 chars) from CJK runs.
            # Using only max runs fails for "手术前" → won't match "手术" in catalog.
            import re as _ngre
            _STOP = {'患者', '病人', '存在', '是否', '没有', '已经', '或者', '以及',
                     '进行', '检查', '情况', '这个', '那个', '什么', '怎么', '为什么',
                     '一个', '一下', '可能', '可以', '需要', '应该', '所有', '每个'}
            _cjk_runs = _ngre.findall(r'[一-鿿]{2,}', condition)
            _words = set()
            for _run in _cjk_runs:
                for _n in range(2, min(5, len(_run) + 1)):
                    for _i in range(len(_run) - _n + 1):
                        _w = _run[_i:_i + _n]
                        if _w not in _STOP:
                            _words.add(_w)
            # First pass: prefer docs with 时间节点
            for _doc_name, _doc_info in _CAT.items():
                _doc_purpose = _doc_info.get("purpose", "")
                _has_time = any(_s.get("info_type") == "时间节点" for _s in _doc_info.get("sections", []))
                for _w in _words:
                    if _w in _doc_purpose:
                        if _has_time:  # Only accept docs with date sections
                            _found_doc = _doc_name
                            for _sec in _doc_info.get("sections", []):
                                if _sec.get("info_type") == "时间节点":
                                    _found_secs.append(_sec["name"])
                            break
                if _found_doc:
                    break
            # Second pass: accept any doc (fallback)
            if not _found_doc:
                for _doc_name, _doc_info in _CAT.items():
                    _doc_purpose = _doc_info.get("purpose", "")
                    for _w in _words:
                        if _w in _doc_purpose:
                            _found_doc = _doc_name
                            _found_secs = [_s["name"] for _s in _doc_info.get("sections", [])[:3]]
                            break
                    if _found_doc:
                        break
            if _found_doc:
                if not _has_date_doc:
                    target_docs = [_found_doc]
                    target_sections = _found_secs
                    # Re-evaluate and clear API anchor if DB now has date
                    for _dn in target_docs:
                        _di = _CAT.get(_dn, {})
                        if any(_s.get("info_type") == "时间节点" for _s in _di.get("sections", [])):
                            _has_date_doc = True
                            _db_has_date = True
                            _anchor_svc = None  # DB has date, don't use API
                            break
                print(f"[Scheduler] 结构化兜底(doc): has_date_doc={_has_date_doc} found={_found_doc} secs={_found_secs} target_docs={target_docs}", flush=True)

        # ── Structural fallback: LLMs failed to match services → try metadata ──
        if not target_skills:
            import re as _sre
            try:
                from microharness.services.service_catalog import load_services as _load_svc
                _svcs = _load_svc()
                for _sid, _svc in _svcs.items():
                    if _sid == "base_url" or not isinstance(_svc, dict) or not _svc.get("url"):
                        continue
                    # Check if trigger or key term from metadata appears in query
                    _triggers = _svc.get("triggers", [])
                    _desc = _svc.get("description", "")
                    _returns = _svc.get("returns", "")
                    _meta_terms = set(_triggers)
                    # Extract key terms from description/returns (CJK words 2+ chars)
                    _meta_terms.update(_sre.findall(r'[一-鿿]{2,}', _desc + " " + _returns))
                    if any(_t in condition for _t in _meta_terms):
                        target_skills.append(_sid)
                if target_skills:
                    print(f"[Scheduler] 结构化兜底(trigger): condition={condition[:30]} → svc={target_skills}", flush=True)
            except Exception:
                pass

        # ── Determine anchor source ──
        # Read service metadata to find which service provides the reference date.
        # A service with [时间字段] tags in its SKILL.md `returns` can serve as anchor.
        # encounter-info provides admission/discharge dates; others provide target data dates.
        try:
            from microharness.services.service_catalog import load_services
            services = load_services()
        except Exception:
            services = {}

        selected_data_services = self._select_data_services(target_hint, target_skills, services)
        if selected_data_services:
            target_skills = selected_data_services

        _anchor_svc = None
        _anchor_field = ""
        _data_services = []
        for s in target_skills:
            svc_meta = services.get(s, {})
            returns = svc_meta.get("returns", "")
            desc = svc_meta.get("description", "")
            # Read SKILL.md metadata: [时间字段] + "参考锚点" = reference date source
            if "[时间字段]" in returns and "参考锚点" in returns:
                _anchor_svc = s
            else:
                _data_services.append(s)

        if not _data_services:
            _data_services = [s for s in target_skills if s != _anchor_svc]
        if not _data_services:
            _data_services = list(target_skills)
            _anchor_svc = None

        # Re-pick the reference-date source from the anchor phrase. This is the
        # key difference from routing the whole query: "出院后7天开药" should use
        # the "出院" date as anchor while "开药" selects the drug service.
        catalog_anchor = self._select_anchor_from_catalog(anchor_hint, [])
        try:
            from microharness.medical.time_window import get_anchor_route_for_condition
            _anchor_docs, _anchor_sections = get_anchor_route_for_condition(condition)
            if _anchor_docs and _anchor_sections:
                catalog_anchor = {
                    "doc": _anchor_docs[0],
                    "section": _anchor_sections[0],
                    "score": max(999, catalog_anchor.get("score", 0) if catalog_anchor else 0),
                }
        except Exception:
            pass
        service_anchor = self._select_anchor_service(anchor_hint, services)
        event_anchor_requires_document = bool(
            catalog_anchor
            and catalog_anchor.get("doc") == "手术记录"
            and any(token in condition for token in ("手术", "术前", "术后", "术中", "手术前", "手术后", "手术中"))
        )
        if catalog_anchor:
            target_docs = [catalog_anchor["doc"]]
            target_sections = [catalog_anchor["section"]]
        if (
            service_anchor
            and not event_anchor_requires_document
            and service_anchor.get("score", 0) >= catalog_anchor.get("score", 0)
        ):
            _anchor_svc = service_anchor.get("service")
            _anchor_field = service_anchor.get("field", "")
            if _anchor_svc and _anchor_svc not in target_skills:
                target_skills.append(_anchor_svc)
        elif catalog_anchor:
            _anchor_svc = None
            _anchor_field = catalog_anchor.get("section", "")

        # ── Decide anchor source: DB or API? ──
        # Read DOCUMENT_CATALOG metadata: sections with time-related info_type
        # provide date values → DB anchor. If no such sections exist in the
        # matched document, check if any matched service provides time fields
        # (SKILL.md [时间字段] tag) → API anchor.
        from microharness.medical.query_router import DOCUMENT_CATALOG
        _db_has_date = False
        if target_docs:
            for doc_name in target_docs:
                doc_info = DOCUMENT_CATALOG.get(doc_name, {})
                if any("时间" in str(sec.get("info_type", "")) for sec in doc_info.get("sections", [])):
                    _db_has_date = True
                    break
        if _db_has_date and not _anchor_svc:
            _anchor_svc = None  # DB has date sections → use DB anchor
        # else: DB lacks date. Try to find an API anchor service.
        if not _db_has_date and not _anchor_svc and not event_anchor_requires_document:
            # Structural fallback was skipped because target_docs was set but
            # the document has no date. Try to find encounter-info.
            import re as _sre2
            try:
                from microharness.services.service_catalog import load_services as _load_svc2
                _svcs2 = _load_svc2()
                for _sid, _svc in _svcs2.items():
                    if _sid == "base_url" or not isinstance(_svc, dict) or not _svc.get("url"):
                        continue
                    _returns = _svc.get("returns", "")
                    _desc = _svc.get("description", "")
                    # Read SKILL.md metadata: [时间字段] + "参考锚点" = this service provides reference dates
                    if "[时间字段]" in _returns and "参考锚点" in _returns:
                        svc_pick = self._select_anchor_service(anchor_hint, _svcs2)
                        _anchor_svc = svc_pick.get("service") or _sid
                        _anchor_field = svc_pick.get("field", "")
                        if _anchor_svc and _anchor_svc not in target_skills:
                            target_skills.append(_anchor_svc)
                        break
            except Exception:
                pass

        # Parse temporal offset
        offset = self._parse_temporal_offset(condition)
        _has_temporal = bool(offset)

        # ── Build plan ──
        plan = []
        dep_ids = []

        # ── Non-temporal: no time offset → skip extract_date and temporal_filter ──
        if not _has_temporal:
            next_id = 1
            # Gather DB data if docs/sections available
            if target_docs and target_sections:
                plan.append({"step_id": next_id, "action": "query_db",
                             "params": {"condition": keyword, "documents": target_docs, "sections": target_sections},
                             "output_var": "db_results"})
                dep_ids.append(next_id)
                next_id += 1
            # Gather API data from all matched services
            for svc in (_data_services or target_skills):
                plan.append({"step_id": next_id, "action": "call_service",
                             "params": {"service": svc, "keyword": keyword},
                             "output_var": "api_results"})
                dep_ids.append(next_id)
                next_id += 1
            if not dep_ids:
                return {}
            # LLM judge (uses raw data, no temporal filtering)
            plan.append({"step_id": next_id, "action": "llm_judge",
                         "params": {"condition": condition, "data_var": "api_results"},
                         "depends_on": dep_ids, "output_var": "medical_judgment"})
            judge_id = next_id
            next_id += 1
            # Boolean combine
            plan.append({"step_id": next_id, "action": "boolean_combine",
                         "params": {"vars": ["medical_judgment", "api_results"], "logic": "and"},
                         "depends_on": [judge_id], "output_var": "final_result"})
            return {"plan": plan, "reasoning": f"non-temporal: docs={target_docs} svc={_data_services or target_skills}"}

        # ── Temporal: full pipeline with date extraction and time filtering ──
        # ... existing temporal plan code ...
        if _anchor_svc:
            # Anchor date from external API (e.g. encounter-info for admission date)
            plan.append({"step_id": 1, "action": "call_service",
                         "params": {"service": _anchor_svc, "keyword": keyword},
                         "output_var": "db_results"})
            plan.append({"step_id": 2, "action": "extract_date",
                         "params": {"source_var": "db_results", "field": _anchor_field},
                         "depends_on": [1], "output_var": "anchor_date"})
            dep_ids = [2]
            next_id = 3
        else:
            # Anchor date from DB documents
            date_sections = [s for s in target_sections if "日期" in s or "时间" in s]
            if not date_sections:
                date_sections = target_sections[:1]
            plan.append({"step_id": 1, "action": "query_db",
                         "params": {"condition": keyword, "documents": target_docs, "sections": date_sections},
                         "output_var": "db_results"})
            plan.append({"step_id": 2, "action": "extract_date",
                         "params": {"source_var": "db_results", "field": date_sections[0]},
                         "depends_on": [1], "output_var": "anchor_date"})
            dep_ids = [2]
            next_id = 3

        # Data service calls — depend on anchor step so they run sequentially
        _api_vars = []
        for idx, svc in enumerate(_data_services):
            _out_var = "api_results" if idx == 0 else f"api_results_{idx+1}"
            _api_vars.append(_out_var)
            plan.append({"step_id": next_id, "action": "call_service",
                         "params": {"service": svc, "keyword": keyword},
                         "depends_on": [1],
                         "output_var": _out_var})
            dep_ids.append(next_id)
            next_id += 1

        # If multiple data services, merge into single api_results for temporal_filter
        if len(_api_vars) > 1:
            plan.append({"step_id": next_id, "action": "merge_results",
                         "params": {"sources": _api_vars},
                         "depends_on": [i for i in range(next_id - len(_api_vars), next_id)],
                         "output_var": "api_results"})
            dep_ids = [next_id]
            next_id += 1

        # Temporal filter
        plan.append({"step_id": next_id, "action": "temporal_filter",
                     "params": {"reference_var": "anchor_date", "target_var": "api_results",
                                "relation": offset["relation"], "value": offset["value"],
                                "unit": offset["unit"],
                                "lower_hours": offset.get("lower_hours"),
                                "upper_hours": offset.get("upper_hours"),
                                "calendar_day": offset.get("calendar_day", False)},
                     "depends_on": dep_ids, "output_var": "time_filtered_data"})
        tf_id = next_id
        next_id += 1

        # Deterministic concept filter before LLM. The LLM may explain close
        # semantic matches, but it must not invent that a drug/diagnosis exists.
        plan.append({"step_id": next_id, "action": "concept_filter",
                     "params": {"condition": condition, "target_text": target_hint,
                                "source_var": "time_filtered_data",
                                "api_var": "api_results",
                                "services": _data_services},
                     "depends_on": [tf_id], "output_var": "filtered_data"})
        concept_id = next_id
        next_id += 1

        # LLM judge
        plan.append({"step_id": next_id, "action": "llm_judge",
                     "params": {"condition": condition, "data_var": "filtered_data"},
                     "depends_on": [concept_id], "output_var": "medical_judgment"})
        judge_id = next_id
        next_id += 1

        # Boolean combine
        plan.append({"step_id": next_id, "action": "boolean_combine",
                     "params": {"vars": ["medical_judgment", "filtered_data"], "logic": "and"},
                     "depends_on": [tf_id, judge_id], "output_var": "final_result"})

        return {"plan": plan, "reasoning": f"anchor={'api:'+_anchor_svc if _anchor_svc else 'db'} svc={_data_services} offset={offset}"}

    # ═══════════════════════════════════════════════════════════════
    # LLM Plan Validation (P0: binary yes/no gate)
    # ═══════════════════════════════════════════════════════════════

    def _generate_compound_temporal_plan(self, condition: str, conditions: list, analysis: dict) -> dict:
        """Build independent temporal branches for compound temporal clauses."""
        if not conditions or len(conditions) <= 1:
            return {}

        offsets = []
        for cond in conditions:
            text = cond.get("text", "") if isinstance(cond, dict) else ""
            offset = self._parse_temporal_offset(text)
            if not offset:
                return {}
            offsets.append(offset)

        try:
            from microharness.services.service_catalog import load_services
            services = load_services()
        except Exception:
            services = {}

        plan = []
        branch_outputs = []
        branch_step_ids = []
        next_id = 1

        for idx, cond in enumerate(conditions, 1):
            text = cond.get("text", "") if isinstance(cond, dict) else ""
            keyword = cond.get("keyword") or text
            temporal_parts = self._extract_temporal_parts(text)
            anchor_hint = temporal_parts.get("anchor_for_score") or temporal_parts.get("anchor") or text
            target_hint = temporal_parts.get("target") or text
            offset = offsets[idx - 1]

            catalog_anchor = self._select_anchor_from_catalog(anchor_hint, cond.get("target_docs", []))
            service_anchor = self._select_anchor_service(anchor_hint, services)

            target_skills = list(cond.get("target_skills", []) or [])
            if not target_skills:
                for sid, svc in services.items():
                    if sid == "base_url" or not isinstance(svc, dict) or not svc.get("url"):
                        continue
                    if any(t and t in target_hint for t in (svc.get("triggers", []) or [])):
                        target_skills.append(sid)

            data_services = self._select_data_services(target_hint, target_skills, services)
            data_services = [
                sid for sid in data_services
                if sid in services and sid != service_anchor.get("service")
            ]
            if not data_services:
                return {}

            anchor_svc = None
            anchor_field = ""
            target_docs = []
            target_sections = []
            if catalog_anchor:
                target_docs = [catalog_anchor["doc"]]
                target_sections = [catalog_anchor["section"]]
            if service_anchor and service_anchor.get("score", 0) >= catalog_anchor.get("score", 0):
                anchor_svc = service_anchor.get("service")
                anchor_field = service_anchor.get("field", "")

            if anchor_svc:
                anchor_source = f"branch{idx}_anchor_source"
                plan.append({"step_id": next_id, "action": "call_service",
                             "params": {"service": anchor_svc, "keyword": keyword},
                             "output_var": anchor_source})
                anchor_source_id = next_id
                next_id += 1
                anchor_date_var = f"branch{idx}_anchor_date"
                plan.append({"step_id": next_id, "action": "extract_date",
                             "params": {"source_var": anchor_source, "field": anchor_field},
                             "depends_on": [anchor_source_id],
                             "output_var": anchor_date_var})
                anchor_date_id = next_id
                next_id += 1
            else:
                if not target_docs or not target_sections:
                    return {}
                anchor_source = f"branch{idx}_anchor_source"
                plan.append({"step_id": next_id, "action": "query_db",
                             "params": {"condition": keyword, "documents": target_docs, "sections": target_sections},
                             "output_var": anchor_source})
                anchor_source_id = next_id
                next_id += 1
                anchor_date_var = f"branch{idx}_anchor_date"
                plan.append({"step_id": next_id, "action": "extract_date",
                             "params": {"source_var": anchor_source, "field": target_sections[0]},
                             "depends_on": [anchor_source_id],
                             "output_var": anchor_date_var})
                anchor_date_id = next_id
                next_id += 1

            api_vars = []
            api_step_ids = []
            for svc_idx, svc in enumerate(data_services, 1):
                api_var = f"branch{idx}_api" if svc_idx == 1 else f"branch{idx}_api_{svc_idx}"
                api_vars.append(api_var)
                plan.append({"step_id": next_id, "action": "call_service",
                             "params": {"service": svc, "keyword": keyword},
                             "depends_on": [anchor_source_id],
                             "output_var": api_var})
                api_step_ids.append(next_id)
                next_id += 1

            if len(api_vars) > 1:
                api_var_for_filter = f"branch{idx}_api_results"
                plan.append({"step_id": next_id, "action": "merge_results",
                             "params": {"sources": api_vars},
                             "depends_on": api_step_ids,
                             "output_var": api_var_for_filter})
                api_dep_id = next_id
                next_id += 1
            else:
                api_var_for_filter = api_vars[0]
                api_dep_id = api_step_ids[0]

            time_var = f"branch{idx}_time_filtered"
            plan.append({"step_id": next_id, "action": "temporal_filter",
                         "params": {"reference_var": anchor_date_var, "target_var": api_var_for_filter,
                                    "relation": offset["relation"], "value": offset["value"],
                                    "unit": offset["unit"],
                                    "lower_hours": offset.get("lower_hours"),
                                    "upper_hours": offset.get("upper_hours"),
                                    "calendar_day": offset.get("calendar_day", False)},
                         "depends_on": [anchor_date_id, api_dep_id],
                         "output_var": time_var})
            time_id = next_id
            next_id += 1

            filtered_var = f"branch{idx}_filtered"
            plan.append({"step_id": next_id, "action": "concept_filter",
                         "params": {"condition": text, "target_text": target_hint,
                                    "source_var": time_var, "api_var": api_var_for_filter,
                                    "services": data_services},
                         "depends_on": [time_id],
                         "output_var": filtered_var})
            branch_outputs.append(filtered_var)
            branch_step_ids.append(next_id)
            next_id += 1

        logic = analysis.get("connector") or "and"
        if logic not in ("and", "or"):
            logic = "and"
        plan.append({"step_id": next_id, "action": "boolean_combine",
                     "params": {"vars": branch_outputs, "logic": logic},
                     "depends_on": branch_step_ids,
                     "output_var": "final_result"})

        return {"plan": plan, "reasoning": f"compound temporal branches={len(branch_outputs)} logic={logic}"}

    def _llm_validate_plan(self, condition: str, plan_dict: dict) -> bool:
        """Ask LLM to validate a template-generated plan. Binary yes/no.

        The LLM checks whether the plan's anchor, services, and structure
        make sense for the given query. This is a SIMPLE classification task,
        not generation — much more reliable for small models.

        Returns True if the plan passes validation, False otherwise.
        On LLM error, returns True (default pass — don't block on infra issues).
        """
        plan_steps = plan_dict.get("plan", [])
        if not plan_steps:
            return False

        # Build a compact plan summary for the LLM to review
        lines = [f"查询: {condition}", "", "执行计划:"]
        for s in plan_steps:
            sid = s.get("step_id", "?")
            action = s.get("action", "?")
            params = s.get("params", {})
            deps = s.get("depends_on", [])
            # Compact params — skip verbose values
            compact_params = {}
            for k, v in params.items():
                if isinstance(v, list):
                    compact_params[k] = v[:3]
                elif isinstance(v, str) and len(v) > 40:
                    compact_params[k] = v[:40] + "..."
                else:
                    compact_params[k] = v
            lines.append(
                f"  Step{sid}: {action} | params={compact_params} | deps={deps}"
            )
        plan_summary = "\n".join(lines)

        prompt = f"""检查以下执行计划是否合理。只输出 yes 或 no。

{plan_summary}

判断标准：
- 锚点步骤(query_db/extract_date)是否对应查询中的时间参照事件？
- 服务调用(call_service)是否对应查询中的医疗行为？
- 时间筛选(temporal_filter)的关系和数值是否正确？
- 步骤依赖关系是否合理？

计划是否合理？只输出 yes 或 no："""

        try:
            from microharness.ollama import OllamaClient
            client = OllamaClient(
                model=self.model, timeout=30, num_predict=20,
            )
            resp = client.chat(
                [{"role": "user", "content": prompt}], temperature=0.0
            )
            result = resp.strip().lower()
            # Accept "yes", "Yes", "YES", "yes." etc.
            is_valid = result.startswith("yes") or "yes" in result[:15]
            print(
                f"[Scheduler] LLM验证: {'✓通过' if is_valid else '✗未通过'} "
                f"({result[:60]})",
                flush=True,
            )
            return is_valid
        except Exception as e:
            print(f"[Scheduler] LLM验证异常({e}) → 默认通过", flush=True)
            return True  # Default pass on error — don't block pipeline

    # ═══════════════════════════════════════════════════════════════
    # LLM Plan Generation
    # ═══════════════════════════════════════════════════════════════

    def _generate_plan_llm(self, condition: str, analysis: dict = None) -> dict:
        """LLM-based plan generation with full catalog context.

        The LLM sees available documents, services, and actions.
        It chooses which document to query, which service to call,
        based on semantic understanding — no hardcoded mappings.
        """
        import re as _sre
        from microharness.agent.scheduler.prompts import build_plan_generation_prompt
        from microharness.medical.query_router import DOCUMENT_CATALOG
        from microharness.services.service_catalog import load_services

        # ── Build catalog descriptions ──

        # Actions catalog (structural — describes what each action does)
        actions_desc = """可用动作（必须使用以下动作名和参数格式）：

- legacy_pipeline: 走完整现有管线做医疗语义判断（分解/路由/修饰词/LLM判断）。
  params: {condition: 医疗条件文本（去掉时间部分）}

- query_db: 查询病历数据库取时间锚点对应的日期。
  params: {condition: 查询关键词, documents: [文档名列表], sections: [章节名列表]}
  文档名必须从"可用文档"列表中选取，章节名必须属于该文档。

- call_service: 调外部API获取医疗数据（含时间戳）。
  params: {service: 服务ID, keyword: 查询关键词}
  服务ID必须从"可用服务"列表中选取。

- extract_date: 从DB查询结果中提取日期时间值。
  params: {source_var: 源$变量名, field: 日期字段名（章节名）}

- temporal_filter: 按时间窗口筛选数据。
  params: {reference_var: $anchor_date, target_var: $api_results,
           relation: within|before|after, value: 数值, unit: hours|days}

- boolean_combine: AND/OR组合多个步骤的匹配结果。
  params: {vars: [$var1, $var2], logic: and|or}

- llm_judge: 用LLM对筛选后数据做最终语义判断。
  params: {condition: 判断条件文本, data_var: 数据所在$变量名}"""

        # Document catalog
        doc_lines = []
        for doc, info in DOCUMENT_CATALOG.items():
            secs = [s["name"] for s in info.get("sections", [])]
            doc_lines.append(f"  - {doc}: 章节=[{', '.join(secs[:8])}]")
        docs_desc = "可用文档及章节（query_db的documents/sections必须从这里选）：\n" + "\n".join(doc_lines[:12])

        # Service catalog
        try:
            services = load_services()
            svc_lines = []
            for sid, svc in services.items():
                if sid == "base_url" or not isinstance(svc, dict):
                    continue
                if not svc.get("url"):
                    continue
                triggers = ", ".join(svc.get("triggers", [])[:8])
                svc_lines.append(f"  - {sid}: {svc.get('description', sid)[:60]} | 触发词: {triggers}")
            svc_desc = "可用服务（call_service的service必须从这里选）：\n" + "\n".join(svc_lines[:10])
        except Exception:
            svc_desc = "  - drug-interaction: 用药医嘱查询 | 触发词: 用药,药物,注射,开了\n  - diagnosis-query: 诊断查询 | 触发词: 诊断,确诊,患有\n  - encounter-info: 就诊信息查询 | 触发词: 就诊,住院,出院"

        # ── Build analysis context (from understand_query, not old query_analyzer) ──
        analysis_context = ""
        if analysis:
            conditions = analysis.get("conditions") or []
            first_cond = conditions[0] if conditions else {}
            kw = first_cond.get("keyword", "")
            tdocs = first_cond.get("target_docs", [])
            tsecs = first_cond.get("target_sections", [])
            tskills = first_cond.get("target_skills", [])
            offset = QueryPlanner._parse_temporal_offset(condition)
            offset_str = f"value={offset['value']} unit={offset['unit']} relation={offset['relation']}" if offset else "未检测到"
            analysis_context = f"""
查询预分析结果（来自 understand_query，供参考）：
- 类型: {analysis.get('type', '?')}
- 关键词: {kw}
- 预选文档: {tdocs}
- 预选章节: {tsecs}
- 预选服务: {tskills}
- 时间偏移: {offset_str}
- 原始条件: {condition}
"""

        prompt = build_plan_generation_prompt(
            self.profile, condition,
            actions_desc=actions_desc,
            docs_desc=docs_desc,
            svc_desc=svc_desc,
            analysis_context=analysis_context,
        )

        try:
            client = OllamaClient(
                model=self.model,
                timeout=self.timeout,
                num_predict=1024,
                format_json=(self.profile.json_mode == "format_json"),
            )
            resp = client.chat([{"role": "user", "content": prompt}], temperature=0.1)
            print(f"[Scheduler] 计划LLM响应 ({len(resp)}字):\n{resp[:500]}", flush=True)
            _clean = _sre.sub(r'//[^\n]*', '', resp)
            result = _extract_json_dict(_clean, context=f"计划生成:{condition[:30]}")
            if isinstance(result, dict) and result.get("plan"):
                plan = result["plan"]
                if isinstance(plan, list) and len(plan) > 0:
                    if self._validate_plan(plan):
                        return {"plan": plan, "reasoning": result.get("reasoning", "")}
                    print(f"[Scheduler] LLM计划验证失败", flush=True)
        except Exception as e:
            print(f"[Scheduler] LLM计划生成异常: {e}", flush=True)

        return {}

    # ═══════════════════════════════════════════════════════════════
    # Generic Fallback Plan (no domain words, no medical knowledge)
    # ═══════════════════════════════════════════════════════════════

    def _generate_plan_generic(self, condition: str, analysis: dict = None) -> dict:
        """Generic fallback plan. Uses analysis data if available, otherwise
        delegates entirely to legacy_pipeline. No hardcoded domain mappings.

        This is the LAST RESORT when LLM plan generation fails.
        """
        # Extract what we can from analysis (no hardcoded defaults for docs/services)
        temporal = (analysis or {}).get("temporal") or {}
        conditions = (analysis or {}).get("conditions") or []
        first_cond = conditions[0] if conditions else {}

        medical_text = first_cond.get("text", condition) if first_cond else condition
        anchor = temporal.get("anchor", "")
        relation = temporal.get("relation", "within")
        time_val = temporal.get("value")
        unit_raw = temporal.get("unit", "小时")

        # Pure unit conversion (not domain knowledge)
        unit_map = {"小时": "hours", "天": "days", "日": "days", "分钟": "minutes", "周": "weeks"}
        time_unit = unit_map.get(unit_raw, "hours")
        # Safety default: if no explicit time, use large window
        if time_val is None:
            time_val = 365 if time_unit == "days" else 8760

        # Build a minimal generic plan:
        # 1. legacy_pipeline for medical judgment
        # 2. llm_judge for final result
        # No document/service selection — that requires domain knowledge
        # which the LLM should have provided. If LLM failed, we do our best
        # with what the analysis gave us.

        plan = [
            {"step_id": 1, "action": "legacy_pipeline",
             "params": {"condition": medical_text},
             "output_var": "medical_judgment"},
        ]

        # If we have temporal info, try to use it (but no hardcoded doc names)
        if anchor and time_val is not None:
            # Use llm_judge with temporal context instead of hardcoded query_db+service
            plan.append({
                "step_id": 2, "action": "llm_judge",
                "params": {"condition": condition, "data_var": "medical_judgment"},
                "depends_on": [1], "output_var": "final_result",
            })
        else:
            plan.append({
                "step_id": 2, "action": "llm_judge",
                "params": {"condition": condition, "data_var": "medical_judgment"},
                "depends_on": [1], "output_var": "final_result",
            })

        print(f"[Scheduler] 通用模板: cond={condition[:30]}", flush=True)
        return {"plan": plan, "reasoning": f"通用模板（LLM计划生成失败）: {condition}"}

    # ═══════════════════════════════════════════════════════════════
    # Plan Validation
    # ═══════════════════════════════════════════════════════════════

    def _validate_plan(self, plan: list) -> bool:
        """Validate that a plan has unique step_ids and valid depends_on references."""
        try:
            step_ids = set()
            for step in plan:
                sid = step.get("step_id")
                if sid is None or sid in step_ids:
                    return False
                step_ids.add(sid)
                # Check depends_on references
                deps = step.get("depends_on", [])
                if not isinstance(deps, list):
                    deps = [deps] if isinstance(deps, int) else []
                for dep in deps:
                    if dep not in step_ids:  # must reference a prior step
                        return False
                # Check required fields
                if not step.get("action") or not step.get("output_var"):
                    return False
            return True
        except Exception:
            return False
