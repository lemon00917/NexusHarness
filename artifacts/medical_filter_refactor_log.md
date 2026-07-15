# 病历筛选改造记录

## 目标架构

1. Normalize 层：处理符号、错别字、中文数字、科学计数法和单位写法。
2. LLM Understand 层：由 qwen2.5:3b 输出初版结构，不能直接作为最终执行依据。
3. Scope Guard 层：在理解结果修复后判断请求是否属于病历筛选能力范围。
4. Deterministic IR Validator 层：集中校验和修复 LLM/兜底分析结果，防止时间、数值、单位、否定、连接关系丢失或被改写。
5. Executor 层：按稳定 IR 查接口、算时间窗、比数值/单位、生成证据链。

## 计划改造项

- [x] 建立统一 Query IR Validator 模块。
- [x] 建立 Scope Guard 并在 Scheduler/Executor 前接入主流程。
- [x] 将“LLM 改坏子条件数字/单位时回退原文字面量”的逻辑集中到 Validator。
- [x] 将“单条件时间问题不能丢时间上下文”的逻辑集中到 Validator。
- [x] 将“术后24小时内这类时间数字不当作普通数值条件”的判断集中到 Validator。
- [x] 将 `web/app.py` 中的大型结构增强逻辑继续迁移到 Validator 或独立结构解析模块。
- [ ] 让 Validator 输出更标准的 IR 字段：`domain/entity/temporal/numeric/negation`。
- [ ] 让 Executor 完全按 IR 执行，减少路由和执行阶段的重复猜测。
- [x] 增加核心固定离线回归用例集，覆盖用药、症状、检验、住院时长、术前时间窗和复合 AND。

## 本轮已改

- 新增 `microharness/medical/query_ir_validator.py`。
- `web/app.py` 中以下函数改为调用 Validator：
  - `_has_explicit_value_predicate`
  - `_is_executable_numeric_condition`
  - `_preserve_literal_clause_texts`
  - `_preserve_single_temporal_condition`
- 保持原有函数名作为兼容包装，降低本轮改动范围和回归风险。

## 本轮验证

- `python -m py_compile web\app.py microharness\medical\query_ir_validator.py microharness\medical\query_ir.py` 通过。
- Validator 轻量用例通过：
  - 输入分析结果只有 `开了维生素`，原句为 `术后24小时内开了维生素的患者`，修复后条件文本保留完整原句，核心词仍为 `维生素`。
  - 输入分析结果把 `1.5x10⁹/L` 改成 `1.5x10¹¹/L`，修复后回退为原句子条件 `术前48小时内中性粒细胞数>1.5x10⁹/L`。
  - `术后24小时内开了维生素的患者` 不再被标记为可执行数值比较。
  - `术前48小时内中性粒细胞数>1.5x10⁹/L` 仍会被标记为检验数值比较。

## 当前效果

- `术后24小时内开了维生素的患者` 不再被执行成单独的 `开了维生素`，会保留完整时间上下文。
- `术前48小时内中性粒细胞数>1.5x10⁹/L` 会保留检验数值比较。
- `术后24小时内` 这类时间范围数字不会在 QueryIR 中显示成普通数值比较。

## 待验证重点

- 复合条件：
  - `术前24小时使用过阿司匹林且术前48小时内中性粒细胞数＞1.5×10⁹/L的患者`
  - `手术3天后开了维生素且诊断为背痛的患者`
- 单条件时间：
  - `术后24小时内开了维生素的患者`
  - `手术1天后开了维生素的患者`
  - `出院后7天开了阿司匹林的患者`
- 检验候选完整性：
  - 同名检验多条记录时，每条都要展示检测时间、数值判断和时间窗判断。

## 验证命令

```powershell
python -m py_compile web\app.py microharness\medical\query_ir_validator.py microharness\medical\query_ir.py
```

## 第二轮已改：结构解析迁移

- 新增 `microharness/medical/query_structure.py`。
- 将以下结构层职责从 `web/app.py` 迁出：
  - 复合条件兜底拆分。
  - 年龄比较、病史年限等结构条件补全。
  - 非执行型上下文片段过滤。
  - 结构修复统一流水线 `repair_analysis_structure`。
- `web/app.py` 主流程改为调用 `_repair_analysis_structure(...)`，避免重复串联多段修复函数。
- 修复结构抽取重叠片段：`40岁以上有10年以上高血压病史，住院期间白细胞计数指标偏高` 不再额外残留 `10年以上高血压病史`，而是稳定拆成：
  - `住院期间白细胞计数指标偏高`
  - `年龄>=40岁`
  - `高血压病史>=10年`

## 第二轮验证

- `python -m py_compile web\app.py microharness\medical\query_structure.py microharness\medical\query_ir_validator.py microharness\medical\semantic_rules.py` 通过。
- 轻量结构用例通过：
  - `术后24小时内开了维生素的患者` 保留完整时间上下文。
  - `40岁以上并且背痛，住院期间血红蛋白指标偏高` 拆成 `年龄>=40岁`、`背痛`、`住院期间血红蛋白指标偏高`。
  - `术前24小时使用过阿司匹林且术前48小时内中性粒细胞数>1.5x10⁹/L的患者` 拆成两个可执行子条件，并保留原始科学计数法。

## 第三轮已改：核心离线回归与 Scope Guard

- 新增 `microharness/medical/scope_guard.py`：
  - 拒绝空/模糊筛选条件、天气/写代码等无关请求、治疗建议等非筛选医学请求。
  - 对 CT/MRI 原图、病理切片、基因测序和院外设备随访返回“数据源不支持”。
  - 保守允许未知疾病/症状和短条件，防止 Scope Guard 变成新的实体白名单。
- `web/app.py` 在 `understand_query`、结构修复和 `build_query_ir` 后执行 Scope Guard，拒绝请求不进入 Router、Scheduler、DB 或外部服务。
- `microharness/medical/query_ir.py` 仅在显式数值比较谓词存在时调用 `parse_numeric_comparison`：
  - `术前48小时内中性粒细胞数偏低` 的 `numeric_comparison` 为 `None`。
  - `术前48小时内中性粒细胞数＞1.5×10⁹/L` 仍解析为 `operator=>`、`threshold=1500000000.0`。
- 新增测试：
  - `tests/test_scope_guard.py`：15 条边界判定与响应契约用例，加 1 条主流程提前返回集成用例。
  - `tests/test_medical_query_offline_regression.py`：5 条交接问题固定回归。

## 第三轮验证

- `python -m py_compile web/app.py microharness/medical/scope_guard.py microharness/medical/query_ir.py` 通过。
- `python -m pytest tests/test_scope_guard.py tests/test_medical_query_offline_regression.py -q`：21 条通过。
- 相关既有回归：24 条通过。
- 当前是离线回归，未验证真实 Ollama、DB 和外部服务的端到端匹配结果。

## 第四轮已改：患者隔离与多维 Query IR

- 新增 `microharness/medical/patient_query.py`：
  - 集中构造患者和就诊范围条件。
  - 缺少全部患者身份时抛出 `MissingPatientIdentityError`，数据库调用前停止。
  - 严格查询可同时使用本地和全局身份；兼容重查最多回退到本地挂号号和就诊号，不允许无患者范围查询。
  - 当前 IRIS REST 客户端仍使用 SQL 字符串，因此统一进行 SQL 字面量转义；后续客户端支持 bind 参数后再迁移为真正参数化查询。
- `web/app.py` 的 `_query_db(...)` 接入统一患者范围构造器，无身份时返回 `error_code=MISSING_PATIENT_IDENTITY`。
- 扩展 `microharness/medical/query_ir.py`：
  - 新增 `TemporalIR`、`AssertionIR`、`QuantifierIR`。
  - `ConditionIR` 新增条件 ID、领域、时间约束、断言、量词、依赖和扩展属性。
  - 支持手术/入院/出院事件窗、住院期间、最近时间段、否定/疑似/既往/家族主体和次数表达的通用解析。
  - 支持上游 LLM 直接提供结构化维度；结构化字段优先，本地通用解析用于补全。
  - `40岁以上`、`住院天数小于5天` 等显式比较保留为数值谓词；时间窗中的 `24小时` 不会误判为普通数值条件。
- 扩展查询理解契约：
  - `microharness/ollama/prompt_adapter.py` 要求 LLM 输出 `domain`、`temporal`、`assertion`、`quantifier`、`depends_on` 和 `attributes`。
  - `microharness/agent/query_understanding.py` 保留并校验这些字段；错误类型回退为空结构，由 Query IR 通用解析补全。
- 本轮未增加任何具体疾病、药品、检验项目或病历文档名称分支。

## 第四轮验证

- `python -m py_compile microharness/medical/query_ir.py microharness/agent/query_understanding.py microharness/ollama/prompt_adapter.py web/app.py` 通过。
- 新增 `tests/test_medical_patient_query.py`，覆盖患者隔离和数据库访问边界。
- 新增 `tests/test_query_ir_dimensions.py`，覆盖多领域、事件依赖、断言、量词、LLM 提示词和结构字段校验。
- 目标测试：22 项通过。
- 完整离线测试：93 项通过。
- 真实 `qwen2.5:3b` 理解层验证：模型原始输出曾将复合条件误报为单条件并误标领域；经过主流程 `repair_analysis_structure` 后恢复为用药和检验两个条件，两个术前事件窗及服务路由正确。该场景已加入离线回归。
- 已知非阻断警告：FastAPI `on_event` 弃用；当前环境无权限创建 `.pytest_cache`。
- 未完成：真实 Ollama、DB 和外部接口端到端验证；该项随 EvidencePlan 和统一证据模型继续验收。

## 第五轮已改：IR 质量门禁与歧义短路

- 新增 `microharness/medical/query_ir_quality.py`：
  - 对复合结构、条件文本、事件锚点、数值比较主体、量词次数和领域置信进行统一评估。
  - 阻断问题与非阻断告警分离，输出稳定原因码和质量评分。
  - 无法可靠执行时返回 `error_code=AMBIGUOUS_QUERY_IR`、`判断状态=无法判断`、`可判定=False`，不把语义不完整误判成“不符合”。
- 查询理解增加一次结构化重试：
  - 首次 IR 不完整时，将具体原因码和问题文本反馈给 LLM。
  - 重试输出重新经过结构修复、Query IR 构建和质量评估。
  - 重试后仍不完整时，在 Scheduler、Executor、数据库和外部服务之前停止。
- 时间解析与门禁增强：
  - 支持“手术3天后”“入院2天前”“出院7天后”等事件、时长、方向后置表达。
  - 支持无明确时长的“术前”“术后”“入院前后”“出院前后”事件关系。
  - 门禁校验真实结构化锚点，不能用一个缺少事件和方向的空壳 `temporal` 对象绕过。
  - 保留自定义事件扩展能力，不为药品、疾病、检验项、文档或章节增加白名单。
- API 正常结果新增 `IR质量`；歧义结果包含 `ir_quality` 诊断信息，原有主要返回字段保持兼容。

## 第五轮验证

- `python -m py_compile microharness/medical/query_ir.py microharness/medical/query_ir_quality.py microharness/agent/query_understanding.py microharness/ollama/prompt_adapter.py web/app.py` 通过。
- 新增 `tests/test_query_ir_quality.py`，覆盖完整复合条件、缺少锚点、最近时间窗、住院期间、无时长事件关系、事件后置表达、自定义事件、开放临床概念、歧义响应、重试提示和主流程短路。
- 核心目标回归：34 项通过。
- 完整离线测试：104 项通过。
- 已知非阻断警告：FastAPI `on_event` 弃用；当前环境无权限创建 `.pytest_cache`。
- 阶段 1 验收完成；下一阶段为元数据驱动动态 `EvidencePlan`。

## 第六轮已改：EvidencePlan 兼容契约

- 新增 `microharness/medical/evidence_plan.py`：
  - `EvidencePlan` 按条件统一记录结构化服务、病历文档和章节候选。
  - 文档、章节和服务通过名称、元数据别名及唯一模糊匹配进行解析。
  - 未知名称不再丢弃，保留 `requested_name`、`resolution_status`、原因和 `UNRESOLVED_*` 诊断。
  - 没有任何候选来源时显式记录 `NO_EVIDENCE_SOURCE_PLANNED`，便于后续触发补充规划而不是无声执行。
- `web/app.py` 在 IR 质量门禁通过后构建 `EvidencePlan`：
  - 输出计划条件数、候选来源数和未解析数日志。
  - Scheduler 与主流程正常响应新增 `证据计划` 字段。
  - 当前计划为兼容只读层，尚未替换旧 Scheduler/Executor，避免本轮改变既有判定结果。
- 新增 `tests/test_evidence_plan.py`，覆盖多来源计划、元数据别名、未知文档保留和无来源诊断。

## 第六轮验证

- `python -m py_compile microharness/medical/evidence_plan.py web/app.py` 通过。
- EvidencePlan 目标测试：4 项通过。
- 完整离线测试：108 项通过。
- `git diff --check` 通过。
- 阶段 2 状态为“进行中”；下一切片是基于标准角色元数据补充候选来源，并让 Scheduler 通过适配器消费计划。

## 第七轮已改：角色召回与 EvidencePlan 执行链接入

- `EvidencePlan` 升级为 `1.1`：
  - 条件领域映射到通用证据角色，不包含具体疾病、药品、检验项目或患者分支。
  - 支持 `document_role`、`section_role`、`evidence_roles`、`evidence_types` 及服务 `semantic` 元数据。
  - 在显式路由结果之外补充角色匹配的服务、文档和章节，并按来源去重。
- 新增 `apply_evidence_plan_to_analysis(...)` 兼容适配器：
  - 只把已解析来源注入现有 `target_skills`、`target_docs`、`target_sections` 和逐文档 `targets`。
  - 未解析名称保留在计划诊断中，不作为新增可执行来源注入。
  - Scheduler 和统一执行管线继续使用原输入契约，但实际消费 EvidencePlan 规划结果。
- 为诊断、检验、用药和就诊 Skill 补充通用领域及 `evidence_types` 元数据。
- `web/app.py` 增加计划注入日志，输出本次进入现有执行链的来源数量。

## 第七轮验证

- `python -m py_compile microharness/medical/evidence_plan.py web/app.py` 通过。
- EvidencePlan 目标测试扩展为 6 项，覆盖角色召回、逐文档章节和仅注入已解析来源。
- 阶段 2 收尾时 `tests/` 目录完整回归：119 项通过。
- 真实查询“住院天数小于5天并且烧伤”生成 2 个条件、8 个候选来源并全部注入；实际调用就诊和诊断接口，同时规划诊断证据文档章节。
- 阶段 2 完成；下一阶段为统一 `EvidenceItem` 和 `ConditionResult`。

## 第八轮已改：统一证据模型兼容契约

- 扩展现有 `microharness/medical/evidence.py`，没有另建重复证据模块：
  - 新增三态 `EvidenceStatus`：`MATCHED`、`NOT_MATCHED`、`UNKNOWN`。
  - 新增 `DataQuality`：`COMPLETE`、`PARTIAL`、`MISSING`、`SOURCE_ERROR`。
  - 新增稳定 `ReasonCode`，覆盖命中、无匹配、时间窗外、缺少事件时间、数值不满足、数据源不可用和证据不足。
  - 统一 `EvidenceItem` 包含条件 ID、来源类型、来源名称、记录 ID、文档、章节、实体、原文、事件时间、数值、单位、异常标志、参考范围、状态、原因码和数据质量。
  - 统一 `ConditionResult` 汇总单条件三态结论和证据列表。
- 新增旧结果兼容适配器：
  - 读取现有 `matched`、`reason`、`files` 和结构化字段生成统一模型。
  - 检验及结构化时间规则返回的 `候选记录` 会逐条生成 `EvidenceItem`，不再只保留来源级汇总文本。
  - 数据源失败、请求超时或未取得关键数据转换为 `UNKNOWN`，不会转换成 `NOT_MATCHED`。
  - 保留旧 `证据明细`、`files`、`matched`、`reason` 和置信字段，不改变已有调用方行为。
- Scheduler 与统一执行管线的最终响应均接入统一模型，新增：
  - 条件级 `evidence_items`、`condition_result`。
  - 患者级 `condition_results`、`evidence_model_version=1.0`。
  - 响应级 `evidence_model_version=1.0`。

## 第八轮验证

- `python -m py_compile microharness/medical/evidence.py microharness/medical/evidence_plan.py web/app.py` 通过。
- 新增 `tests/test_evidence_model.py`，覆盖结构化字段保留、数据源失败三态、时间窗原因码、Query IR 条件 ID 对齐和旧字段兼容。
- `python -m pytest tests -q`：125 项通过。
- 已知非阻断警告：FastAPI `on_event` 弃用；当前环境无权限创建 `.pytest_cache`。
- 阶段 3 状态为进行中；下一切片为各数据源原生输出统一字段，而不是继续依赖旧结果文本适配。

## 第九轮已改：用药原生统一证据与业务错误识别

- 新增 `microharness/medical/medication_rules.py`：
  - 使用 Skill `semantic.fields` 的通用角色映射记录，不按具体药品写死。
  - 支持医嘱项、处方号、开立时间、执行/给药时间、状态、剂量、频次、途径、剂型、疗程和备注等角色。
  - 根据查询谓词区分开立、使用和停用；具体谓词所采用的时间角色与状态要求由 Skill 的 `predicate_policies` 配置。
  - 当前项目按业务约定将“使用过”解释为“开立时间命中目标时间窗且医嘱状态描述有效”，逐条返回医嘱项、开立时间、状态描述、时间窗与状态判断。
- `skills/drug-interaction/SKILL.md` 增加通用 `semantic.fields`、`evidence_capabilities` 和 `predicate_policies`；当前项目读取 `ordStatusDesc`，状态有效/无效描述位于元数据，不使用项目特定状态编码，Python 规则层不包含具体药品或状态分支。
- 外部服务 binding 透传 `semantic`，统一执行链在检验结构化规则旁接入用药规则；旧 `matched/reason/fields/候选记录` 契约保持不变。
- `microharness/medical/evidence.py` 扩展用药候选字段适配，并修复条件级三态冒泡：没有命中时，只要证据存在 `UNKNOWN`，条件级不会被旧布尔逻辑降成 `NOT_MATCHED`。
- 补充状态约束适配：记录在时间窗内但状态缺失或未知时，统一证据保持 `UNKNOWN`，不能被“是否在时间窗=true”误转为 `MATCHED`。
- `microharness/services/http_client.py` 增加通用 HTTP 200 业务失败识别：`data=null` 搭配错误消息或 `success=false` 转为接口不可用，不针对“请先登录系统”写死。
- 真实接口核对：`MES0005` 当前返回 `msg=请先登录系统,data=None`，已正确转成 `service_error=true`，不会再整理成一条伪医嘱记录。

## 第九轮验证

- 新增 `tests/test_medication_rules.py`，覆盖任意药品、窗内/窗外、缺时间锚点、缺记录时间、仅开立证据、执行时间命中、无匹配记录、接口失败和统一 EvidenceItem 适配。
- 新增 `tests/test_http_client_business_error.py`，覆盖 HTTP 200 业务失败、显式失败和正常空结果。
- `python -m py_compile microharness/medical/medication_rules.py microharness/medical/evidence.py microharness/services/http_client.py web/app.py` 通过。
- `python -m pytest tests -q`：138 项通过，3 个既有非阻断警告（FastAPI `on_event` 弃用、pytest 缓存目录权限）。
- `git diff --check` 通过。

## 第十轮：检验条件误路由纠偏

- 修复小模型把“住院期间高密度脂蛋白胆固醇指标偏低”“术前48小时内白细胞>1.5×10⁹/L”错误路由到 `diagnosis-query` 的问题。
- 检验条件确认后会移除冲突的诊断/用药服务、诊断文档和诊断章节，并统一修复为 `entity_type=lab`、`domain=laboratory` 及对应谓词。
- 检验识别不依赖具体检验项目写死：明确检验表达可直接确认；数值/异常表达必须与服务元数据中的检验概念或检验单位组合，不能由“大于、小于、异常”等通用词单独触发。
- 年龄比较优先归入 `demographic`，住院时长比较优先归入 `encounter`；即使 LLM 错标为 `lab-results`，也会主动清除冲突路由。
- 保留疾病诊断边界，例如“患有白细胞减少症”仍使用 `diagnosis-query`，不会因疾病名包含检验概念而转为检验。
- 本轮没有修改内部病历文档元数据，外部病历元数据接口无需同步。

## 第十轮验证

- 新增 `tests/test_semantic_lab_routing.py`，覆盖错误诊断路由纠偏、检验数值比较、明确疾病反例、年龄误标检验和住院时长误标检验。
- 针对性回归：37 项通过。
- `python -m pytest tests -q`：158 项通过，3 个既有非阻断警告（FastAPI `on_event` 弃用、pytest 缓存目录权限）。
- `python -m py_compile microharness/medical/semantic_rules.py` 和 `git diff --check` 通过。

## 第十一轮：接口有界并发与可见排队

- 确认当前部署为单 Uvicorn worker，原病历筛选线程池固定为 4 个工作线程，但线程池等待队列无上限。
- 新增通用 FIFO 并发协调器，不改变病历理解、路由、取证和判断逻辑：
  - `MEDICAL_QUERY_MAX_CONCURRENCY`：同时执行上限，默认 4。
  - `MEDICAL_QUERY_MAX_QUEUE`：等待队列上限，默认 20。
  - `MEDICAL_QUERY_QUEUE_TIMEOUT_SECONDS`：排队超时，默认 300 秒。
- 请求只有取得执行名额后才提交线程池，避免任务继续进入 `ThreadPoolExecutor` 的无界内部队列。
- 队列满返回 HTTP `429`，排队超时返回 HTTP `503`，均携带明确错误和 `request_id`。
- 新增 `/api/medical/query/status` 状态接口；筛选页面按请求 ID 轮询并显示“排队中，前方 N 个”或“筛选中”。
- 客户端断开后，已经在线程中运行的工作不会被错误地视为停止；执行名额等后台工作真正结束后才释放。
- 本轮未修改内部或外部病历元数据，无需同步元数据接口。

## 第十一轮验证

- 新增 `tests/test_medical_query_concurrency.py`，覆盖 FIFO、队列位置、队列满、排队超时、重复请求 ID 和环境变量配置。
- FastAPI 模拟并发验证：并发 1、队列 1 时，第二个请求处于 `waiting/position=1`，第三个请求返回 `429`，前两个请求保持原成功响应。
- `python -m pytest tests -q -p no:cacheprovider`：163 项通过，2 个既有 FastAPI `on_event` 弃用警告。
- `python -m py_compile microharness/medical/query_concurrency.py web/app.py`、页面脚本 `node --check` 和 `git diff --check` 通过。

## 第十二轮：第一步性能改造

- 不改变条件拆分、Query IR、EvidencePlan、文档路由、时间窗、异常标志、AND/OR 或证据判断语义。
- `_run_medical_query` 新增全链路阶段计时，响应新增 `timings`：
  - `normalization_ms`
  - `metadata_ms`
  - `understanding_ms`
  - `evidence_plan_ms`
  - `structured_services_ms`
  - `condition_execution_ms`
  - `evidence_enrichment_ms`
  - `explanation_polish_ms`
  - `total_ms`
- 修正原 `total_ms` 起点过晚且未覆盖最终解释润色的问题；当前 `total_ms` 从请求内部处理起点计算到响应清洗完成。
- 诊断、检验、就诊等独立结构化服务使用有界并行，默认最大并发为 3，可通过 `MEDICAL_SERVICE_MAX_CONCURRENCY` 调整。
- 同一请求内按服务 ID 使用共享 Future 去重；初始路由和后续回退路由命中同一服务时，不会重复请求外部接口。
- 病历数据库连通性检查改为请求级复用：并行子条件最多执行一次 `db.test()`，不引入跨请求缓存。
- 本轮没有修改内部或外部病历元数据，无需同步病历元数据接口。

## 第十二轮验证

- `python -m py_compile web/app.py` 通过。
- 新增请求级数据库健康检查兼容测试；`python -m pytest tests -q`：164 项通过。
- 指定入参实测中，诊断、检验、就诊三个接口同时发起，结构化服务阶段约 1.5 秒；此前三个接口串行累计约 2.9 秒。
- 新响应 `total_ms=20257`，外部墙钟 `20265ms`，误差约 8ms，已覆盖最终解释润色约 4.2 秒。
- 本次实测时 IRIS `http://124.222.57.198:52773/HDCV2DEV` 连通性检测失败，因此 20.3 秒的整体耗时不能与此前数据库正常时的 38.3 秒直接比较；数据库恢复后必须使用同一入参再次验证整体收益和结果一致性。

## 第十三轮：按可执行证据源收紧 fallback

- fallback 的触发条件由“没有目标文档”改为“既没有可执行病历文档，也没有可执行结构化服务”。
- 路由现在统一覆盖四种组合：文档+服务联合取证、纯文档取证、纯结构化服务取证、无可执行来源时 fallback。
- `target_docs` 存在且 `target_skills` 为空时继续执行纯文档取证；`target_docs` 为空但结构化服务有效时直接执行服务，不再额外调用路由 LLM。
- 可执行文档同时校验实时文档目录和内部表映射；可执行服务校验服务注册信息和 URL，不依赖具体药品、检验项、诊断或项目编码。
- 未知文档名不再在理解阶段静默删除：保留到 Query IR/EvidencePlan 形成未解析诊断，执行前再过滤，避免未知文档进入数据库查询。
- 查到有效来源但返回 0 条不会触发扩大路由；fallback 只由执行前的来源可用性决定。
- 本轮没有修改内部或外部病历元数据，无需同步病历元数据接口。

## 第十三轮验证

- 新增可执行来源决策矩阵测试，覆盖文档+服务、纯文档、纯服务、无来源和未知文档。
- 新增未知文档保留测试，确认未知名称进入未解析诊断且不会作为可执行文档。
- `python -m py_compile web/app.py microharness/agent/query_understanding.py` 通过。
- `python -m pytest tests -q`：171 项通过。
- 指定复合入参真实链路验证：检验条件日志明确输出“纯结构化服务路由，跳过fallback”，最终仅使用 `lab-results`；本次总耗时约 13.9 秒，前一轮同入参约 20.3 秒。两次数据库均不可用，结果数据不作为准确性对比，仅验证路由调用和阶段耗时。
- `git diff --check` 通过；仅保留既有 FastAPI `on_event` 弃用和 pytest 缓存目录权限警告。

## 第十四轮：就诊相对时间锚点与业务错误隔离

- 修复“出院后N天内”相对时间窗被“住院期间必须存在入院时间”提前拦截的问题。
- 入院相对时间只要求 `encStartDate/encStartTime`，出院相对时间只要求 `encEndDate/encEndTime`；“住院期间”仍保持必须有入院时间的原规则。
- 该修复基于时间角色和 encounter-info 字段映射，不针对血红蛋白、背痛或具体查询写死。
- 扩展 HTTP 200 业务失败识别：响应体 `code>=400` 时转成数据源失败，数据库异常消息不再被整理成诊断、检验或就诊记录参与判断。
- 本轮没有修改内部或外部病历元数据，无需同步病历元数据接口。

## 第十四轮验证

- 新增时间窗测试，覆盖出院后、出院前、入院后、缺少出院时间及住院期间缺少入院时间。
- 新增业务状态码测试，覆盖 `code=500` 失败和 `code=200` 正常响应。
- `python -m pytest tests -q`：178 项通过。
- 指定入参真实链路耗时约 14.8 秒；三项条件拆分及路由正确，但本地 `MES0002/MES0004/MES0023` 均返回 `code=500`，后端数据库报 `Communication link failure: Connection reset by peer`，病历数据库也未取得数据，因此最终严格返回“无法判断”。
- 复测确认 SQL/数据库错误不再作为候选医学证据进入判断。

## 第十五轮：诊断、就诊和病历文档原生证据

- 诊断服务按记录前缀聚合原始字段，保留诊断名称、类型、日期、时间和字段路径。
- 就诊服务保留就诊类型、科室、状态、病区以及入院、出院日期时间。
- 病历文档按实际选中的章节生成证据，保留文档名、模板、章节原文、XPath 或数据库字段路径。
- 结构化服务合并结果继续携带 `service_id`，证据来源类型由服务元数据和语义角色决定，不依赖具体疾病、药品、检验项或文档名称。
- 诊断和文档只有在目标实体能够与原始记录确定对应时才生成具体命中证据；无法对应时回退旧证据表示，不把全部候选记录冒充为命中记录。
- 已明确字段角色的数值条件允许保留计算操作数字段，不要求字段值重复条件实体；例如日期差、时长等派生值仍能追溯到原始字段。该规则依据 IR 的 `is_numeric` 和目标字段角色，不依赖具体条件或文档名称。
- 数据源失败不会生成临床原生记录，仍统一表示为 `UNKNOWN/SOURCE_UNAVAILABLE`。
- `_structured_evidence_records` 仅用于内部转换，生成 `EvidenceItem` 后立即从兼容 API 结果中移除。
- 本轮没有新增外部 API、数据库或 LLM 调用，没有修改内部或外部病历元数据。

## 第十五轮验证

- `python -m py_compile microharness/medical/evidence.py microharness/services/http_client.py web/app.py` 通过。
- `python -m pytest tests/test_evidence_model.py -q`：12 项通过。
- `python -m pytest tests -q`：185 项通过。
- 新增诊断、就诊、文档章节、无关记录隔离、服务失败隔离和内部字段不泄漏回归。
- 仅保留既有 FastAPI `on_event` 弃用和 pytest 缓存目录权限警告。

## 第十六轮：判定说明与证据状态一致性

- 修复“总体符合，但条件说明只写未取得接口数据、当前无法判断”的矛盾展示。
- 条件规则解释根据三态结论选择证据：符合优先命中证据，不符合优先有效排除证据，无法判断优先不可用或不足证据。
- LLM 润色增加条件级和总体级状态一致性门禁，不能使用数据源失败文案替换已存在的支持证据。
- 只调整展示解释，不修改 `matched`、`判断状态`、置信度、路由、时间窗或证据执行结果。
- 本轮没有修改内部或外部病历元数据，无需同步病历元数据接口。

## 第十六轮验证

- `python -m pytest tests/test_reason_polisher.py -q`：13 项通过。
- `python -m pytest tests -q`：191 项通过。
- 离线复验“住院天数小于5天并且背痛”：总体和两个子条件均引用命中的住院日期、住院天数及胸背部疼痛文档证据，不再显示“符合但无法判断”。

## 第十七轮：派生日期差说明一致性

- 日期差预计算不再按字段出现顺序拼接公式，而是通用地按实际时间先后生成“较晚时间字段 - 较早时间字段”。
- LLM 润色增加减法操作数顺序校验；确定性证据中的日期差公式不得被逆转。
- 新增字段正序、倒序以及润色逆转操作数回归测试。
- 本轮没有修改内部或外部病历元数据，无需同步病历元数据接口。

## 第十七轮验证

- `python -m pytest tests/test_medical_patient_query.py tests/test_reason_polisher.py -q`：29 项通过。
- `python -m pytest tests -q`：191 项通过。
- `git diff --check` 通过。
