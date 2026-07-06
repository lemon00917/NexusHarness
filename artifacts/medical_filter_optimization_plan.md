# AI病历智能筛选优化计划

## 目标

把当前能力从“LLM理解 + 局部补丁”收敛为可落地的四层架构：

1. Normalize 层：只做通用语言/符号/数字/单位归一化。
2. LLM Understand 层：只产出候选 IR，不直接决定最终判定。
3. Scope Guard 层：判断问题是否属于病历筛选能力范围，不相关时给出明确提示。
4. Deterministic IR Validator 层：校验原句语义约束是否被完整保留。
5. Executor 层：结构化查接口、算时间窗、比数值、聚合证据。

核心原则：业务实体不写死在代码里；药品、检验、诊断、字段、服务路由尽量来自 skill metadata、service catalog、medical catalog 或可维护配置。

## 已完成

- Normalize 层已增加比较签名保护：
  - 防止 LLM 把 `>` 改成 `<`。
  - 防止 LLM 丢失 `1.5x10^9/L` 这类科学计数阈值。
  - 支持 `住院天数》三天`、`年龄大于四十岁`、`不低于100g/L` 等通用比较语法。
- 增加回归测试：
  - `tests/test_query_normalizer.py`
  - 验证比较方向不可翻转、科学计数阈值不可丢、等价比较符可接受。
- Deterministic IR Validator 已扩展：
  - 显式 AND/OR 子条件数量不一致时，按原句重建子条件。
  - 单条件时间表达被 LLM 丢失时，按原句回填。
  - 校验数值比较、比较方向、阈值、单位、否定词是否保留。
  - Validator 统一接入 `query_structure.repair_analysis_structure`，后续新增校验只维护一处。
- 增加 IR Validator 回归测试：
  - `tests/test_query_ir_validator.py`
  - 覆盖复合条件数量修复、时间表达保留、科学计数单位保留。
- 路由映射已开始 metadata 化：
  - `query_router.py` 支持读取 `configs/medical_routing_map.json` 或 `configs/disease_section_map.json`。
  - 支持 `replace_default=false` 增量覆盖默认映射。
  - 支持 `replace_default=true` 项目配置全量替换默认映射。
- 展示解释层已增加防漂移校验：
  - LLM 润色不能把 `>阈值` 的不满足解释成证据不支持的“低于阈值”。
  - 找到候选记录但不在时间窗时，不能润色成“没有使用过/未使用过”。
  - 不通过校验时回退规则解释，不使用 LLM 改写文本。
  - 规则回退解释会优先汇总候选记录明细，说明哪条记录、记录时间/检测时间、结果值、是否在时间窗内、数值如何判断。
- 前端候选记录展示已按证据源类型区分：
  - 检验候选记录展示项目、检测时间、结果、数值判断。
  - 用药候选记录展示药物名称、开立时间、用药途径、剂量/频次、时间窗。
  - 诊断候选记录展示诊断名称、诊断类型、诊断时间、时间窗。
  - 未识别类型走通用候选记录表格。

## 当前盘点结论

### 可以保留在代码里的通用规则

- 符号归一化：`＞/》/≥/≤/×/X`。
- 中文数字解析：`三天`、`四十岁`。
- 比较语法：`大于/小于/不低于/不超过/以上/以下`。
- 复合条件连接：`且/并且/或者`。
- 时间窗口语法：`术前48小时内`、`入院后1天`、`住院期间`。
- 数值比较执行：左值、比较符、阈值、单位兼容性。
- 数据缺失处理：返回“不可判定/数据不足”，不强行判断。

### 需要继续迁移或收敛的硬编码风险

- `microharness/medical/query_router.py`
  - `DISEASE_SECTION_MAP` 含大量疾病/症状到文档章节的业务映射，应迁入 `configs/medical_catalog.json` 或独立 metadata。
- `microharness/medical/semantic_rules.py`
  - `LAB_QUERY_RE` 含部分检验相关词，应逐步改为读取 `lab-results` skill triggers/semantic metadata。
  - `OUTCOME_PATTERNS` 是通用转归语义，可保留，但后续应集中配置，避免散落。
  - `NON_DIAGNOSIS_KEYWORDS` 属于类型排除词，可保留或迁配置。
- `web/app.py`
  - `_primary_service_for_condition` 已优先使用 `entity_type/semantic_class/target_skills`，但仍有服务 ID 分支，需要进一步从 service metadata 抽象。
  - `_prune_primary_service_route` 目前重点照顾 lab-results，应泛化为“结构化主证据服务 + 时间锚点辅助证据”规则。
- `microharness/ollama/prompt_adapter.py`
  - prompt 中有服务选择示例和实体例子，允许作为提示示例，但不能成为执行依据。

### 需要新增的适用范围提示

- 在 LLM Understand 之后、Executor 之前增加 Scope Guard：
  - 如果问题不是“筛选患者/判断患者是否符合某个病历条件”，直接返回提示，不调用病历接口。
  - 如果问题和病历数据源无关，例如闲聊、天气、写代码、医学常识问答、治疗建议、药品说明书解释等，提示用户输入病历筛选条件。
  - 如果问题属于医疗但当前数据源无法支持，例如要求影像原图诊断、基因测序结论、院外长期随访等，返回“当前数据源不支持/需要补充数据源”。
  - 如果问题语义过空，例如“这个患者怎么样”“查一下有没有问题”，提示用户补充明确条件。
- 返回格式建议：
  - `判断状态: 无法执行`
  - `可判定: false`
  - `用户解释: 当前问题不属于病历智能筛选条件，请输入例如“术前48小时使用过某药且检验指标>阈值的患者”这类条件。`
  - 不生成“符合/不符合”，避免误导。

## 优化执行顺序

### P0：先稳住正确性

- 完成 Normalize 层保护。
- 增加 Scope Guard：
  - 非病历筛选问题不进入 Executor。
  - 无法映射到数据源的问题返回能力范围提示。
  - 模糊问题要求用户补充明确筛选条件。
- 补 IR Validator：
  - 子条件数量和连接词一致。
  - 时间表达不能丢。
  - 数值比较不能丢。
  - 比较方向不能反。
  - 单位不能丢。
  - 否定词不能丢。
  - 事件锚点必须能绑定数据源，否则不可判定。

### P1：去硬编码

- 把 `DISEASE_SECTION_MAP` 迁到 `configs/medical_catalog.json` 或 metadata。
- 把检验/用药/诊断服务触发语义统一读取 skill metadata。
- 把服务主证据选择抽象成通用策略：
  - `entity_type`
  - `semantic_class`
  - `target_services`
  - `service.metadata.evidence_role`
  - `service.metadata.temporal_fields`

### P2：Executor 统一

- 统一时间锚点模型：
  - 手术、入院、出院、住院期间都走 `anchor + window + record_time_field`。
- 统一结构化接口筛选：
  - 用药医嘱按药名和开立时间。
  - 检验按项目、结果、单位、检测时间。
  - 诊断按诊断名、诊断类型、诊断时间。
- 统一证据角色：
  - 主证据
  - 时间范围依据
  - 候选证据
  - 排除证据
  - 不可判定证据

### P3：测试与落地

- 固化真实回归问题集：
  - 住院天数比较。
  - 术前/术后用药。
  - 检验数值比较。
  - 检验偏高/偏低/异常。
  - 诊断/病史/症状。
  - AND/OR 复合条件。
  - 数据缺失/接口失败。
- 每次修改都跑：
  - Normalize 单测。
  - IR Validator 单测。
  - Executor 结构化接口单测。
  - 真实问题端到端回归。
- 灰度落地：
  - 保留原始条件、规范条件、IR、服务调用、证据链、最终判断。
  - 失败样本回流到测试集。

## 下一步

进入 P1 去硬编码：

1. 增加 Scope Guard，先拦截非病历筛选/无关/无法映射数据源的问题。
2. 把当前 `query_router.py` 里的默认 `DISEASE_SECTION_MAP` 逐步迁出到 `configs/medical_routing_map.json`。
3. 把 `semantic_rules.py` 中检验/用药/诊断服务触发语义统一改为读取 skill metadata。
4. 把 `web/app.py` 的主证据服务选择泛化为 service metadata 策略。
