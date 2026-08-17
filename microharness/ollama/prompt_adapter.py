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


def build_semantic_candidate_prompt(sq: str, query_entity: str,
                                    judge_summary: str) -> str:
    """Ask the model to extract one verbatim medical entity candidate."""
    return f"""查询条件：{sq}
查询实体：{query_entity}
病历原文：
{judge_summary[:1500]}

你只做医学实体原文抽取，不判断同义关系，也不判断患者是否符合。
从病历原文中逐字复制与查询实体可能相关的最短完整医学实体短语，最多返回5个不同候选。

要求：
1. matched_entity 必须是病历原文中的连续子串。
2. matched_entity 只保留疾病、症状、体征、检验异常、药物或操作名称；不要包含章节名、患者、日期、时长、标点或完整句子。
3. 不得把原文中不存在的查询词填入 matched_entity。
4. evidence_span 必须是包含 matched_entity 的连续原文短句。
5. 本阶段只负责召回候选；相关但不等价的表达也可以抽取，后续会独立审核。
6. 原文即使是否定、疑似、既往或家属情况，只要存在相关医学实体，也要抽取实体；但 matched_entity 不得包含“否认、无、未见、考虑、疑似、既往、患者”等断言词或主体词，这些词必须保留在 evidence_span 中供后续程序判断。
7. 对体征、测量或检验结果，必须保留会改变医学含义的结果状态词，例如“偏高、偏低、升高、降低、阳性、阴性、异常、正常”；不得只抽取项目名称。患者是否符合仍由后续阶段判断。
8. 尽量覆盖原文中所有不同表达；同一实体若分别出现在肯定、否定、疑似、既往或不同时间语境中，应分别保留对应 evidence_span。
9. candidates 按原文出现顺序排列，完全相同的 matched_entity 与 evidence_span 不要重复。

示例：查询“腹痛”，原文“主诉: 患者上腹部疼痛3天。”，matched_entity 应为“上腹部疼痛”。
否定示例：查询“咳嗽”，原文“现病史: 患者否认咳嗽。”，matched_entity 应为“咳嗽”，evidence_span 应保留“患者否认咳嗽”。

只输出一个 JSON 对象。新格式必须包含 search_complete 和 candidates；同时保留首个候选的旧字段以兼容旧调用方：
- search_complete: 是否已经完整扫描给定原文，JSON 布尔值 true 或 false
- candidates: JSON 数组，最多5项；每项包含 matched_entity 和 evidence_span；未找到时为空数组
- candidates 中每个 matched_entity 与 evidence_span 都必须满足上述逐字原文约束
- 顶层 matched_entity 和 evidence_span 复制 candidates 第一项；没有候选时为空字符串

字段约束如下，不要复制类型占位符：
- candidate_found: JSON 布尔值 true 或 false
- matched_entity: 原文医学实体字符串，未找到时为空字符串
- evidence_span: 连续原文短句，未找到时为空字符串"""


def build_semantic_candidate_retry_prompt(
    sq: str,
    query_entity: str,
    judge_summary: str,
    previous_reason: str = "",
) -> str:
    """Retry candidate extraction when the first response omitted completeness."""
    return f"""查询条件：{sq}
查询实体：{query_entity}
完整病历原文：
{judge_summary[:1500]}

首次抽取未通过完整性校验：{previous_reason or '未明确确认搜索完整'}。
请重新逐字扫描上面给出的全部原文。你只抽取原文候选，不判断患者是否符合，不判断同义关系。

严格要求：
1. 只输出一个 JSON 对象，不要输出解释、Markdown 或代码块。
2. 程序已经确认“完整病历原文”全部传入且没有被截断；你必须逐字扫描它，并将 search_complete 返回为 JSON 布尔值 true。即使没有任何候选，也必须返回 true，不能因为 candidates 为空而返回 false。
3. 必须包含 candidate_found 和 candidates。candidates 最多5项；未找到候选时返回空数组并将 candidate_found 设为 false。
4. 每项只包含 matched_entity 和 evidence_span。两者必须是原文中的连续子串，严禁补写原文不存在的实体。
5. matched_entity 只保留最短完整医学实体；evidence_span 保留否定、疑似、既往和主体等上下文。
6. 顶层 matched_entity 和 evidence_span 复制第一项；无候选时都返回空字符串。

返回结构：
{{"search_complete":true,"candidate_found":false,"candidates":[],"matched_entity":"","evidence_span":""}}"""


def build_semantic_equivalence_prompt(query_entity: str, matched_entity: str,
                                      entity_type: str = "") -> str:
    """Ask the model to audit one-way clinical entailment only."""
    return f"""你是医学筛选的严格单向语义蕴含审核器。只比较两个医学短语，不判断患者，不补充病历外事实。

查询概念：{query_entity}
原文概念：{matched_entity}

审核目标：假设病历肯定描述“原文概念”，判断这是否足以证明“查询概念”存在。只要求原文到查询的单向蕴含，不要求查询反向证明原文。

方向定义：
- 前提 PREMISE：患者存在“原文概念”。
- 假设 HYPOTHESIS：患者存在“查询概念”。
- relation 只描述“前提到假设”的关系，禁止检查或解释“假设到前提”的反向关系。
- 方向示例仅用于理解方向：前提“左膝疼痛”可以推出假设“膝部疼痛”；反向是否成立与本题无关。

先分别标注两个短语的断言层级，只能使用以下枚举：
- DIAGNOSIS：疾病名称或明确诊断
- SYMPTOM_OR_SIGN：患者症状或临床体征
- OBSERVATION_OR_MEASUREMENT：测量值或描述性发现，单独不能作为疾病诊断
- MEDICATION：药物
- LAB_TEST：检验项目或检验结果
- PROCEDURE：操作、手术或治疗程序
- OTHER：无法归入以上类别

然后只选择一个 relation 枚举：
- SAME_CONCEPT：两个短语是同一临床概念的同义、简称、口语或规范表达。
- SOURCE_MORE_SPECIFIC：原文概念是查询概念的更具体部位、诱因、程度、时间、亚型或表现形式；患者存在原文概念时，必然也存在查询概念。
- RELATED_ONLY：两个概念相关、伴随或可能互相提示，但原文概念不能单独证明查询概念。
- SOURCE_BROADER：原文概念比查询概念更宽，不能证明更具体的查询概念。
- UNRELATED：无关。
- UNCERTAIN：无法可靠分类。

判断约束：
- 医学口语、书面语、简称和规范名称的差异，本身不代表抽象层级不同。
- 不要因为症状名称采用规范医学术语就把它标为疾病诊断；只有疾病名称或明确诊断才是 DIAGNOSIS。
- 疾病诊断不能由症状、一次观察、测量值、体征、检验异常或治疗行为直接推出。
- 部位、诱因、程度、时间或亚型等限定可以让原文更具体；只要原文仍直接证明查询概念，不应因为反向替换不成立而拒绝。
- 不得因为两个短语都属于症状或都与同一种疾病相关，就判为蕴含。
- 否定、疑似、既往、家属主体和时间范围不在本阶段判断，由后续程序结合完整原文确定。
- 上游的 entity_type 只是粗粒度路由分类，可能把疾病和症状放在同一类；不得用该分类强行区分两个短语。

relation 必须严格返回上述枚举之一，不能返回布尔值或自创标签。

只输出一个 JSON 对象，字段约束如下，不要复制类型占位符：
- query_kind: 查询概念的断言层级枚举
- source_kind: 原文概念的断言层级枚举
- relation: 单向关系枚举
- reason: 一句话说明选择该关系的医学语义依据"""


def build_semantic_symptom_relation_prompt(query_entity: str,
                                            matched_entity: str) -> str:
    """Ask the model to distinguish one symptom from a related symptom."""
    return f"""你是医学筛选的严格症状同一性审核器。只比较两个已经被识别为症状或体征的短语，不判断患者，不输出患者是否符合。

查询症状：{query_entity}
原文症状：{matched_entity}

审核目标：判断“原文症状”究竟是“查询症状”的同一表达或带限定表达，还是一个可以单独记录的不同症状。

relation 只能选择以下一个枚举：
- SAME_SYMPTOM：同一症状的同义、口语、书面语、简称或规范表达。
- SOURCE_QUALIFIED_SAME_SYMPTOM：原文仍是查询症状本身，只增加了部位、诱因、程度、时间或活动状态等限定。
- DISTINCT_SYMPTOMS：两个短语是不同的症状现象；即使经常伴随、存在因果关系、属于同一系统或同一种疾病，也不能互相证明。
- UNCERTAIN：无法可靠确定。

严格约束：
- 只判断症状本身是否相同，不要因为医学相关、常同时发生或一个可能导致另一个就判为同一症状。
- 如果临床记录中可以把两者作为两个独立症状分别询问、分别肯定或分别否定，应选择 DISTINCT_SYMPTOMS。
- 如果原文只是给同一症状增加位置、诱因、程度、时间或活动状态，应选择 SOURCE_QUALIFIED_SAME_SYMPTOM。
- 不得返回布尔值，不得自创标签，reason 与 relation 必须一致。

方向示例仅用于理解规则：
- 查询“腹痛”，原文“右下腹疼痛”属于 SOURCE_QUALIFIED_SAME_SYMPTOM。
- 查询“头晕”，原文“头痛”属于 DISTINCT_SYMPTOMS，不能因为二者可能同时出现就互相证明。

只输出一个 JSON 对象：
- relation: 症状关系枚举
- reason: 一句话说明依据"""


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
                       modifiers: list = None,
                       semantic_recall: bool = False,
                       query_entity: str = "",
                       entity_candidates: list | None = None,
                       entity_type: str = "") -> str:
    """Build the per-file judge prompt adapted to model capabilities.

    Args:
        modifiers: Optional list of modifier words (e.g. ["没有输血"], ["治好"]).
                   When provided, modifier verification is merged into this
                   single judge call instead of a separate LLM round.
        semantic_recall: Ask the model only to locate a strictly equivalent
                         entity and quote source evidence. The program still
                         performs the final medical assertion judgment.
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

    evidence_semantics_section = """
证据语义边界（必须遵守）：
- 只有患者本人、肯定性且与条件时间语境一致的记录才能作为匹配证据
- “否认/无/未见/不存在/已排除”属于否定证据，不能因为关键词出现就判定匹配
- 家属、父母、子女、配偶等非患者主体的情况不能证明患者本人存在该情况
- “考虑/疑似/可能/待排/不除外”等不确定表述不能证明确定存在；除非条件本身明确查询疑似或待排状态
- 既往或历史记录不能自动证明当前仍存在；条件明确查询既往史时除外
- 字符分散或部分字符重叠只能用于发现候选文本，不能单独作为最终匹配依据
"""

    if semantic_recall:
        return build_semantic_candidate_prompt(
            sq,
            query_entity or str(next(iter(entity_candidates or []), "")),
            judge_summary,
        )

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
        prompt += evidence_semantics_section
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
        prompt += evidence_semantics_section
        prompt += """
推理步骤：
1. 确认核心关键词
2. 扫描字段值中的候选关键词，并核对患者主体、否定、确定性和时间语境
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
        prompt += evidence_semantics_section
        prompt += """
## 推理步骤（必须按顺序执行）

第1步 确认关键词：确认上方给出的核心关键词。如果没有预提取的关键词，则从条件中去掉修饰词（的/患者/病人/了/有/开了/服用了）提取核心关键词。

第2步 扫描字段值：优先看orderName(药名)、diagnoseName(诊断名)等名称字段，这些是判断的主要依据。
   - 优先匹配：先检查orderName/diagnoseName字段，名称匹配即可判定
   - 连续匹配：关键词作为子串出现在字段值中 → 匹配
   - 字符分散：关键词拆开的字只用于定位候选片段；必须在同一局部语境中形成目标概念，并通过主体、否定和确定性检查后才能匹配
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
5. canonical_entity: entity对应的规范医学名称；无法可靠规范化时与entity相同
6. aliases: 只填写与canonical_entity严格等价的别名、缩写、全称或常见书写变体；禁止填写相关疾病、上下位概念、并发症、症状联想或检查组合，无可靠别名时[]
7. entity_confidence: 本次实体归一化置信度0到1；normalization_source固定填写"llm"
8. entity_type: diagnosis/drug/lab/imaging/procedure/demographic/duration/outcome/unknown 之一
9. predicate: exists/diagnosed/used/performed/high/low/abnormal/normal/compare/outcome/unknown 之一
10. keyword: 等于entity；如果是年龄/住院天数等结构条件，填"年龄"/"住院天数"
11. modifiers: 否定短语或状态变化词，无则空数组[]
12. is_numeric: 是否为数值比较(false/true)
13. numeric_comparison: is_numeric=true时必须输出subject、operator、threshold、unit；operator只能是>/</>=/<=/=或等价中文，非数值条件填null
14. target_docs: 阅读上方各文档的"用途"和章节说明，选择最相关的文档名列表
15. target_sections: 在选定文档的章节中选择相关章节，章节名必须逐字复制
16. target_skills: 阅读上方各服务的"描述"和"触发词"，选择相关服务id列表，无则[]
17. 语义归一规则："有X/患有X/存在X/诊断为X/诊断有X/有诊断有X"都是诊断存在类，entity=X，entity_type=diagnosis，target_skills必须包含diagnosis-query
18. domain: 条件所属通用领域，如demographic/encounter/diagnosis/symptom/clinical_sign/medication/laboratory/procedure/document_semantic/clinical_concept
19. temporal: 时间约束对象；无则null。有则输出scope、event、relation、duration、unit、selection、raw。event使用通用事件语义，不得填文档名
20. assertion: 断言对象，输出present、certainty(confirmed/suspected)、subject(patient/family)、temporal_context(current/history)
21. quantifier: 次数或序列约束；无则null。有则输出mode(any/all/at_least/more_than/exact/at_most/less_than/earliest/latest/consecutive)、count、unit
22. depends_on: 依赖的事件写成["event:事件名"]，无依赖为[]；attributes保存剂量、途径、部位、程度等未被固定字段表达的属性
23. 转归/状态条件必须在attributes中输出通用字段：outcome_state取improved/resolved/not_improved/persistent/worsened/recurred之一；outcome_phase取admission/hospitalization/discharge/post_discharge/post_treatment/postoperative/follow_up之一；只有条件明确要求“出院诊断/仍诊断”时outcome_evidence才填diagnosis，否则填state。非转归条件不要填写这些字段

输出JSON：
{{"reasoning":"简述推理","type":"simple","negated":false,"connector":null,"conditions":[{{"text":"子条件原文","entity":"医学实体","canonical_entity":"规范医学名称","aliases":["严格等价别名"],"entity_confidence":0.95,"normalization_source":"llm","entity_type":"diagnosis","domain":"diagnosis","predicate":"exists","keyword":"核心概念","temporal":null,"assertion":{{"present":true,"certainty":"confirmed","subject":"patient","temporal_context":"current"}},"quantifier":null,"depends_on":[],"attributes":{{}},"modifiers":[],"is_numeric":false,"numeric_comparison":null,"target_docs":["文档名"],"target_sections":["章节名"],"target_skills":["skill-id"]}}]}}"""
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
5. canonical_entity: entity对应的规范医学名称；无法可靠规范化时与entity相同
6. aliases: 仅限严格等价别名、缩写、全称或书写变体，禁止相关疾病、上下位概念、并发症和联想词；无可靠别名时[]
7. entity_confidence: 归一化置信度0到1；normalization_source固定为"llm"
8. entity_type: diagnosis/drug/lab/imaging/procedure/demographic/duration/outcome/unknown 之一
9. predicate: exists/diagnosed/used/performed/high/low/abnormal/normal/compare/outcome/unknown 之一
10. keyword: 等于entity；结构条件填"年龄"/"住院天数"等主体
11. modifiers: 否定/状态词数组，无则[]
12. is_numeric: 数值比较为true
13. numeric_comparison: is_numeric=true时输出subject/operator/threshold/unit，非数值条件为null
14. target_docs: 阅读上方文档用途选择，文档名逐字复制
15. target_sections: 阅读章节说明选择，章节名逐字复制
16. target_skills: 阅读服务描述和触发词选择服务id，无则[]
17. "有X/患有X/存在X/诊断为X/诊断有X/有诊断有X"都是诊断存在类，entity=X，entity_type=diagnosis，target_skills包含diagnosis-query
18. domain: 通用领域，如demographic/encounter/diagnosis/symptom/clinical_sign/medication/laboratory/procedure/document_semantic/clinical_concept
19. temporal: 无时间约束为null；否则输出scope/event/relation/duration/unit/selection/raw，event不得填文档名
20. assertion: 输出present/certainty/subject/temporal_context
21. quantifier: 无次数约束为null；否则输出mode(any/all/at_least/more_than/exact/at_most/less_than/earliest/latest/consecutive)、count、unit
22. depends_on: 事件依赖格式为["event:事件名"]，无则[]；attributes保存剂量、途径、部位、程度等扩展属性
23. 转归/状态条件在attributes中输出outcome_state(improved/resolved/not_improved/persistent/worsened/recurred)、outcome_phase(admission/hospitalization/discharge/post_discharge/post_treatment/postoperative/follow_up)和outcome_evidence(diagnosis/state)；非转归条件不要填写
24. ⚠️ simple类型conditions数组只有1个元素

只输出JSON，conditions每项必须包含canonical_entity、aliases、entity_confidence、normalization_source、domain、numeric_comparison、temporal、assertion、quantifier、depends_on、attributes："""
