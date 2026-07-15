"""
Prompt Adapter
==============
Builds model-appropriate prompts based on ModelProfile.

Each prompt has 3 variants:
- "native":  for deepseek-r1 etc. — direct, no CoT, model handles reasoning
- "relaxed": for qwen3.5 etc. — CoT but fewer guardrails
- "strict":  for qwen2.5:3b etc. — full CoT, explicit guardrails
"""

from microharness.ollama.model_profile import ModelProfile


def build_query_normalization_prompt(profile: ModelProfile, raw_condition: str,
                                     deterministic_condition: str) -> str:
    """Build a prompt that repairs user wording without judging data."""
    return f"""你是病历筛选查询的归一化器。你的任务是把用户输入纠错、补全成更标准的查询表达，但不要判断患者是否符合。

原始问题：
{raw_condition}

程序已做的基础符号归一化：
{deterministic_condition}

要求：
1. 只修正错别字、漏字、口语省略、比较符号、中文数字、明显的医学查询表达。
2. 不要新增原句没有暗示的疾病、药物、检查、检验项目。
3. 不要计算患者是否符合，不要编造病历证据。
4. 多条件要保留，不要丢条件。
5. 对 “》/＞/>/大于/高于” 统一为 “>”，“《/＜/</小于/低于” 统一为 “<”。
6. 对 15×10⁹、15*10⁹、15*10^9、15×10^9 保留为规范科学计数表达，不要用自然语言解释。
7. 如果原句少了动词，可以按医疗常识补成查询动作，例如“术后五天感染”可规范为“术后5天诊断为感染”。
8. 如果不确定，保守保留原意，并把 needs_review 设为 true。

只输出 JSON：
{{
  "normalized_condition": "规范后的查询句",
  "corrections": ["修正说明"],
  "confidence": 0.0到1.0,
  "needs_review": false
}}"""


def build_judge_prompt(profile: ModelProfile, sq: str, kw_hint: str,
                       judge_summary: str, hints: str,
                       modifiers: list = None) -> str:
    """Build the per-file judge prompt adapted to model capabilities.

    Args:
        modifiers: Optional list of modifier words (e.g. ["没有输血"], ["治好"]).
                   When provided, modifier verification is merged into this
                   single judge call instead of a separate LLM round.
    """

    # ── Build modifier guidance section ──
    mod_section = ""
    if modifiers:
        mod_list = "、".join(modifiers)
        mod_section = f"""
修饰词（需在本次判断中一并验证）：{mod_list}
修饰词判断规则：
- "没有好转/未缓解/无改善/不见好"不是普通否定词，必须有明确的未改善/仍存在/持续/加重证据才算满足；如果字段写"好转/改善/缓解/无不适"，则不满足
- 否定型修饰词（含"没有/无/不/未"）：检查字段值中是否"存在"被否定的内容。不存在→修饰词满足；存在→不满足
  例："没有输血" → 字段中无"输血"字样→满足；有"输血"→不满足
- 状态型修饰词（治好/好转/加重/恶化/复发/缓解/不退）：检查字段值中是否有对应状态变化的证据
  例："治好" → 字段中有"好转/治愈/改善/恢复"→满足；无证据→不满足
- 诊断接口只说明诊断名称和诊断类型，不能单独证明"好转/没有好转"等转归；除非查询明确问"出院诊断/仍诊断为"，且记录类型是出院诊断
- 修饰词不满足时 matched=false
"""

    if profile.thinking == "native":
        # ── Native thinking (deepseek-r1): direct, no hand-holding ──
        # Model reasons internally; we just need a clean JSON answer.
        prompt = f"""判断患者数据是否匹配以下条件。在reasoning字段简述推理，matched为true或false。

条件：{sq}
"""
        if kw_hint:
            prompt += f"\n核心关键词：{kw_hint}\n"
        prompt += f"""
字段值：
{judge_summary[:1500]}
"""
        if hints:
            prompt += f"""
预计算值（直接使用）：
{hints}
"""
        prompt += mod_section
        prompt += """
输出JSON格式（必须同时包含reasoning和reason两个字段）：
{{"reasoning":"简述推理过程","matched":true或false,"reason":"用户可读的一句话理由"}}"""
        return prompt

    elif profile.prompt_style == "relaxed":
        # ── Relaxed CoT (qwen3.5): shorter steps, trust model ──
        prompt = f"""你是病历筛选判断器。逐步推理后输出JSON。

条件：{sq}{kw_hint}

字段值：
{judge_summary[:1500]}
"""
        if hints:
            prompt += f"""
预计算值（已自动计算，直接使用）：
{hints}
"""
        prompt += mod_section
        prompt += """
推理步骤：
1. 确认核心关键词
2. 扫描字段值是否包含关键词（连续匹配或字符分散均算匹配）
3. 数值条件使用预计算值
4. 如有修饰词，验证修饰词是否满足
5. 综合关键词和修饰词给出结论

输出JSON：
{{"matched":true或false,"reason":"简述理由"}}"""
        return prompt

    else:
        # ── Strict CoT (qwen2.5:3b): full guardrails, pre-injected keywords ──
        prompt = f"""你是病历筛选判断器。按以下步骤逐步推理，最后输出JSON。

条件：{sq}{kw_hint}

字段值：
{judge_summary[:1500]}
"""
        if hints:
            prompt += f"""
预计算值（已自动计算，直接使用）：
{hints}
"""
        prompt += mod_section
        prompt += """
## 推理步骤（必须按顺序执行）

第1步 确认关键词：确认上方给出的核心关键词。如果没有预提取的关键词，则从条件中去掉修饰词（的/患者/病人/了/有/开了/服用了）提取核心关键词。

第2步 扫描字段值：优先看orderName(药名)、diagnoseName(诊断名)等名称字段，这些是判断的主要依据。
   - 优先匹配：先检查orderName/diagnoseName字段，名称匹配即可判定
   - 连续匹配：关键词作为子串出现在字段值中 → 匹配
   - 字符分散：关键词拆开的字都在字段值中出现 → 也算匹配（如"背痛"→"背部疼痛"）
   - 数值条件：只能用预计算值或日期字段做计算，禁止从文本描述推断数值
   - 严禁推测：不要从剂型(颗粒剂/注射剂等)、用法、剂量等字段推测是否包含某药物，只看名称字段

第3步 验证修饰词：如有修饰词，按修饰词规则验证。否定型(没有/无)检查字段中是否存在被否定的内容；状态型检查是否有状态变化证据。

第4步 逐条判断：每个候选条目写"匹配"或"不匹配"及一句话理由。

第5步 结论：输出JSON。修饰词不满足时matched必须为false。

输出格式：
第1步 关键词：XXX
第2步 候选：...
第3步 修饰词：...
第4步 判断：...
第5步 JSON：{"matched":true或false,"reason":"简述理由"}"""
        return prompt


def build_modifier_prompt(profile: ModelProfile, modifiers: list,
                          mod_text: str) -> str:
    """Build the modifier verification prompt."""

    mod_word = modifiers[0] if modifiers else ""

    if profile.thinking == "native":
        # Direct question, internal reasoning
        return f"""判断患者的治疗/恢复状态是否匹配修饰词"{mod_word}"。在reasoning字段简述推理。

修饰词含义：
- "治好"/"好转"/"改善" → 症状减轻或消失
- "加重"/"恶化" → 症状变严重
- "复发" → 曾经好转后又出现
- "不退" → 症状持续未缓解
- "没有好转"/"未缓解" → 症状仍在

字段值：
{mod_text[:800]}

输出JSON：
{{"reasoning":"简述推理","satisfied":true或false,"reason":"一句话"}}"""

    elif profile.prompt_style == "relaxed":
        return f"""判断患者的治疗/恢复状态是否匹配修饰词"{mod_word}"。

修饰词含义参考：
- "治好"/"好转"/"改善"/"缓解"/"痊愈" → 症状减轻或消失
- "加重"/"恶化"/"进展" → 症状变严重
- "复发" → 曾经好转后又出现
- "不退"/"持续" → 症状一直存在未缓解
- "没有好转"/"未缓解" → 症状仍在，未改善

字段值：
{mod_text[:800]}

按以下步骤推理：
第1步：理解修饰词"{mod_word}"的含义
第2步：检查字段值中是否有描述治疗结果/症状变化的文本
第3步：判断是否匹配

输出JSON：
{{"satisfied":true或false,"reason":"简述判断依据"}}"""

    else:
        # Strict: same as current
        return f"""判断患者的治疗/恢复状态是否匹配修饰词"{mod_word}"。

修饰词含义参考：
- "治好"/"好转"/"改善"/"缓解"/"痊愈" → 症状减轻或消失
- "加重"/"恶化"/"进展" → 症状变严重
- "复发" → 曾经好转后又出现
- "不退"/"持续" → 症状一直存在未缓解
- "没有好转"/"未缓解" → 症状仍在，未改善

字段值：
{mod_text[:800]}

按以下步骤推理：
第1步：理解修饰词"{modifiers[0]}"的含义
第2步：检查字段值中是否有描述治疗结果/症状变化的文本
第3步：判断是否匹配

输出JSON：
{{"satisfied":true或false,"reason":"简述判断依据"}}"""


def build_decompose_prompt(profile: ModelProfile, condition: str,
                           sec_catalog: dict) -> str:
    """Build the semantic decomposition prompt."""
    import json as _json
    sec_json = _json.dumps(sec_catalog, ensure_ascii=False, indent=2)

    if profile.thinking == "native":
        return f"""将医学查询拆解为核心概念+语义修饰词。在reasoning字段简述推理。

查询：{condition}

可用病历章节：
{sec_json}

重要：修饰词仅指状态变化词（治好/好转/加重/恶化/复发/缓解/痊愈/没有好转/未缓解/不退/不见好），不包括动作动词（注射/服用/开了/做了/检查了/进行了/服用了/输液了）。动作动词只表示查询方式，不代表患者状态变化。
例："注射了葡萄糖" → keyword="葡萄糖", modifiers=[]（"注射"是动作动词，不是修饰词）
例："背痛治好的患者" → keyword="背痛", modifiers=["治好"]（"治好"是状态变化）

输出JSON：
{{"reasoning":"简述","keyword":"核心医学概念","modifiers":["修饰词1"],"extra_sections":["章节名1"]}}
注：无修饰词时modifiers和extra_sections为空数组。extra_sections的章节名必须从可用章节列表中选择。"""

    elif profile.prompt_style == "relaxed":
        return f"""将医学查询拆解为核心概念+语义修饰词。输出JSON。

查询：{condition}

可用病历章节及用途：
{sec_json}

规则：
- keyword: 提纯核心医学概念。动作动词（注射/服用/开了/做了/检查了）只是查询方式，不属于修饰词，应从keyword中剥离但不放入modifiers。例："注射了葡萄糖" → keyword="葡萄糖", modifiers=[]
- modifiers: 仅提取状态/结果描述词（治好/好转/加重/恶化/复发/缓解/痊愈/没有好转/未缓解/不退）。查询本身提到的动作动词不算修饰词
- extra_sections: 根据modifiers推断验证章节，章节名必须从可用列表中选择
- modifiers不为空时extra_sections也必须不为空

只输出JSON：
{{"keyword":"核心医学概念","modifiers":[],"extra_sections":[]}}"""

    else:
        # Strict: same as current
        return f"""将医学查询拆解为核心概念 + 语义修饰词。输出JSON。

查询：{condition}

可用病历章节及用途：
{sec_json}

输出格式：
{{
  "keyword": "背痛",
  "modifiers": ["治好"],
  "extra_sections": ["出院情况", "诊疗经过"]
}}
注：modifiers不为空时extra_sections也必须不为空。
如果没有修饰词：
{{"keyword":"高血压","modifiers":[],"extra_sections":[]}}

规则：
- keyword: 提纯后的核心医学概念。⚠️ 动作动词（注射/服用/开了/服用了/做了/检查了/进行了/输液了）只是查询方式，不是modifiers！剥离后丢弃，不要放入modifiers。例："注射了葡萄糖"→keyword="葡萄糖", modifiers=[]
- modifiers: ⚠️ 这是必填字段！仅提取状态/结果描述词。动作动词不算修饰词！没有修饰词才填空数组
  否定前缀必须保留！"没有好转"→modifiers=["没有好转"]，"未缓解"→modifiers=["未缓解"]
  修饰词严格从查询原文中提取，不要自作主张用近义词替换！查询写"好转"就填"好转"，不要改成"治好"
  只提取以下类型：治好/好转/加重/恶化/复发/缓解/痊愈/没有好转/未缓解/不见好/不退
- extra_sections: 根据modifiers推断需要查哪些章节来验证。没有modifiers则空数组
- ⚠️ extra_sections中的章节名必须从上方可用章节列表中选择，禁止编造

只输出JSON："""


def build_service_router_prompt(profile: ModelProfile, condition: str,
                                menu_lines: list) -> str:
    """Build the service routing prompt."""

    menu = "\n".join(menu_lines)

    if profile.thinking == "native":
        return f"""选择与查询直接相关的服务。只输出所选服务ID的JSON数组。

查询：{condition}

可用服务：
{menu}

匹配指南：
- diagnosis-query：查询明确问疾病/诊断 → 选
- drug-interaction：查询明确问药物/医嘱/处方 → 选
- encounter-info：查询明确问住院天数/日期/科室/病区 → 选
- lab-results：查询明确问化验/检验指标 → 选
- imaging-query：查询明确提到CT/MR/X线/超声/影像 → 选

只选明确相关的，不要选可能相关的。纯数值计算无医学实体→输出[]。
输出JSON数组：["service-id1"]或[]"""

    # relaxed and strict use the same for now (service router is less sensitive)
    return f"""查询：{condition}

从下方服务列表中选择与查询直接相关的服务。只输出所选服务ID的JSON数组，无关的服务不要输出。

可用服务：
{menu}

匹配指南（严格按以下规则，不要泛化）：
- diagnosis-query：查询明确问某种疾病/诊断是否存在、患者得了什么病、诊断名称 → 选
- drug-interaction：查询明确问用了什么药、药物名称、医嘱、处方 → 选
- encounter-info：查询明确涉及住院天数、出院日期、入院日期、就诊科室、病区、出院时间 → 选
- lab-results：查询明确问化验结果、检验指标、血常规、生化等 → 选
- imaging-query：查询明确提到CT/MR/X线/超声/影像/放射 → 选

重要：
- 只选与查询内容直接相关的服务。例如查询仅问"是否存在烧伤"，只需 diagnosis-query，不需要 encounter-info
- 不要因为"可能相关"就选，必须"明确相关"才选
- 触发词示例仅供参考，不是完整匹配列表
- 纯数值计算+无医学实体 → 输出 []
- 多个明确相关的领域 → 输出多个ID

只输出JSON数组，不要其他内容。
输出："""


# ═══════════════════════════════════════════════════════════════
# Query Understanding — 合并4阶段为1次LLM调用
# 替代: analyze_query + router.route + _decompose_semantic + match_services
# ═══════════════════════════════════════════════════════════════

def build_query_understanding_prompt(profile: ModelProfile, condition: str,
                                     doc_catalog: dict, skills_menu: list,
                                     retry_feedback: str = "") -> str:
    """Build a unified query understanding prompt.

    Catalog is presented as readable text (not JSON) so the LLM can scan and pick
    like reading a menu — much easier for smaller models.
    """
    # ── Build readable document catalog (text, not JSON) ──
    doc_lines = []
    for doc_name, doc_info in doc_catalog.items():
        purpose = doc_info.get("purpose", "")
        used_for = doc_info.get("used_for", [])
        doc_lines.append(f"### {doc_name}")
        doc_lines.append(f"用途：{purpose}")
        if used_for:
            doc_lines.append(f"场景：{'、'.join(used_for)}")
        doc_lines.append("章节：")
        sections = doc_info.get("sections", {})
        for sec_name, sec_purpose in sections.items():
            doc_lines.append(f"  - {sec_name}：{sec_purpose}")
        doc_lines.append("")
    docs_text = "\n".join(doc_lines)

    # ── Build readable service menu (text, not JSON) ──
    svc_lines = []
    for svc in skills_menu:
        sid = svc.get("id", "")
        name = svc.get("name", sid)
        desc = svc.get("desc", "")
        triggers = svc.get("triggers", "")
        returns = svc.get("returns", "")
        svc_lines.append(f"### {sid} — {name}")
        svc_lines.append(f"描述：{desc}")
        if triggers:
            svc_lines.append(f"触发词：{triggers}")
        if returns:
            svc_lines.append(f"返回字段：{returns}")
        svc_lines.append("")
    svc_text = "\n".join(svc_lines)
    correction_text = ""
    if retry_feedback:
        correction_text = (
            "\n## 上一次输出需要修正\n"
            f"{retry_feedback}\n"
            "必须重新分析原句并补齐这些字段，不要照抄上一次结果。\n"
        )

    if profile.thinking == "native":
        return f"""分析医学查询。阅读下方的病历文档目录和外部服务菜单，判断查询类型并选择路由目标。

查询：{condition}
{correction_text}

## 病历文档及章节
{docs_text}

## 外部服务
{svc_text}

## 任务
1. type判断: simple(单条件) / compound(含并且/且/和/或连接词) / temporal(含"X天前/X天后"等时间偏移)
2. negated: 是否含否定语义(false/true)
3. connector: compound时"and"或"or"，否则null
4. entity: 医学实体本体，必须去除"有/存在/患有/诊断为/诊断有/有诊断有/的患者"等功能词。例如"有诊断有胃息肉"→"胃息肉"
5. entity_type: diagnosis/drug/lab/imaging/procedure/demographic/duration/outcome/unknown 之一
6. predicate: exists/diagnosed/used/performed/high/low/abnormal/normal/compare/outcome/unknown 之一
7. keyword: 等于entity；如果是年龄/住院天数等结构条件，填"年龄"/"住院天数"
8. modifiers: 否定短语或状态变化词，无则空数组[]
9. is_numeric: 是否为数值比较(false/true)
10. target_docs: 阅读上方各文档的"用途"和章节说明，选择最相关的文档名列表
11. target_sections: 在选定文档的章节中选择相关章节，章节名必须逐字复制
12. target_skills: 阅读上方各服务的"描述"和"触发词"，选择相关服务id列表，无则[]
13. 语义归一规则："有X/患有X/存在X/诊断为X/诊断有X/有诊断有X"都是诊断存在类，entity=X，entity_type=diagnosis，target_skills必须包含diagnosis-query
14. domain: 条件所属通用领域，如demographic/encounter/diagnosis/symptom/clinical_sign/medication/laboratory/procedure/document_semantic/clinical_concept
15. temporal: 时间约束对象；无则null。有则输出scope、event、relation、duration、unit、selection、raw。event使用通用事件语义，不得填文档名
16. assertion: 断言对象，输出present、certainty(confirmed/suspected)、subject(patient/family)、temporal_context(current/history)
17. quantifier: 次数或序列约束；无则null。有则输出mode(at_least/more_than/exact/consecutive/first/last/any)、count、unit
18. depends_on: 依赖的事件写成["event:事件名"]，无依赖为[]；attributes保存剂量、途径、部位、程度等未被固定字段表达的属性

输出JSON：
{{"reasoning":"简述推理","type":"simple","negated":false,"connector":null,"conditions":[{{"text":"子条件原文","entity":"医学实体","entity_type":"diagnosis","domain":"diagnosis","predicate":"exists","keyword":"核心概念","temporal":null,"assertion":{{"present":true,"certainty":"confirmed","subject":"patient","temporal_context":"current"}},"quantifier":null,"depends_on":[],"attributes":{{}},"modifiers":[],"is_numeric":false,"target_docs":["文档名"],"target_sections":["章节名"],"target_skills":["skill-id"]}}]}}"""
    else:
        return f"""分析医学查询。阅读下方的病历文档目录和外部服务菜单，判断类型并选择路由。

查询：{condition}
{correction_text}

## 病历文档及章节
{docs_text}

## 外部服务
{svc_text}

## 要求
1. type: simple(单条件) / compound(含并且/且/和/或) / temporal(含X天前/X天后等时间偏移)
2. negated: 含否定语义为true
3. connector: compound时and/or，其他null
4. entity: 医学实体本体，去除"有/存在/患有/诊断为/诊断有/有诊断有/的患者"等功能词。例："有诊断有胃息肉"→"胃息肉"
5. entity_type: diagnosis/drug/lab/imaging/procedure/demographic/duration/outcome/unknown 之一
6. predicate: exists/diagnosed/used/performed/high/low/abnormal/normal/compare/outcome/unknown 之一
7. keyword: 等于entity；结构条件填"年龄"/"住院天数"等主体
8. modifiers: 否定/状态词数组，无则[]
9. is_numeric: 数值比较为true
10. target_docs: 阅读上方文档用途选择，文档名逐字复制
11. target_sections: 阅读章节说明选择，章节名逐字复制
12. target_skills: 阅读服务描述和触发词选择服务id，无则[]
13. "有X/患有X/存在X/诊断为X/诊断有X/有诊断有X"都是诊断存在类，entity=X，entity_type=diagnosis，target_skills包含diagnosis-query
14. domain: 通用领域，如demographic/encounter/diagnosis/symptom/clinical_sign/medication/laboratory/procedure/document_semantic/clinical_concept
15. temporal: 无时间约束为null；否则输出scope/event/relation/duration/unit/selection/raw，event不得填文档名
16. assertion: 输出present/certainty/subject/temporal_context
17. quantifier: 无次数约束为null；否则输出mode/count/unit
18. depends_on: 事件依赖格式为["event:事件名"]，无则[]；attributes保存剂量、途径、部位、程度等扩展属性
19. ⚠️ simple类型conditions数组只有1个元素

只输出JSON，conditions每项必须包含domain、temporal、assertion、quantifier、depends_on、attributes："""
