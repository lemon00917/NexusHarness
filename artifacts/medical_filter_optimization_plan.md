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

- Scope Guard 已完成：
  - 新增 `microharness/medical/scope_guard.py`，保守拦截模糊请求、无关请求、非筛选医学请求和明确缺少数据源的请求。
  - 接入点位于 Understand + IR 结构修复之后、Scheduler/Executor 之前。
  - 拒绝响应保留 `results`、`reason`、`用户解释` 和 `scope_guard`，前端可直接展示。
  - 未知疾病/症状和暂未识别的路由默认允许，避免误杀“背痛”、“烧伤”等短条件。
- 核心离线回归已固化：
  - 新增 `tests/test_medical_query_offline_regression.py`，固定 5 条交接问题的拆分、AND 关系、服务路由和数值语义。
  - 新增 `tests/test_scope_guard.py`，覆盖边界判定、拒绝响应契约和主流程提前返回。
  - Query IR 只在出现显式数值比较谓词时生成 `numeric_comparison`，“术前48小时内...偏低”不再误解为 `<48小时`。

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

### 2026-07-08 追加盘点

本轮围绕“严格判定、证据链完整、避免写死”补充了以下结论，后续继续按 metadata/IR/Executor 分层收敛。

#### 本轮已处理

- 用户解释层改为证据驱动：
  - 用药、检验、诊断、文档章节候选记录都尽量展示关键字段。
  - 候选记录在时间窗外时，解释必须说明哪条记录、记录时间/检测时间、目标时间窗和不符合原因，不能简单说“没有”。
- LLM 路由异常不再静默丢弃：
  - LLM 返回未知文档/章节时，保留 `llm_invalid_targets`、`route_warnings`、`route_repairs`、`raw_response`。
  - 真实无效文档不用于查库，但必须进入证据链诊断，避免“为什么没查到”不可追溯。
- API 异常兜底：
  - `/api/medical/query` 对未捕获异常返回“无法判断/不可判定”，不直接 500 给前端。
  - debug trace 仅在 `MEDICAL_QUERY_DEBUG=1` 时返回。
- 复合条件拆分修复：
  - `40岁以上并且背痛，住院期间血红蛋白指标异常` 可稳定拆为 3 个 AND 子条件。
- 检验规则修复：
  - `术前48小时内中性粒细胞数偏低` 中的 `48小时内` 不再被误当作检验阈值。
  - `术前48小时内中性粒细胞数>1.5x10^9/L` 仍能保留真正数值阈值。
  - 参考范围 `2-7.7` 不再被解析成 `2` 和 `-7.7`。
  - 检验解释补充项目、检测时间、结果、单位、异常标志、参考范围、判断结论。
- Step1 展示修复：
  - `术前48小时内中性粒细胞数偏低` 的结构摘要不再显示成 `判断=≤48小时`，而是 `限定=术前48小时内`、`判断=偏低`。
- 裸疾病/症状路由修复：
  - `烧伤` 这类裸疾病/症状词，不应因文档目录未直接命中而提前放弃服务匹配。
  - 服务匹配到 `diagnosis-query` 时，路由继续返回结构化诊断服务。
- 病历章节证据改为 metadata 驱动：
  - 在 `configs/medical_catalog.json` 章节上增加 `evidence_roles`。
  - 疾病/症状存在类查询通过 `evidence_roles` 找候选章节，而不是在代码里写“烧伤查入院记录”。
  - 当前角色包括 `disease_symptom_evidence`、`diagnosis_evidence`、`symptom_evidence`。
- 文档到章节映射保留：
  - Executor 构建 DB 查询时保留 `targets` 映射，避免把所有章节拍平成全局列表后套给每个文档。
  - 例如出院记录只查自己的 `入院情况/入院诊断/出院诊断`，不再混入入院记录的 `主诉/现病史`。
- 配置加载兼容：
  - `medical_catalog.json` 用 `utf-8-sig` 读取，避免 Windows 工具写入 BOM 后目录加载失败。

#### 本轮暴露、下次需要继续优化

- `evidence_roles` 需要升级为正式元数据协议：
  - 当前是字符串数组，下一步建议改为对象数组，包含 `role/scope/priority`。
  - 示例：
    ```json
    {
      "role": "diagnosis_evidence",
      "scope": ["general", "discharge", "inpatient"],
      "priority": "primary"
    }
    ```
- 角色建议收敛为：
  - `diagnosis_evidence`：明确诊断结论，如入院诊断、出院诊断、初步诊断。
  - `symptom_evidence`：症状/主诉/发病经过，如主诉、现病史、入院情况。
  - `history_evidence`：既往病史/长期存在，如既往史。
  - `course_evidence`：住院期间病情变化或补充发现，如诊疗经过、住院病程。
  - `procedure_evidence`：手术/操作证据，如手术名称、手术经过。
  - `anchor_evidence`：时间锚点，如入院日期、出院日期、手术日期。
- 场景建议：
  - `general`：无明确时间锚点的“XX患者/存在XX”。
  - `admission`：入院时、入院前、入院诊断。
  - `discharge`：出院诊断、最终诊断、出院时。
  - `inpatient`：住院期间、本次住院、住院过程中发现。
  - `preop/postop`：术前/术后。
  - `outpatient`：门诊/急诊。
- 优先级建议：
  - `primary`：可直接支持结论，如结构化诊断、诊断章节。
  - `supporting`：辅助证据，如主诉、现病史、入院情况。
  - `context`：上下文证据，不单独判符合。
  - `anchor`：只做时间锚点。
- 疾病/症状类路由需要按 `role + scope + priority` 选证据：
  - `诊断类查询`：优先 `diagnosis_evidence + primary`。
  - `症状类查询`：查 `diagnosis_evidence primary` + `symptom_evidence supporting`。
  - `入院前/入院时`：只取 `admission/general`。
  - `出院/最终诊断`：只取 `discharge/general`。
  - `术前/术后`：取 `preop/postop`，并绑定手术日期锚点。
  - `住院期间发现`：取 `inpatient`。
- 服务与病历文档证据需要分层解释：
  - 结构化服务是主证据时，病历正文可作为辅助/补充证据。
  - 如果结构化诊断未命中，但病历正文数据源不可用，应返回“无法判断/证据不足”，不是直接“不符合”。
- 现有 `DISEASE_SECTION_MAP` 仍是硬编码风险：
  - 已有 `evidence_roles` 方向，但默认疾病映射还在代码里。
  - 后续应逐步迁移到 medical catalog 或 routing metadata。
- 服务匹配仍依赖部分 LLM：
  - 对 1-2 字短词，如“烧伤”，service metadata 确认可命中 `diagnosis-query`，但应尽量减少不稳定 LLM 兜底。
  - 可在 skill metadata 增加 `semantic.entity_types`、`evidence_roles`、`default_for` 等字段。
- DB 数据源不可用时的证据链还需细化：
  - 当前能说明未取得哪些文档/章节。
  - 后续应区分“数据库连接失败”“查无该文档”“章节字段未映射”“文档存在但字段为空”。

#### 本轮新增/应固化的回归问题

- `住院天数小于5天并且烧伤的患者`
  - `住院天数<5` 应由 `encounter-info` 判定。
  - `烧伤` 应同时路由到 `diagnosis-query` 和 medical catalog 中标记为疾病/症状证据的文档章节。
  - 文档章节必须保持文档到章节映射，不能全局混用。
- `术前24小时使用过阿司匹林且术前48小时内中性粒细胞数偏低的患者`
  - 阿司匹林候选记录在术前 24 小时窗外，应不符合。
  - 中性粒细胞数 `4.00×10^9/L`、异常标志 `无`、参考范围 `2-7.7`，应判“不偏低”。
  - 不允许把 `48小时内` 当成 `≤48` 检验阈值。
- `术前24小时使用过阿司匹林且术前48小时内中性粒细胞数＞1.5×10⁹/L的患者`
  - 阿司匹林不符合。
  - 中性粒细胞数 `4×10^9 > 1.5×10^9` 符合。
  - AND 总体不符合。
- `40岁以上并且背痛，住院期间血红蛋白指标异常`
  - 应拆成年龄、背痛、住院期间血红蛋白异常 3 个 AND 子条件。
  - 血红蛋白若检测时间不在住院期间，应严格不符合或无法判断，不能忽略时间窗。

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

### 已完成的适用范围提示

- 已在 LLM Understand 之后、Executor 之前增加 Scope Guard：
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
- [x] 增加 Scope Guard：
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

- [x] 已固化 5 条核心问题的离线结构/IR 回归和 Scope Guard 回归。
- [ ] 待在可用的 Ollama、DB 和外部服务环境补真实数据端到端回归。

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

P0 中的核心离线回归和 Scope Guard 已完成。接下来进入 P1 去硬编码：

1. 把当前 `query_router.py` 里的默认 `DISEASE_SECTION_MAP` 逐步迁出到 `configs/medical_routing_map.json`。
2. 把 `semantic_rules.py` 中检验/用药/诊断服务触发语义统一改为读取 skill metadata。
3. 把 `web/app.py` 的主证据服务选择泛化为 service metadata 策略。
4. 在真实环境补 Scope Guard 拒绝率监控和 5 条固定问题的端到端验证。
