"""
Scheduler Prompts
=================
Complexity judgment and plan generation prompts.

All prompts describe SYSTEM CAPABILITIES and PATTERNS — not hardcoded word lists.
The LLM learns to recognize patterns from descriptions and examples,
generalizing to novel phrasings (e.g., "开刀后", "拔管后", "插管前").

Pattern descriptions use semantic roles, not specific words:
- "时间参照事件" = any medical event used as time origin
- "医疗行为" = any action whose timing is being constrained
"""

from microharness.ollama.model_profile import ModelProfile


def build_complexity_judge_prompt(profile: ModelProfile, condition: str) -> str:
    """Build the complexity judgment prompt.

    Describes what the existing pipeline CAN and CANNOT do.
    The LLM reasons generically based on these capability boundaries.
    No hardcoded word lists — just pattern descriptions with illustrative examples.
    """

    base = f"""判断以下查询是否超出了现有管线的处理能力。仅输出JSON。

查询：{condition}

现有管线已具备以下能力（这些都不需要调度层）：
- 拆分AND/OR复合条件为独立子条件，各自并行判断后布尔组合
- 识别并验证语义修饰词（如状态变化、转归描述）
- 数值比较直接用计算引擎处理
- 调用单个外部数据源查询（诊断、用药、就诊信息等）
- 以上能力的任意组合（如AND复合+修饰词+数值）

现有管线无法处理（以下才需要调度层）：
1. 跨数据源的时间约束 — 查询中隐含了需要从两个不同来源取时间再算差值的模式。
   典型模式：一个"时间参照事件" + 一个时间窗口 + 一个"医疗行为"
   参照事件是某个医疗节点（如手术、出院、入院等），医疗行为需要与此节点的发生时间做比较。
   例如："手术后3天内开了阿司匹林"、"出院后5天复诊"、"入院前已诊断糖尿病"、"拔管后2小时发热"
2. 事件先后顺序 — 两个事件存在时序关系，需要分别查询再比较时间
3. 查询参数依赖 — 需要从一个数据源的结果推导另一个数据源的查询条件

判断方法：如果查询中同时出现了一个"时间参照事件"和另一个"医疗行为"，且它们之间有时间关系（前/后/内），则为COMPLEX。否则为SIMPLE。

只输出JSON：
{{"complexity":"SIMPLE或COMPLEX","reasoning":"基于上述能力边界的判断理由"}}"""

    return base


def build_plan_generation_prompt(
    profile: ModelProfile,
    condition: str,
    actions_desc: str = "",
    docs_desc: str = "",
    svc_desc: str = "",
    analysis_context: str = "",
) -> str:
    """Build the execution plan generation prompt for COMPLEX queries.

    Includes:
    - Available actions, documents, services (catalog-driven, not hardcoded)
    - Pre-analysis context (from query_analyzer) to help the LLM
    - Pattern-matching guidance for document/service selection

    The LLM selects documents and services from the catalog based on
    semantic relevance — no hardcoded mappings in code.
    """

    base = f"""你是查询执行计划生成器。为以下查询生成执行步骤计划。

查询：{condition}
{analysis_context}
{actions_desc}

{docs_desc}

{svc_desc}

计划生成规则：

1. **选择文档和章节**：根据时间参照事件的语义，从"可用文档"列表中选择语义最相关的文档。
   - 参照事件是某类医疗操作 → 选择描述该操作的文档，取其日期章节
   - 参照事件是出入院 → 选择出入院相关文档，取日期时间章节
   - 选最相关的1个文档，取其最相关的1个日期章节

2. **选择外部服务**：根据医疗行为的语义，从"可用服务"列表中选择语义最相关的服务。
   - 医疗行为涉及用药/给药 → 选用药相关服务
   - 医疗行为涉及诊断 → 选诊断相关服务
   - 医疗行为涉及就诊/住院 → 选就诊相关服务
   - 参考服务的"触发词"和"描述"来做选择

3. **确定时间关系**：
   - "X后Y天内" / "X后Y天"（无"后"修饰Y）→ relation: "within"
   - "X后Y天后" / "X后Y天之后" → relation: "after"
   - "X前Y小时" / "X前Y天" → relation: "before"
   - 无明确时间数值（如"X前已Z"）→ relation: "before", value: 8760（表示任意时间前）

4. **计划结构**（6步标准模板）：
   - Step 1: legacy_pipeline → 对医疗条件部分做语义判断
   - Step 2: query_db → 查询锚点文档的日期
   - Step 3: extract_date → 从DB结果提取日期（depends_on: [2]）
   - Step 4: call_service → 调外部API取医疗数据（与step 2-3并行）
   - Step 5: temporal_filter → 按时间窗口筛选（depends_on: [3, 4]）
   - Step 6: llm_judge 或 boolean_combine → 最终判断（depends_on: [1, 5]）
     如果只需要判断时间筛选结果 → 用 llm_judge
     如果需要组合医疗判断+时间筛选 → 用 boolean_combine(logic: "and")

5. **格式要求**：
   - 动作名必须用英文（如上所列）
   - output_var 用英文名（medical_judgment/db_results/anchor_date/api_results/filtered_data/final_result）
   - depends_on 引用前置步骤的 step_id（数字）
   - 文档名和章节名必须从"可用文档"列表中逐字复制，禁止编造
   - 服务ID必须从"可用服务"列表中逐字复制
   - 所有字符串值用双引号
   - value 和 step_id 用数字类型，不用字符串

只输出JSON（填入从原文和目录中提取的具体值，不要保留任何占位符）：
{{"reasoning":"一句话说明为什么选这些文档和服务","plan":[
{{"step_id":1,"action":"legacy_pipeline","params":{{"condition":"医疗条件文本"}},"output_var":"medical_judgment"}},
{{"step_id":2,"action":"query_db","params":{{"condition":"关键词","documents":["从可用文档中选"],"sections":["从该文档章节中选"]}},"output_var":"db_results"}},
{{"step_id":3,"action":"extract_date","params":{{"source_var":"db_results","field":"日期章节名"}},"depends_on":[2],"output_var":"anchor_date"}},
{{"step_id":4,"action":"call_service","params":{{"service":"从可用服务中选","keyword":"关键词"}},"output_var":"api_results"}},
{{"step_id":5,"action":"temporal_filter","params":{{"reference_var":"anchor_date","target_var":"api_results","relation":"within或before或after","value":数字,"unit":"hours或days"}},"depends_on":[3,4],"output_var":"filtered_data"}},
{{"step_id":6,"action":"llm_judge","params":{{"condition":"原始完整条件","data_var":"filtered_data"}},"depends_on":[1,5],"output_var":"final_result"}}
]}}"""
    return base
