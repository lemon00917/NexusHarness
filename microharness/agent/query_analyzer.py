"""
Query Analyzer
==============
Single LLM call analyzes query structure, replacing scattered regex cascades.

Returns purely structural analysis — no medical knowledge, no system routing.
Service/document mapping happens downstream using catalogs.

Output format:
    {"type": "simple"|"compound"|"temporal", "negated": bool,
     "connector": "and"|"or"|null,
     "conditions": [{"text": "...", "verb": "...", "target": "..."}],
     "temporal": {"anchor": "...", "value": N, "unit": "...", "relation": "within"|"before"|"after"}|null}
"""

from __future__ import annotations
from typing import Optional


def analyze_query(condition: str, model: str = "qwen2.5:3b",
                  timeout: int = 30) -> dict:
    """Analyze query structure. Structural pre-check (0ms) + LLM for details.

    Structural pre-check skips LLM only for trivially simple queries.
    LLM determines type (temporal/simple/compound) and extracts details.
    NO hardcoded medical/anchor words in code — all pattern recognition in LLM prompt.
    """
    import re as _re

    # ═══════════════════════════════════════════════════════════
    # Stage 1: Structural pre-check (0ms, no medical knowledge)
    # Only skip LLM for trivially simple queries — purely structural.
    # ═══════════════════════════════════════════════════════════
    _negated = bool(_re.match(r'(不存在|没有|无|非)\s*', condition))
    _clean = _re.sub(r'^(不存在|没有|无|非)\s*', '', condition) if _negated else condition

    # Purely structural features (no domain words):
    _has_connector = bool(_re.search(r'并且|且|和|与|或者|或|还是', _clean))
    _has_number = bool(_re.search(r'\d+', _clean))
    _len = len(_clean)

    # Trivially simple: short, no connectors, no numbers → skip LLM (performance)
    if not _has_connector and not _has_number and _len < 15:
        print(f"[QueryAnalyzer] 结构简单 → 跳过LLM: {condition[:30]}", flush=True)
        return {
            "type": "simple", "negated": _negated, "connector": None,
            "conditions": [{"text": _clean, "verb": None, "target": _clean}],
            "temporal": None,
        }

    # ═══════════════════════════════════════════════════════════
    # Stage 2: LLM determines type + extracts details
    # The LLM prompt teaches pattern recognition — NO hardcoded word lists.
    # ═══════════════════════════════════════════════════════════
    llm_result = _llm_analyze(_clean, model, timeout)
    if llm_result:
        llm_result["negated"] = _negated
        return llm_result

    # ═══════════════════════════════════════════════════════════
    # Stage 3: Regex fallback (structural only, no domain words)
    # ═══════════════════════════════════════════════════════════
    print(f"[QueryAnalyzer] regex fallback: {condition[:30]}", flush=True)
    return _fallback_analyze(condition, negated=_negated)


def _llm_analyze(condition: str, model: str, timeout: int) -> dict:
    """LLM determines query type and extracts structure.

    The prompt teaches the LLM to recognize temporal patterns,
    compound connectors, and extract conditions — without hardcoding
    specific anchor/service words in code.
    """
    import json as _json
    import re as _re

    prompt = f"""分析以下中文医疗查询的语法结构。输出JSON。

查询：{condition}

你需要判断查询类型并提取结构：

1. **temporal（时间约束型）** — 查询包含一个"时间参照事件" + 另一个需要与该事件比较时间的"医疗行为"
   - 时间参照事件：通常是某个医疗节点（手术、出院、入院等），查询中以它作为时间计算的起点
   - 识别方法：查询中出现了类似"X后Y天内Z"、"X前Z"的模式，其中X是参照事件，Z是医疗行为
   - 例如："手术后3天内开了葡萄糖" — 手术=参照事件，开了葡萄糖=医疗行为
   - 例如："出院后5天复诊" — 出院=参照事件，复诊=医疗行为
   - temporal 字段:
     · anchor: 时间参照事件词（从查询中直接提取，如"手术"、"出院"、"入院"）
     · value: 时间数值（数字，无则null）
     · unit: 时间单位（小时/天/日/周/分钟）
     · relation: 时间关系
       - "X后Y天内" / "X后Y天"（无"后"修饰）→ within（参照事件后0到Y时间内）
       - "X后Y天后" / "X后Y天之后" → after（至少Y时间后）
       - "X前Y小时" / "X前Y天" → before（参照事件前Y时间内）
       - 无明确时间数值（如"X前已Z"）→ before, value=null
   - conditions[0].text: 去掉时间前缀后的医疗条件部分
     · "术后3天开了葡萄糖" → "开了葡萄糖"
     · "入院前已诊断糖尿病" → "已诊断糖尿病"
   - conditions[0].verb: 医疗动作词（开了/注射/用了/服用/诊断/检查/复诊 等），无法确定填null
   - conditions[0].target: 核心名词（药物名/疾病名/检查名），无法确定填null

2. **compound（复合条件型）** — 查询用"并且/且/和/与"或"或者/或"连接了多个独立条件
   - connector: "and"（并且/且/和/与）或 "or"（或者/或）
   - conditions: 每个子条件的text/verb/target
   - 注意：如果查询同时有复合连接词和时间约束，优先判为temporal

3. **simple（简单条件型）** — 单一医疗条件，无时间约束，无复合连接

重要规则：
- conditions数组不能为null，至少包含一个有效对象
- 每个condition的text字段必须是非空字符串
- 不要编造内容，从查询原文中提取

只输出JSON：
{{"type":"temporal或simple或compound","conditions":[{{"text":"非空字符串","verb":"...或null","target":"...或null"}}],"connector":"and或or或null","temporal":{{"anchor":"...","value":数字或null,"unit":"...","relation":"within或before或after"}}或null}}"""

    try:
        from microharness.ollama import OllamaClient
        client = OllamaClient(model=model, timeout=timeout,
                             num_predict=256, format_json=True)
        resp = client.chat([{"role": "user", "content": prompt}], temperature=0.1)

        for fence in ("```json", "```"):
            if fence in resp:
                parts = resp.split(fence)
                if len(parts) >= 2:
                    resp = parts[1].split("```")[0] if "```" in parts[1] else parts[1]
                    resp = resp.strip()
                    break
        resp = _re.sub(r'//[^\n]*', '', resp)

        result = _json.loads(resp)
        if isinstance(result, dict) and result.get("type"):
            # Filter out null/invalid conditions
            conds = result.get("conditions") or []
            result["conditions"] = [c for c in conds if isinstance(c, dict) and c.get("text")]
            if not result["conditions"]:
                print(f"[QueryAnalyzer] LLM返回空conditions → 丢弃", flush=True)
                return {}
            print(f"[QueryAnalyzer] LLM: {condition[:30]} → {result.get('type')} conds={[c.get('text','') for c in result['conditions']]}", flush=True)
            return result
    except Exception as e:
        print(f"[QueryAnalyzer] LLM失败({condition[:20]}): {e}", flush=True)
    return {}


def _fallback_analyze(condition: str, negated: bool = False) -> dict:
    """Pure structural regex fallback. No domain-specific words.

    Only uses: negation prefix, compound connectors, numbers.
    Does NOT use medical terms or temporal anchor words.
    """
    import re

    _clean = re.sub(r'^(不存在|没有|无|非)\s*', '', condition) if negated else condition

    # Compound detection (structural only)
    has_and = bool(re.search(r'并且|且|和|与', _clean))
    has_or = bool(re.search(r'或者|或|还是', _clean))

    if has_and or has_or:
        _kw = r'并且|且|和|与' if has_and else r'或者|或|还是'
        parts = re.split(rf'(?:{_kw})\s*', _clean)
        parts = [p.strip() for p in parts if len(p.strip()) >= 2]
        return {
            "type": "compound", "negated": negated,
            "connector": "and" if has_and else "or",
            "conditions": [{"text": p, "verb": None, "target": p} for p in parts],
            "temporal": None,
        }

    punct_parts = re.split(r'[，,；;]\s*', _clean)
    punct_parts = [p.strip() for p in punct_parts if len(p.strip()) >= 3]
    if len(punct_parts) > 1:
        return {
            "type": "compound", "negated": negated,
            "connector": "and",
            "conditions": [{"text": p, "verb": None, "target": p} for p in punct_parts],
            "temporal": None,
        }

    # Default: simple
    return {
        "type": "simple", "negated": negated, "connector": None,
        "conditions": [{"text": _clean, "verb": None, "target": _clean}],
        "temporal": None,
    }
