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

## 第十八轮：文档局部语义门禁

- 新增 `microharness/medical/document_semantics.py`，围绕目标实体所在的局部句段判断主体、否定、确定性和历史/当前语境。
- 患者本人肯定陈述可作为命中证据；明确否认、已排除和非患者主体不能证明患者当前存在该条件。
- 疑似、考虑、待排、不除外等不确定语义返回 `UNKNOWN`；既往语境不会自动证明当前仍存在，既往后已缓解或消失不作为当前阳性证据。
- 同一文档存在多次正负提及时逐次分析，避免只按首次出现或整篇字符散落判断。
- 查询明确指定家属主体时允许使用对应主体证据；普通患者查询不把家属疾病归到患者本人。
- 文档字面纠正只有在语义门禁确认 `MATCHED` 后才能反转 LLM 漏判；`UNKNOWN` 或 `NOT_MATCHED` 不允许被关键词覆盖。
- 判断提示同步增加主体、否定、确定性和历史/当前语境约束，最终三态仍由程序事实门禁控制。
- 本轮没有针对具体疾病、药品、检验项目、文档名、章节名、服务 ID 或院方编码写分支。
- 本轮没有修改内部或外部病历元数据、外部接口配置和 API 契约。

## 第十八轮验证

- 文档语义针对性测试覆盖肯定、否认、排除、家属主体、疑似、既往、已缓解、多次提及、指定主体和字符散落场景。
- `python -m pytest tests -q`：204 项通过。
- 同一版真实金标准评测：7/7 案例通过，49/49 断言通过，无 `BLOCKED` 和基础设施失败。
- 平均耗时 22495.57ms，P50 16799.30ms，P95 52959.05ms；没有观察到基线回归，但不能据此宣称整体医学准确率或性能稳定提升。

## 第十九轮：结构化诊断确定性判断

- 新增 `microharness/medical/diagnosis_rules.py`，对可明确对应目标实体的结构化诊断候选记录执行确定性三态判断。
- 疑似、待排、考虑、不除外等不确定诊断返回 `UNKNOWN`；明确排除、否定或无效状态返回 `NOT_MATCHED`；有效确定诊断返回 `MATCHED`。
- 诊断记录结合诊断日期、诊断时间、查询条件和 Skill `temporal_semantics` 判断目标时间范围；必要时间事实不足时保持 `UNKNOWN`。
- 没有明确实体候选时不强行接管，返回 `applicable=False` 并继续使用现有 LLM 同义词语义路径。
- 诊断规则位于检验和用药确定性规则之后、通用 LLM 判断之前，执行日志使用 `[Step4-诊断规则]` 标识。
- 本轮规则基于通用字段角色和状态语义，不包含具体疾病、文档名、章节名、服务 ID、院方状态编码或项目编码分支。
- 本轮没有修改内部或外部病历元数据、外部接口配置和 API 契约。

## 第十九轮验证

- 诊断规则及相关针对性测试：35 项通过。
- `python -m pytest tests -q`：213 项通过。
- 同一版真实金标准评测：7/7 案例通过，49/49 断言通过，无 `BLOCKED` 和基础设施失败。
- 平均耗时 18333.20ms，P50 13392.66ms，P95 33229.08ms；相较当前基线下降，但样本较少且受模型和数据源状态影响，暂不宣称性能稳定提升。
- 评测报告位于忽略目录 `evaluation/medical_filter/reports/p1_diagnosis_certainty.json`，原始响应位于 `evaluation/medical_filter/responses/p1_diagnosis_certainty/`，可能包含病历信息，不纳入提交。

## 第二十轮：文档时间语境与查询时间窗绑定

- 文档局部语义判断新增可选 `TimeWindow` 和明确文档记录时间，时间条件不再仅凭文档中出现目标实体就判定命中。
- 实体局部句段中的绝对日期、与查询同锚点的相对时间表达以及绑定中明确标注的记录日期时间可作为时间依据。
- 阳性文档证据明确位于时间窗内时返回 `MATCHED`，明确位于窗外时返回 `NOT_MATCHED`；缺事件锚点、局部时间或记录时间时严格返回 `UNKNOWN`。
- 住院期间可识别入院时、入院后、住院第 N 日、住院过程中、出院前和出院时；入院前和出院后作为住院窗外语境。
- 相对时间从查询提取事件锚点，再核对局部文本中的同锚点、方向和偏移量；除既有手术、入院和出院外，也支持化疗等自定义事件。
- 修复同一句中“较早否认，后续明确时间事件出现阳性”仍被前一否定覆盖的问题，否定作用域在明确后续时间事件处截断。
- 记录时间只从标签或字段路径明确表示日期/时间的绑定提取，不从临床自由文本中的任意日期冒充文档时间。
- 本轮没有自动追加数据库字段，没有修改内部或外部病历元数据、外部接口配置、服务 ID 和 API 契约。

## 第二十轮验证

- 文档时间、诊断和时间窗针对性测试：36 项通过。
- `python -m py_compile microharness/medical/document_semantics.py microharness/medical/structured_time.py web/app.py` 通过。
- `python -m pytest tests -q`：222 项通过。
- 同一版真实金标准评测：7/7 案例通过，49/49 断言通过，无 `BLOCKED` 和基础设施失败。
- 最终平均耗时 24397.77ms，P50 20090.29ms，P95 54477.50ms；单个本地模型调用波动明显，本轮不宣称性能提升。
- 评测报告位于忽略目录 `evaluation/medical_filter/reports/p1_document_time_semantics.json`，原始响应位于 `evaluation/medical_filter/responses/p1_document_time_semantics/`，可能包含病历信息，不纳入提交。

## 第二十一轮：有界跨句指代与上下文继承

- `microharness/medical/document_semantics.py` 新增按章节、段落和句子分段的有界指代解析，仅在同章节、同段落和有限句距内处理通用指代表达。
- 先行词必须唯一；同句多个医学实体、中间出现其他医学实体、断言冲突、主体切换、跨段、跨章节和超距均不强行绑定，返回可解释的 `UNKNOWN` 原因码。
- 引用句重新判断患者/家属主体、肯定、否定、排除、不确定、历史状态和时间语境；后续引用替代唯一先行状态，避免旧阳性覆盖新否定。
- 支持有限连续指代链，每一跳保留先行句、引用句、主体、断言、时间状态、时间原因和最终原因。
- `web/app.py` 在文件级结果附加 `semantic_trace`；`microharness/medical/evidence.py` 将其保留到规范化 `EvidenceItem.metadata`，原 API 字段保持兼容。
- 新增单一先行词、多实体歧义、中间实体歧义、患者/家属切换、引用否定、不确定继承、住院时序、跨段/跨章节/超距保护、时间窗继承、连续链和链式否定测试。
- 收尾回归发现并修复非患者先行词分支未定义局部变量的问题；新增“家属先行词保持非患者”和“引用句显式切回患者”的对称测试，避免异常或错误主体继承。
- 本轮没有修改内部或外部病历元数据、外部接口 URL、服务配置、服务 ID 或 API 契约，无需同步外部病历元数据接口。

## 第二十一轮验证

- 文档语义测试：38 项通过；文档语义与统一证据定向测试：52 项通过。
- `python -m pytest tests -q -p no:cacheprovider`：239 项通过。
- `python -m py_compile microharness/medical/document_semantics.py microharness/medical/evidence.py web/app.py` 通过；相关文件 `git diff --check` 通过。
- 当前工作区临时服务的同版金标准评测：6 个案例 `PASS`、1 个案例 `BLOCKED`，无 `FAIL`、无基础设施失败；49 项断言中 46 项通过、3 项临床断言因关键数据源不可用阻塞。
- 阻塞案例日志显示病历文档、诊断和就诊数据均未取得，且本地结构化服务地址 `127.0.0.1:9091` 未监听；严格三态返回“无法判断”符合数据不足约束，不能据此宣称医学准确率回归或提升。
- 平均耗时 21723.86ms、P50 19960.04ms、P95 28133.44ms；受本地模型与数据源状态影响，本轮不宣称性能稳定提升。
- 评测报告位于忽略目录 `evaluation/medical_filter/reports/p1_coreference_semantics.json`，原始响应位于 `evaluation/medical_filter/responses/p1_coreference_semantics/`，可能包含病历信息，不纳入提交。

## 第二十二轮：来源职责矩阵与多来源证据冲突消解

- `microharness/medical/evidence.py` 新增通用 `EvidenceRole` 和 `ConflictLevel`，证据来源分为主证据、辅助证据、上下文、时间锚点和候选证据。
- 来源角色通过服务 ID、服务语义元数据、主证据服务、时间锚点来源以及路由文档元数据推导；不依赖具体中文文档名、章节名、疾病名、药品名、检验项目名或院方编码。
- 证据先按来源身份聚合。同一服务的多条候选记录先形成一个来源级状态，再参与跨来源投票，避免同一来源的多条记录被错误放大成来源冲突。
- `TIME_ANCHOR` 和 `CONTEXT` 不参与条件阳性/阴性投票；多个确定性主证据冲突时统一返回 `UNKNOWN`，原因码为 `EVIDENCE_CONFLICT`，并保留冲突来源明细。
- 主证据为阳性、辅助证据为阴性时保留主证据阳性，同时记录 `SUPPORTING_DISAGREEMENT`；主证据为阴性、辅助证据为阳性时返回无法判断，避免辅助证据覆盖主证据反证。
- 主证据不可用时，满足证据契约的辅助来源可以接管判定；阳性/阴性/无法判断结果均通过 `ConditionResult` 统一输出。
- `web/app.py` 在执行结果进入证据模型前完成来源标注，并在统一裁决后同步旧版 `matched`、`status`、`判断状态`、`可判定`、`reason`、`reason_code` 等字段，保持 `/api/medical/query` 兼容。
- 本轮没有修改内部或外部病历元数据、外部接口 URL、服务 ID、服务配置和 API 请求契约，无需修改外部元数据接口数据。

## 第二十二轮验证

- `python -m pytest tests -q -p no:cacheprovider`：250 项通过，保留 2 个既有 FastAPI `on_event` 弃用警告。
- `python -m py_compile microharness/medical/evidence.py web/app.py tests/test_evidence_model.py`：通过。
- 相关文件 `git diff --check`：通过；仅存在工作区换行符转换提示。
- 本轮仅验证离线结构化证据模型和主流程接入，没有可用真实结构化服务时不执行医学准确率结论，不宣称准确率或性能提升。

## 第二十三轮：诊断候选证据展示完整性

- 结构化诊断候选记录补充诊断日期和完整诊断时间，前端诊断证据表不再因候选对象缺字段而把已有时间显示为“缺失”。
- 日期和时刻分字段返回时统一组合为 `YYYY-MM-DD HH:MM:SS`；若数据源只有其中一个字段，则只展示实际取得的值，不虚构时间。
- 诊断命中说明只展示实际存在的诊断类型、诊断状态和诊断时间，不再把缺失状态输出为“诊断状态=未取得”。
- 新增“诊断2 / 背痛 / 门诊诊断 / 2026-03-10 10:09:40”回归场景，验证候选记录编号、实体、诊断类型和时间完整传递到前端所使用的数据结构。
- 本轮为通用诊断字段处理，没有针对背痛、门诊诊断或诊断2写业务分支；没有修改路由、证据裁决、内部/外部病历元数据、外部接口配置和 API 契约。

## 第二十三轮验证

- `python -m pytest tests/test_diagnosis_rules.py -q`：10 项通过。
- `python -m pytest tests -q -p no:cacheprovider`：251 项通过，保留 2 个既有 FastAPI `on_event` 弃用警告。
- 2026-07-16 独立服务真实复测请求 `eeca5f44-cf46-4f9e-9ac2-554c513a358d`：接口总耗时约 21.5 秒，但 MES0004 诊断查询返回数据库 `Communication link failure: Connection reset by peer`，未取得诊断列表，因此本次不能用真实响应验证诊断2展示，也不据此判断业务回归。
- 直接重试 MES0004 得到相同数据库错误，确认阻塞位于上游诊断查询数据源，不是候选记录展示代码。
- 排查前端仍显示“缺失”时确认，8000 服务进程启动于 2026-07-16 16:58:02，早于诊断字段代码修改时间 17:18:56，实际加载的仍是旧模块；重启服务后新代码生效。
- 重启后的真实请求 `c796611f-e174-45b1-a864-9c2b4a8374da` 返回总体符合，诊断查询取得 7 条记录；目标候选记录明确返回 `诊断2 / 背痛 / 门诊诊断 / 2026-03-10 10:09:40`，前端诊断时间不再显示“缺失”。

## 第二十四轮：医疗实体与严格别名归一化

- 查询理解提示词要求 LLM 返回规范实体、严格等价别名、归一化置信度和来源；禁止把相关疾病、上下位概念或联想词当作别名。
- 新增 `microharness/medical/entity_normalization.py`，仅负责候选清洗、标点/大小写去重、置信度范围校验和兼容旧字段，不维护具体医学词典。
- Query IR、完整路由日志和执行路由均保留实体归一化信息；诊断、检验、用药规则按规范实体及严格别名匹配，并在候选证据中标出实际命中的候选词。
- 没有 LLM 别名时仍使用原有单实体匹配；没有修改病历元数据、本地/外部元数据接口、服务配置、字段映射、URL 或 API 契约。

## 第二十四轮验证

- `python -m pytest -q -p no:cacheprovider tests/test_entity_normalization.py`：7 项通过。
- `python -m pytest -q -p no:cacheprovider tests`：268 项通过，保留 2 个既有 FastAPI `on_event` 弃用警告。
- `py_compile` 覆盖实体归一化、Query IR、提示词、三类结构化规则和 `web/app.py`：通过。
- 本轮只验证离线结构化匹配和字段传递；未在真实外部服务环境进行医学标注集评估，不宣称真实准确率已经提升。

## 第二十五轮：受约束的医学语义实体召回

- 本地病历预筛从单一关键字扩展为规范实体、严格别名和全部实体候选共同召回；已有字面候选仍沿用原确定性文档语义链。
- 本地病历没有任何字符候选时，不再直接返回“未提及”，而是调用受约束 LLM 识别 `EXACT/STRICT_EQUIVALENT/RELATED/BROADER/NARROWER/NONE/UNCERTAIN` 关系。
- LLM 必须返回 `matched_entity` 和逐字原文 `evidence_span`；程序校验证据片段确实存在于源文档，且命中实体位于证据片段内。
- 只有 `EXACT` 和 `STRICT_EQUIVALENT` 可进入后续判断；相关概念、上下位概念和观察值不能证明目标诊断。证据通过后仍由 `document_semantics` 确定患者主体、否定、疑似、既往和时间窗状态。
- LLM 不直接输出最终四态，证据不实、字段缺失或关系不确定统一返回 `UNKNOWN`，完整文档无严格等价实体返回 `NOT_MENTIONED`。
- 外部诊断、检验、用药等结构化服务保持原字段召回和领域执行器逻辑，不扩大 LLM 语义匹配范围。
- 实现不包含“发烧=发热”等具体医学同义词字典，规则按实体关系协议和原文证据校验通用执行。
- 本轮没有修改内部或外部病历元数据、接口 URL、服务配置、字段映射和 API 契约，无需同步元数据。

## 第二十五轮验证

- 新增严格等价阳性、否定、未提及、相关观察不推断诊断、长症状表达、非原文证据、实体不在证据片段、无效 LLM 输出和精确关系校验用例。
- 定向执行链测试 `81 passed`，覆盖语义实体召回、文档语义、实体归一化和病历查询基础逻辑。
- `py_compile` 覆盖新增模块、文档语义、提示词和 `web/app.py`：通过。
- 全量测试 `297 passed`，保留 2 个既有 FastAPI `on_event` 弃用警告；`git diff --check` 通过，仅有工作区换行符提示。
- 尚需在真实模型及实际患者病历链路复测；完成标注集评估前不宣称真实医学准确率或性能提升。

## 第二十六轮：症状同一性枚举复核

- 真实模型验证发现主临床蕴含审核会把相关但独立的症状误判为更具体关系，例如错误地认为原文`呕吐`足以证明查询`恶心`。
- 新增独立症状同一性审核提示，只在主审核返回可蕴含、且查询和原文断言层级均为`SYMPTOM_OR_SIGN`时调用，不影响诊断、观察值、药物、检验和操作关系。
- 审核仅返回`SAME_SYMPTOM`、`SOURCE_QUALIFIED_SAME_SYMPTOM`、`DISTINCT_SYMPTOMS`或`UNCERTAIN`枚举，不使用容易与理由矛盾的布尔字段。
- 程序对枚举进行白名单校验：不同症状返回`NOT_MENTIONED`，不确定、缺失或非法结果返回`UNKNOWN`，同一症状或带限定同一才继续进入文档主体、否定、确定性和时间判断。
- `web/app.py`新增`[症状同一性复核]`日志及审核响应留痕；语义召回模型继续使用`seed=0`和`temperature=0.0`，普通 judge 调用保持原行为。
- 没有增加疾病、症状、药品、检验项目或医院编码词典；没有修改内部/外部病历元数据、外部接口 URL、服务配置、字段映射和 API 契约。

## 第二十六轮验证

- 症状关系真实模型专项：5/5。`发烧 <- 发热`为同一症状，`呼吸困难 <- 活动后气促`和`背痛 <- 胸背部疼痛`为带限定同一，`恶心 <- 呕吐`和`咳嗽 <- 咳痰`为不同症状。
- 完整真实模型链路：6/6。覆盖肯定、明确否定、观察值不能推出诊断、症状同义、带部位/活动限定和相关独立症状。
- 修复前同一组关键链路为5/6，失败项是`恶心 <- 呕吐`；修复后该项由主审核错误阳性被症状同一性复核否决，最终返回`NOT_MENTIONED`。
- `python -m pytest -q -p no:cacheprovider tests`：308 项通过，保留 2 个既有 FastAPI `on_event`弃用警告。
- `py_compile`覆盖语义召回、提示词和`web/app.py`：通过；`git diff --check`通过，仅有工作区换行符提示。
- 6/6 是固定回归样本结果，不代表整体医学准确率；后续需用更大的人工标注集持续评估。

## 第二十七轮：多候选语义召回与四态聚合

- 自由文本候选提取协议新增 `search_complete` 和 `candidates`，单篇证据文本最多返回五个按原文顺序排列的候选；旧版顶层单候选字段继续保留并兼容解析。
- 每个候选独立执行原文逐字校验、临床蕴含审核、症状同一性复核和文档语义判断，避免第一处否定、既往或相关表达遮蔽后续有效阳性证据。
- 新增确定性候选聚合：有效阳性优先；没有阳性时，完整候选中的明确否定或约束失败优先于未提及；不完整搜索、候选溢出、非法结构或无法审核的关系严格返回 `UNKNOWN`。
- 聚合证据 trace 记录候选数量、完整性、溢出状态、各四态计数以及每个候选的状态、原因码和原文证据，便于前端解释和问题回放。
- 新增 `evaluation/medical_filter/semantic_recall_gold.json`，以不含真实患者标识的人工复核案例覆盖多表达冲突、否定、跨断言层级、独立症状、语义不确定、非患者主体和完整未提及。
- 金标准测试直接调用生产代码中的候选解析、逐候选判断和聚合函数，不维护测试专用医学映射。
- 本轮没有修改内部或外部病历元数据、外部接口 URL、字段映射、服务配置和 API 契约，无需同步元数据接口。

## 第二十七轮验证

- `semantic_recall_gold.json` 通过 `python -m json.tool` 格式校验。
- `python -m pytest -q -p no:cacheprovider tests/test_semantic_entity_recall.py`：29 项通过，其中金标准逐案例执行生产候选解析、逐候选语义判断和四态聚合。
- `python -m pytest -q -p no:cacheprovider tests`：316 项通过，保留 2 个既有 FastAPI `on_event` 弃用警告。
- `py_compile` 覆盖语义召回、提示词、Web 执行链和金标准测试：通过；`git diff --check` 通过，仅有既有换行符提示。
- 当前结果证明离线规则和既有接口回归没有破坏，不等同于整体真实医学准确率；仍需持续扩充人工标注案例并在真实服务环境复测。

## 第二十八轮：Executor IR 驱动输入第一阶段

- 新增 `microharness/medical/condition_execution.py`，由最终 Query IR 和 EvidencePlan 富化结果构建冻结的 `ConditionExecutionSpec`，执行阶段统一读取规范实体、实体候选、修饰词、数值比较、时间、断言、量词、依赖、目标文档、目标章节和目标服务。
- 请求内服务映射和并行子条件结果改用 `condition_id@position` 执行键，解决两个子条件文本相同或 condition ID 重复时的覆盖风险；仅在前端兼容结果键需要区分时附加条件位置。
- 现代 IR 路径不再执行 `_extract_core_keyword` 关键词回退，也不再加载服务触发词配置猜测主证据服务；显式 `entity_type`、`semantic_class` 和 `target_skills` 仍保持原服务选择逻辑。
- 明确来源含 `fallback` 的旧分析继续允许关键词清洗和配置触发词扫描，避免一次性移除历史兼容能力。
- 条件结构日志也遵循同一边界：现代 IR 使用规格中的关键词，只有 legacy fallback 调用旧关键词清洗器。
- `condition_dict()` 补齐 `temporal`、`assertion`、`quantifier`、`depends_on`、`attributes`、执行来源和执行键，为后续时间窗与领域执行器迁移提供完整输入。
- 本轮未修改内部或外部病历元数据、外部元数据接口、接口 URL、服务 ID、服务配置、字段映射和公开 API 契约，无需同步外部接口数据。

## 第二十八轮验证

- 新增 12 项回归，覆盖现代 IR 不调用旧关键词解析、legacy fallback 兼容、重复文本唯一执行键、EvidencePlan targets/source IDs 保留、纯文档/纯结构化路由和显式主服务选择。
- `python -m pytest -q -p no:cacheprovider tests`：328 项通过，保留 2 个既有 FastAPI `on_event` 弃用警告。
- `python -m py_compile microharness/medical/condition_execution.py web/app.py tests/test_condition_execution.py tests/test_medical_patient_query.py`：通过。
- 相关文件 `git diff --check`：通过，仅有工作区既有换行符转换提示。
- 本轮证明执行输入迁移未破坏离线回归，不代表真实医学准确率提升；仍需在诊断、检验、用药、病历和时间锚点服务均可用的环境运行端到端固定问题集。

## 第二十九轮：TemporalIR 时间执行与统一执行实体

- `microharness/medical/time_window.py` 新增结构化事件规范化、事件元数据匹配和 `TemporalIR` 时间偏移计算。`before`、`after`、`during` 及无时长开放时间窗由确定性代码处理。
- 入院、出院和住院期间从 `encounter-info` 提取时间；手术及其他事件通过病历元数据声明的锚点章节取时间。结构化事件不匹配时不再退回全部锚点，避免使用无关文档日期。
- 多个事件记录支持 `first` 和 `last` 选择；未指定或 `any` 保持既有首个可用锚点行为，后续可结合证据量词进一步扩展。
- `web/app.py` 将执行规格中的 `temporal` 和 `legacy_fallback_allowed` 传入事件路由、检验主证据裁剪和时间窗解析。现代 IR 严格使用结构化时间，legacy fallback 保持旧中文时间解析能力。
- 条件执行链中剩余 9 处 `_extract_core_keyword(sq)` 已替换为 `ConditionExecutionSpec` 提供的执行实体，覆盖用药、诊断、病历预筛、语义判断、缺失判断、LLM 提示、字面纠正、原生证据和用户展示。
- legacy 执行实体优先使用清洗后的兼容关键词，修复 fallback IR 自动补入完整条件文本后可能覆盖核心实体的问题；现代 IR 仍优先规范实体和严格别名。
- 本轮没有修改内部或外部病历元数据、外部元数据接口、接口 URL、服务 ID、字段映射、服务配置或公开 API 契约，无需同步本地或外部元数据。

## 第二十九轮验证

- 新增结构化出院、入院、住院期间、手术锚点路由、手术时间窗和禁用文本回退测试；旧版中文时间窗测试继续通过。
- `python -m pytest -q -p no:cacheprovider tests/test_time_window.py tests/test_condition_execution.py`：18 项通过。
- 病历查询、Query IR、诊断、检验、用药和文档语义核心回归共 132 项通过。
- `python -m pytest -q -p no:cacheprovider tests`：334 项通过，保留 2 个既有 FastAPI `on_event` 弃用警告。
- `python -m py_compile microharness/medical/condition_execution.py microharness/medical/time_window.py web/app.py`：通过；相关文件 `git diff --check` 通过，仅有工作区换行符转换提示。
- 以上证明离线执行边界和既有接口回归未被破坏，不代表真实医学准确率已经提升；仍需在外部诊断、检验、用药、就诊及病历数据源同时可用时运行固定端到端问题集。

## 第三十轮：断言、病史与转归语义 IR 化

- `microharness/medical/condition_execution.py` 将历史语境、内部否定、转归状态、转归阶段和阶段诊断证据许可冻结到 `ConditionExecutionSpec`。执行阶段不再分别解析相同中文条件。
- `microharness/medical/semantic_rules.py` 新增通用转归状态和阶段规范化；确定性转归判断支持好转、恢复、未好转、持续、恶化和复发等通用状态，不维护具体医学实体词表。
- `microharness/medical/query_ir.py` 明确现代 IR 与 legacy fallback 的断言边界：现代查询理解缺少 `assertion` 时保留未知值，不从条件文本推断否定或既往语境；旧 fallback 保留原文本兼容解析。
- `microharness/ollama/prompt_adapter.py` 要求查询理解在转归条件的 `attributes` 中输出 `outcome_state`、`outcome_phase` 和 `outcome_evidence`，由程序校验后执行。
- `web/app.py` 已改为从执行规格读取病史语境、内部否定、转归状态和阶段；最终外部否定处理读取全部 `_execution_specs`，不再扫描原始 `analysis.conditions.modifiers`。
- 转归条件使用结构化诊断候选时增加证据能力限制：诊断存在不能自动推出阶段性好转、缓解、持续或恶化；明确的出院诊断语义通过 IR 单独授权。
- 新增现代 IR 禁止调用 legacy 文本解析器、结构化病史/否定、结构化转归、出院诊断证据许可、legacy 转归兼容和缺失断言不补猜等回归。
- 本轮未修改内部或外部病历元数据、外部元数据接口、接口 URL、服务 ID、服务配置、字段映射和公开 API 契约，无需同步外部接口数据。

## 第三十轮验证

- 执行规格、Query IR 与文档语义定向回归：72 项通过。
- 病历筛选主流程回归：24 项通过，保留 2 个既有 FastAPI `on_event` 弃用警告。
- `python -m pytest -q -p no:cacheprovider tests`：340 项通过，保留上述 2 个既有警告。
- `python -m py_compile microharness/medical/semantic_rules.py microharness/medical/condition_execution.py microharness/medical/query_ir.py microharness/ollama/prompt_adapter.py web/app.py`：通过。
- 相关文件 `git diff --check`：通过，仅有工作区换行符转换提示。
- 以上证明本轮离线执行边界和既有功能回归未被破坏，不代表真实服务环境的医学准确率已提升；仍需在外部结构化服务和真实病历数据同时可用时执行固定端到端问题集。

## 第三十一轮：年龄、数值比较与预判 IR 化

- `microharness/medical/condition_execution.py` 新增数值执行契约：`numeric_execution_required`、`is_age_condition`、`numeric_comparison_issue()` 和 `prejudge_numeric_hints()`。年龄识别与数值预判直接读取冻结的执行规格。
- 数值预判统一复用通用比较符、单位规范化和时间单位换算工具；只有比较主体和单位能够与预计算字段确定性对齐时才输出 `MATCHED/NOT_MATCHED`。
- 结构化比较缺少主体、有效比较符、有效阈值，年龄比较缺少“岁”单位，或证据中没有可比较数值时，执行器返回带原因码的 `UNKNOWN`，不再调用 LLM 补猜数值结论。
- `microharness/medical/query_ir.py` 支持上游直接提供 `numeric_comparison`，并在真实查询文本存在显式比较时优先采用确定性解析结果，避免 LLM 改写方向或阈值。
- `microharness/agent/query_understanding.py` 与查询理解提示新增 `numeric_comparison` 字段；结构修复生成年龄条件时同步写入结构化比较，不再只依赖 `is_numeric=true`。
- `web/app.py` 的预筛、Python 快速判断、年龄缺值保护和文档语义门禁统一消费 `ConditionExecutionSpec`，现代执行路径不再调用原始中文年龄/数值解析器。
- legacy fallback 中文表达继续在 IR 构建阶段解析一次，原有年龄、住院天数、检验、用药、诊断、文档和时间窗行为保持兼容。
- 本轮没有修改内部或外部病历元数据、外部元数据接口、接口 URL、服务 ID、服务配置、字段映射或公开 API 契约，无需同步外部接口数据。

## 第三十一轮验证

- 新增结构化年龄、结构化住院时长、无关展示文本、现代 IR 禁止旧解析器、不完整比较严格未知和 legacy 中文数值兼容测试。
- `python -m pytest -q -p no:cacheprovider tests/test_condition_execution.py`：18 项通过。
- Query IR、离线数值与路由专项回归：66 项通过；病历筛选主流程：24 项通过。
- `python -m pytest -q -p no:cacheprovider tests`：346 项通过，保留 2 个既有 FastAPI `on_event` 弃用警告。
- `python -m py_compile microharness/medical/condition_execution.py microharness/medical/query_ir.py microharness/agent/query_understanding.py microharness/medical/query_structure.py microharness/ollama/prompt_adapter.py web/app.py tests/test_condition_execution.py`：通过。
- 相关代码未修改病历元数据与外部服务契约；以上结果证明离线回归未被破坏，不代表真实临床数据上的准确率已经完成生产验收。

## 第三十二轮：统一确定性领域执行协议

- 新增 `microharness/medical/domain_execution.py`，统一确定性执行器输入 `DomainExecutionRequest`、结果 `DomainExecutionResult` 和通用证据能力 `EvidenceCapability`。
- 统一结果固定携带 `status`、`reason_code`、`data_quality`、候选记录数、支持能力、必需能力和缺失能力，同时保留 `matched/reason/fields/cot_response/候选记录` 等旧字段，现有 API 和前端无需更改协议。
- 检验、用药和诊断执行器按既有兼容顺序接入统一调度；原领域规则没有重写，既有时间、数值、医嘱状态、诊断断言和四态结论由适配层原样规范化。
- 数值、年龄和住院时长继续使用原 IR 数值引擎，只将输出收敛到统一协议。住院时长明确声明 `ENCOUNTER_PERIOD/NUMERIC_VALUE/TEMPORAL_OCCURRENCE` 能力，缺少入院或出院证据时返回 `UNKNOWN` 并列出缺失能力。
- 文档语义执行器接入同一协议，统一表达实体存在、文档上下文、患者主体和时间证据能力；肯定、否定、未提及、主体/时态不确定继续由 `document_semantics` 决定。
- 文档字面未提及时，原有受约束 LLM 严格等价召回仍可继续；文档肯定结果仍进入后续修饰词、转归和 LLM 校验链，适配层不会越过已有执行顺序直接接管复杂结论。
- `semantic_trace` 继续传递到旧文件结果和证据链，便于前端或日志展示主体、否定、指代和时间判断轨迹。
- 本轮没有修改内部或外部病历元数据、外部元数据接口、接口 URL、服务 ID、服务配置、字段映射或公开 API 契约，无需同步本地或外部元数据。

## 第三十二轮验证

- 文档、数值和执行规格定向回归：`72 passed`，覆盖文档肯定、否定、未提及、时间不确定、语义轨迹、住院时长符合/不符合和就诊周期缺失。
- 病历主流程、证据模型及诊断/检验/用药规则回归：`90 passed`，保留 2 个既有 FastAPI `on_event` 弃用警告。
- `python -m pytest -q -p no:cacheprovider tests`：`356 passed`，仅保留上述 2 个既有警告。
- `py_compile` 覆盖执行规格、统一领域协议、Web 主链路和新增测试：通过；`git diff --check` 通过，仅有工作区既有换行符提示。
- 本轮证明统一协议没有破坏离线既有行为，不代表真实临床数据准确率或外部服务稳定性已经完成生产验收。

## 第三十三轮：条件语义与证据能力注册表

- `microharness/medical/domain_execution.py` 新增 `ConditionSemanticType`、`EvidenceRequirement` 和通用证据要求注册表，执行器不再分别维护互相独立的“证据是否足够”判断。
- `DomainExecutionRequest` 接收冻结执行规格中的 `semantic_class/history_context/internal_negation/is_outcome_condition/outcome_state/outcome_phase/diagnosis_phase_evidence_allowed`，领域执行阶段不重新扫描原始问题推断这些语义。
- `DomainExecutionResult` 新增 `semantic_type/acceptable_source_roles/source_role_acceptable`，同时继续输出旧版 `matched/status/reason/fields/candidate_records` 等兼容字段。
- 能力缺失只会把原本需要该能力才能成立的阳性结论降级为 `UNKNOWN`；完整来源没有目标实体继续为 `NOT_MENTIONED`，明确否定或明确比较失败继续为 `NOT_MATCHED`。
- 上下文和时间锚点角色不能独立产生阳性结论；主证据、辅助证据和候选证据仍可参与既有裁决链。
- 诊断来源默认不能证明病史年限和转归状态。仅当 IR 明确允许出院诊断证据、且候选记录确为出院诊断时，才声明对应阶段状态能力。

## 第三十三轮：病史年限与转归迁移

- `judge_explicit_absence()`、`judge_history_duration()` 和 `judge_outcome_polarity()` 补充统一四态和原因码，旧调用方依赖的 `matched/reason` 字段保持不变。
- 文档领域执行器接管病史存在、病史年限和转归状态判断，并明确区分未提及、明确不满足、必要状态缺失和条件满足。
- `web/app.py` 删除病史缺失、病史年限及 LLM 后置转归覆盖分支；特殊语义的确定性文档结果直接返回，不再由后续 LLM 改写。
- 未针对高血压、背痛或其他具体疾病、症状、药品、检验项目、文档名称、患者或医院编码增加业务分支。
- 未修改内部/外部病历元数据、元数据来源切换、外部接口 URL、服务配置、字段映射或公开 API 契约。

## 第三十三轮验证

- 新增语义类型解析、证据能力要求、来源角色限制、诊断不能证明病史年限、文档病史年限四态和转归状态四态测试。
- 定向领域、文档和执行规格回归：`77 passed`。
- 诊断、检验、用药、证据模型和病历主流程兼容回归：`90 passed`，保留 2 个既有 FastAPI `on_event` 弃用警告。
- `python -m pytest -q -p no:cacheprovider tests`：`361 passed`，仅保留上述 2 个既有警告。
- `py_compile` 覆盖领域执行、条件执行、语义规则、Web 主链路和新增测试：通过；`git diff --check` 通过，仅有工作区换行符提示。
- 当前结果证明离线契约和既有功能未回归，不代表真实临床准确率、外部数据完整性或生产性能已经完成验收。

## 第三十四轮：多记录量词与记录选择执行器

- 新增 `microharness/medical/record_selection.py`，集中执行 `any/all/at_least/more_than/exact/at_most/less_than/latest/earliest` 多记录语义，并兼容 `first/last` 输入别名。
- `ConditionExecutionSpec.quantifier` 直接进入 `DomainExecutionRequest`，领域执行阶段不再扫描原始中文猜测“全部、至少、至多、最新、首次”等量词。
- 检验、用药和诊断候选记录统一补充 `record_status`、`record_reason_code`、`record_reason`、`scope_status` 和 `event_time`，为跨领域量词裁决提供同一记录契约。
- 时间窗外记录不计入量词；`latest/earliest` 必须具备完整可比较的事件时间。候选状态未知、记录明细未完全物化或声明条数与已加载明细不一致时，执行器按上下界严格返回 `UNKNOWN`。
- 连续次数属于有顺序约束的独立语义，当前仅完成 IR 识别并返回 `QUANTIFIER_UNSUPPORTED`，没有降级成普通 `exact` 次数，也没有交给 LLM 猜测。
- `DomainExecutionResult` 新增量词模式、次数、单位、选中记录、记录状态统计和候选完整性；旧版 `matched/status/reason/fields/candidate_records` 字段继续保留。
- 修复证据聚合边界：来源级量词裁决是该来源的最终事实，逐条候选只能进入展示证据链，不能通过投票覆盖 `all/count/latest/earliest` 的裁决结果。
- API 展示证据新增“量词裁决”，可追溯模式、目标次数、状态计数、完整性和实际选中记录。
- 未针对具体疾病、症状、药品、检验项目、文档名、患者或医院编码增加业务分支；未修改内部/外部病历元数据、接口 URL、服务配置、字段映射或公开 API 契约。

## 第三十四轮验证

- 新增和扩展多记录量词、领域执行、Query IR 量词解析和证据聚合测试，覆盖任一/全部、上下界次数、精确次数、最新/最早、时间窗排除、缺失事件时间、候选明细不完整和连续次数严格未知。
- 定向回归：`117 passed`。
- `python -m pytest -q -p no:cacheprovider tests`：`381 passed`，仅保留 2 个既有 FastAPI `on_event` 弃用警告。
- `py_compile` 通过；`git diff --check` 通过，仅有工作区既有 CRLF 转换提示。
- 当前结果证明多记录量词执行与既有离线功能兼容，不代表真实临床数据准确率、外部数据完整性或生产性能已经完成验收。

## 下一轮

1. 语义召回只输出通过原文位置校验的候选片段和关系标签，不直接输出最终四态。
2. 将语义召回候选转换为统一 `DomainExecutionRequest`，重新执行主体、否定、时间、数值、量词、病史和转归规则。
3. 继续迁移 Web 主链路中的结果二次修正，使解释层只消费统一领域结果、原因码和裁决后的证据事实。
4. 增加语义召回回灌执行器的金标准和真实环境端到端回归，确认状态与证据链一致后再推进性能优化。

## 第三十五轮：语义召回结果统一回灌领域执行器

- `domain_execution.py` 新增语义召回文档执行入口，并抽取普通文档判断与语义召回共用的结果终结逻辑。两条路径统一执行语义类型识别、证据能力声明、病史年限、转归状态、候选记录规范化和四态结果归一化。
- `web/app.py` 的单候选和多候选语义召回路径均改为构造规范领域请求，并通过 `DomainExecutionResult.to_file_result()` 输出兼容结果；不再由 Web 层直接拼接最终 `matched/status/reason` 字典。
- 审核通过的原文实体写入 `matched_entity`，病史年限和明确缺失判断使用实际原文实体执行，支持查询词与原文为严格等价表达时继续完成确定性判断。
- 语义候选记录新增 `record_status`、`record_reason_code`、`record_reason`、`scope_status`、`event_time`、`matched_entity` 和 `evidence_span`，为证据链和后续统一裁决保留可审计事实。
- 只允许 `any/exists` 对语义提及执行存在性量词。`all`、次数、最早和最新等量词需要独立临床记录身份，当前语义片段不具备该能力，统一严格返回 `DOCUMENT_QUANTIFIER_RECORD_IDENTITY_UNAVAILABLE`。
- 候选搜索不完整时，确定阳性仍可满足 `any`；非阳性结果保持 `UNKNOWN`，避免把未搜索到的潜在阳性误判为不符合。
- LLM 响应继续保留在 `cot_response` 供审计和调试，不参与四态裁决、量词裁决或证据能力判断。
- 未增加任何具体疾病、症状、药物、检验项目、文档名、患者、医院编码或预期答案分支；未修改内部/外部病历元数据、接口 URL、服务配置、字段映射和公开 API 契约。

## 第三十五轮验证

- 新增语义同义词阳性、明确否定、非患者主体、时间缺失、时间窗外、病史年限、转归状态、存在性量词、记录量词拒绝和候选搜索不完整测试。
- 聚焦语义召回、领域执行和记录选择回归：`65 passed`。
- 医疗筛选核心回归：`288 passed`，保留 2 个既有 FastAPI `on_event` 弃用警告。
- 剩余测试文件拆分回归：`51 passed`、`25 passed`、`24 passed`；与核心回归合计覆盖全部 `388` 项测试。
- 完整测试单进程执行在 300 秒上限内未退出，终止后按测试文件拆分复验全部通过；没有发现失败用例。
- `py_compile` 覆盖领域执行器、Web 主链路和新增测试：通过；`git diff --check` 通过，仅有工作区既有 CRLF 转换提示。
- 当前结果证明离线执行契约和既有功能未回归，不代表真实临床准确率、外部接口数据完整性或生产性能已经完成验收。

## 下一轮

1. 建立条件级统一裁决器，集中处理跨文档和跨结构化服务的来源角色、证据能力、时间范围、候选完整性和冲突关系。
2. 继续移除 Web 主链路对领域结果的二次状态修正，保留兼容序列化而不保留重复业务判断。
3. 让解释层只消费统一裁决后的事实，增加状态、数值、日期、来源和选中记录一致性测试。
4. 扩充人工标注与真实接口端到端回归，通过准确率门槛后再进入性能优化。

## 第三十六轮：条件级跨来源统一裁决

- `evidence.py` 新增类型化条件裁决请求与独立裁决入口，规范链路为 `EvidenceItem -> 来源级决策 -> ConditionResult`。旧字典调用统一通过兼容适配器进入同一裁决过程。
- 来源决策现在输出稳定逻辑来源 ID、来源角色、四态状态、原因码、数据质量、证据数量、记录 ID、候选完整性、支持/要求/缺失能力及角色可裁决性，便于接口和日志审计。
- 显式来源 ID 优先于服务 ID、模板 ID 和展示名称；同一来源的多条记录先按来源内语义聚合，避免把同一服务的候选记录误判成跨来源冲突。
- 增加三道条件级防线：缺少必要证据能力的阳性返回 `MISSING_REQUIRED_CAPABILITY`；不可裁决角色的阳性返回 `SOURCE_ROLE_NOT_DECISIVE`；候选集合不完整的缺失结论返回 `INCOMPLETE_CANDIDATE_SET`。
- 跨来源规则继续保持严格四态：主证据确定性冲突为 `UNKNOWN`；主证据阳性不会被辅助阴性覆盖；主证据阴性与辅助阳性冲突为 `UNKNOWN`；主证据不可用时，只有完整且可裁决的辅助证据可以接管。
- Web 条件执行末端改用 `adjudicate_condition_result()`；`build_condition_result()` 保留兼容，响应字段、证据明细和置信评估调用方式保持不变。
- 未针对具体疾病、症状、药品、检验项目、文档名、患者、医院编码或预期答案增加分支；未修改内部/外部病历元数据、接口 URL、服务配置、字段映射或公开 API 契约。

## 第三十六轮验证

- 新增类型化裁决入口、缺失证据能力、候选不完整辅助证据和显式逻辑来源聚合测试。
- 证据模型与领域执行器：`64 passed`。
- 查询理解、执行规格、诊断/检验/用药、文档语义、时间窗、记录选择和路由核心回归：`212 passed`。
- 复合条件、解释、作用域、技能和路由异常回归：`64 passed`。
- 外部服务错误、元数据来源、患者查询、并发、离线回归和服务目录：`52 passed`。
- 全部测试文件按组覆盖合计：`392 passed`，仅保留既有 FastAPI `on_event` 弃用警告。
- `py_compile` 通过；`git diff --check` 通过，仅有工作区既有 CRLF 转换提示。
- 当前结果证明统一裁决契约与既有离线功能兼容，不代表真实临床准确率、外部接口数据完整性、并发容量或生产性能已经完成验收。

## 下一轮

1. 让 Web 条件执行直接收集规范领域结果和证据，继续移除 `sq_matched/sq_reason` 对业务状态的重复计算。
2. 让解释层只消费裁决后的来源决策、事实和原因码，禁止根据自由文本重新推断状态。
3. 增加条件结果与用户解释之间的状态、时间、数值、来源和选中记录一致性测试。
4. 使用真实接口和人工标注病例验证准确率门槛，再安排性能与并发优化。

## 第三十七轮：移除 Web 条件级重复状态计算

- 删除 `check_one_condition` 中 `sq_matched/sq_reason` 的二次汇总。该分支此前按文件 `matched` 布尔值、修饰词和错误原因字符串先计算一次条件结论，随后又被统一证据裁决覆盖，存在双重真相源。
- Web 现在只负责收集并标注来源结果、补充原始证据展示和调用统一裁决；条件 `matched/status/reason/reason_code` 由 `ConditionResult` 一次性同步到兼容响应。
- 删除基于“外部数据源调用失败”“检验”“关键字未出现”等用户文本的阴性原因优先级排序。原因选择改由来源角色、规范状态、原因码和跨来源冲突结果驱动。
- 无来源证据的条件以 `UNKNOWN` 进入裁决，严格返回证据不足；不再把空结果默认解释成不符合。
- 辅助证据与主证据分歧时，即使旧汇总原因为空，也会从主证据来源提取原因并明确说明辅助分歧没有覆盖主结论。
- 未修改病历元数据、外部元数据接口、模板/章节用途、XPath、服务 URL/ID、字段映射、查询路由或公开 API 契约。

## 第三十七轮验证

- 新增旧摘要状态不能覆盖规范证据、空证据严格未知和辅助分歧解释测试。
- 证据模型与领域执行器：`66 passed`。
- 查询理解、执行规格、诊断/检验/用药、文档语义、时间窗、记录选择和路由核心回归：`212 passed`。
- 复合条件、解释、作用域、技能和路由异常回归：`64 passed`。
- 外部服务错误、元数据来源、患者查询、并发、离线回归和服务目录：`52 passed`。
- 全部测试文件按组覆盖合计：`394 passed`，仅保留既有 FastAPI `on_event` 弃用警告。
- 当前结果证明条件状态已经收敛到统一裁决器且现有离线行为未回归，不代表真实临床准确率、外部数据完整性或生产性能已经完成验收。

## 下一轮

1. 建立统一解释事实视图，解释层只读取裁决后的状态、原因码、来源决策和规范证据。
2. 对 LLM 润色结果执行事实一致性校验，禁止改变状态、数值、单位、日期、来源和选中记录。
3. 为条件解释和总体 AND/OR 解释补充一致性测试。
4. 完成真实接口金标准验收后，再推进性能、缓存、并发和生产保障。

## 第三十八轮：解释层只消费裁决事实

- `reason_polisher` 新增条件级事实视图，将规范 `ConditionResult` 的状态、原因码、数据质量、冲突级别、来源决策和 `EvidenceItem` 事实转换为面向解释器的只读载荷。
- 证据事实包含稳定来源、来源角色、记录 ID、文档/章节、实体、事件时间、数值、单位、异常标志、参考范围，以及时间窗、用药状态、剂量、频次、途径、候选完整性和量词等扩展事实。
- 子条件和来源展示状态优先读取 `condition_result/source_decisions`。测试覆盖旧 `matched/status/reason` 故意与规范裁决矛盾的场景，规范裁决保持唯一真相源。
- 来源级事实使用稳定来源 ID 关联，避免展示名称附带“(N条)”时无法找到对应证据；缺少稳定 ID 的旧数据仍保留精确来源名兼容。
- 润色提示明确禁止新增事实；返回后校验四态、日期、数值、比较方向、时间窗候选和住院天数减法方向。校验失败时不修改规范判定，继续使用确定性回退说明。
- 日期与数值校验支持等价格式规范化，例如日期分隔符和前导零差异、整数与小数表示差异；规则不包含具体医学实体或预期答案。
- 本轮没有修改病历元数据、外部元数据接口、模板/章节、用途、XPath、服务 URL/ID、字段映射、查询路由、领域判断规则或公开 API 契约。

## 第三十八轮验证

- 新增规范裁决覆盖旧字段、裁决事实字段完整性、稳定来源关联、等价日期/数值格式、虚构日期/数值拒绝、错误四态拒绝和事实一致解释接受测试。
- 解释、证据模型和条件总结聚焦回归：`68 passed`。
- 全部测试：`402 passed`，仅保留 2 个既有 FastAPI `on_event` 弃用警告。
- `py_compile` 通过；相关文件 `git diff --check` 通过，仅有工作区既有 CRLF 转换提示。
- 当前结果证明解释层不会通过润色重新改写规范裁决事实，不代表真实临床准确率、外部接口数据质量或生产性能已经完成验收。

## 下一轮

1. 将总体 AND/OR 用户解释改为直接读取规范 `condition_results` 和组合结构，移除对旧总体原因文本的依赖。
2. 对来源、记录 ID、单位、异常标志和参考范围增加结构化一致性校验，并记录解释拒绝原因。
3. 扩充真实接口金标准，分别统计四态、证据来源、时间窗、数值和解释一致性。
4. 准确性基线稳定后，再进入 LLM 调用预算、并发、缓存、超时和 P95 性能优化。

## 第三十九轮：总体裁决只消费规范子条件事实

- 在 `evidence.py` 新增通用 `build_overall_result()`，使用统一四态组合器从 `condition_results` 生成 `SINGLE/AND/OR` 总体裁决、原因码、组合依据和子条件摘要。
- 证据模型归一化完成后生成 `overall_result`，并同步单患者响应的兼容总体字段。无规范子条件时不覆盖旧响应，避免空结果兼容回归。
- 在 `reason_polisher.py` 新增总体事实视图，优先读取规范 `condition_results`，连接关系优先读取查询 IR，其次兼容旧 route/overall 字段。
- LLM payload 新增“总体裁决事实”。规则兜底和 LLM 结果校验统一使用规范总体状态、连接关系和子条件事实，旧总体原因不再作为真相源。
- 总体校验新增 AND/OR 语义保护、子条件状态校验和复合条件覆盖校验。错误连接关系、错误总体四态或遗漏关键子条件时拒绝 LLM 文本并使用确定性说明。
- 拆分总体与来源级“有用解释”校验，避免规范 JSON 中的字段名迫使总体说明机械复述所有来源细节。
- 所有规则均针对 IR、四态和证据结构，不包含具体疾病、症状、药品、检验项目、文档、患者或医院编码。
- 本轮没有修改病历元数据、外部元数据接口、模板/章节用途、XPath、服务 URL/ID、字段映射、查询路由或公开请求参数。

## 第三十九轮验证

- 新增 AND/OR 四态组合、规范字段覆盖冲突旧字段、错误连接关系拒绝、遗漏子条件拒绝、正确复合说明接受和 mocked LLM 回退测试。
- 证据模型与解释层聚焦回归：`73 passed`。
- 全部测试：`409 passed`，仅保留 2 个既有 FastAPI `on_event` 弃用警告。
- `py_compile` 通过；相关文件 `git diff --check` 通过，仅有工作区既有 CRLF 转换提示。
- 当前结果证明总体展示与规范子条件裁决保持一致，不代表真实临床准确率、外部接口数据完整性或生产性能已经完成验收。

## 下一轮

1. 对来源、记录 ID、单位、异常标志、参考范围和证据片段增加结构化解释一致性校验，并输出解释被拒绝的原因码。
2. 建立真实接口金标准和分层准确率报表，区分理解、路由、召回、执行、裁决和解释问题。
3. 完善可观测性后，再推进并发队列、LLM 调用预算、缓存、超时和 P95 性能优化。

## 第四十轮：解释校验与 Skill 无关化

- `reason_polisher.py` 的扩展事实序列化改为通用公开字段机制。任意 skill 的公开 `metadata`、来源决策自定义字段和嵌套结构均可在数量、深度和长度边界内进入解释事实视图；以下划线开头的内部字段不会暴露。
- 修复机器字段被展示自然化的问题。状态枚举、来源 ID、记录 ID、自定义单位、版本和扩展枚举在事实载荷中保持原值。
- 新增总体、子条件、证据源三类统一校验入口，全部返回通用原因码，不再使用分散的布尔短路判断。
- 新增 `解释校验` 审计结构，包含 `scope`、`accepted`、`used_fallback` 和 `reason_codes`。LLM 文本被接受时原因码为空；文本缺失、禁用、生成异常或校验失败时保留确定性回退说明并记录原因。
- 通用事实校验可拒绝新增日期/数值、比较方向改变、减法操作数颠倒、时间窗外候选被改写为从未发生、来源替换、记录 ID 伪造、单位替换、异常标志替换、参考范围替换和虚构证据原文。
- 校验动态读取规范裁决事实和任意公开 metadata，不识别具体 skill 名称或医学实体。未来 skill 只要遵守 `EvidenceItem`、来源决策和规范状态契约，即可复用现有解释与校验链。
- 本轮没有修改病历元数据、外部元数据接口、模板/章节、用途、XPath、服务配置、URL/ID、字段映射、IR 路由或公开请求参数。

## 第四十轮验证

- 新增虚拟未来 skill 测试，覆盖自定义来源字段、嵌套 metadata、私有字段过滤、事实一致文本接受、确定性回退和审计记录。
- 新增篡改测试，覆盖未知来源、伪造记录 ID、错误单位、错误异常标志、错误参考范围和虚构证据原文。
- 解释与证据模型聚焦回归：`81 passed`。
- `python -m pytest -q -p no:cacheprovider tests`：`417 passed`，仅保留 2 个既有 FastAPI `on_event` 弃用警告。
- `py_compile` 和相关文件 `git diff --check` 通过，仅有工作区既有 CRLF 转换提示。
- 当前结果证明解释层可以在不认识具体 skill 的情况下传递和校验规范事实，不代表真实临床准确率、外部接口数据完整性或生产性能已经完成验收。

## 下一轮

1. 建立真实接口金标准与分层准确率报表，优先验证多维自然语言条件在理解、路由、召回、时间窗和四态裁决各层的准确度。
2. 把解释拒绝原因、来源降级、LLM 回退和阶段耗时接入请求级审计与聚合指标。
3. 增加新 skill 注册契约测试，验证稳定来源身份、证据能力、候选完整性、规范状态和公开 metadata。
4. 准确性验收达到门槛后，再处理并发队列、调用预算、缓存、超时和 P95 性能。

## 第四十一轮：八层金标准评估与问题归因

- `scripts/evaluate_medical_filter.py` 将评测拆为理解、IR、路由、证据、时间窗、子条件裁决、总体裁决和解释八层；每层独立输出状态、断言数量和稳定原因码。
- 案例结果新增最早失败层和最早阻塞层，汇总报告新增分层案例/断言指标、首失败层计数、首阻塞层计数和失败原因码计数。
- 规范结果提取优先读取 `overall_result`、`condition_results`、来源决策和 `EvidenceItem.metadata.logical_source_id`，旧响应字段仅用于兼容。
- 支持通过字段路径断言归一化、IR、EvidencePlan、证据 metadata、时间语义、原因码、数据质量、冲突等级和解释审计，不需要为新 skill 增加评估器代码分支。
- 来源匹配收紧为精确逻辑身份或条件前缀形式，避免相似来源名称发生子串误匹配。
- 新增金标准 schema 校验。重复案例 ID、错误集合类型、非对象 `fields`、缺少 selector 和非法解释 scope 会在请求发出前明确报错。
- `evaluation/medical_filter/README.md` 已更新为 schema `1.1.0`，记录八层语义、`BLOCKED` 规则、首失败归因、通用 selector、字段路径和未来 skill 契约。
- `gold_cases.json` 升级为 `1.1.0`。7 个案例补充查询类型、连接关系、规范 IR 和时间语义，未复核的临床结果保持不填写。
- 虚拟未来 skill 测试覆盖八层全部通过、IR 最早失败、数据不可用阻塞、分层汇总、来源身份、证据事实和三级解释审计。
- 本轮没有修改病历元数据、外部元数据接口、模板/章节、用途、XPath、服务 URL/ID、字段映射、业务执行规则或公开 API 契约。

## 第四十一轮验证

- 评估器聚焦测试：`7 passed`。
- IR、证据模型、证据计划和解释层相关回归：`127 passed`。
- `python -m pytest -q -p no:cacheprovider tests`：`424 passed`，仅保留 2 个既有 FastAPI `on_event` 弃用警告。
- `py_compile`、实际 `gold_cases.json` schema 校验和相关文件 `git diff --check` 均通过。
- 本轮未运行依赖真实外部病历、诊断、检验和用药服务的医学标注集验收，因此不能据此宣称实际医学准确率提高。

## 下一轮

1. 将解释校验原因、LLM 回退、来源降级、跨来源冲突和首失败层写入请求级可观测 trace。
2. 建立 skill 注册契约测试，确保未来来源按稳定合同接入而非主链路特判。
3. 扩充人工复核的真实接口金标准并生成分领域准确率基线。
4. 准确性达到门槛后，再推进并发、缓存、调用预算、超时、熔断和 P95 性能。

## 第四十二轮：未来 Skill 机器展示合同与元数据时间策略

- `annotate_evidence_source()` 为服务和文档证据补充 `source_kind`、`source_label`、`domain`、`entity_type`、`evidence_type/evidence_types` 和 `record_type`，同时保留旧 `source_type` 字段。
- 新 skill 可通过 `semantic.presentation.record_type` 声明候选记录展示类型。前端优先读取机器字段并自动选择专用或通用表格，不再识别当前 skill 的中文展示名称。
- 前端证据角色改为读取规范 `source_role/evidence_role`；旧字段形状识别仅保留为兼容路径。
- `web/app.py` 的通用时间过滤由 `semantic.temporal_filter_mode` 驱动，默认 `generic`。`lab-results` 和 `encounter-info` 声明为 `domain`，由各自领域执行逻辑负责时间语义，主链路不再维护具体服务名称排除列表。
- 时间窗响应新增运行时解析的 `source_label`，前端移除固定服务 ID 名称映射。
- 置信度评估改为读取稳定的服务身份字段，清除最后一处按诊断、用药、检验和就诊中文名称识别结构化来源的逻辑。
- 新增任意影像 skill 和任意风险评分 skill 测试，验证新来源的展示合同、证据角色和置信度不需要修改主链路名称分支。
- 本轮没有修改病历文档元数据、外部元数据接口、模板/章节、用途、XPath、服务 URL/ID、字段映射、IR 语义或公开 API 请求参数。

## 第四十二轮验证

- 病历筛选核心、领域执行、证据计划、服务目录和前端契约回归：`99 passed`。
- `python -m pytest -q -p no:cacheprovider tests`：`428 passed`，仅保留 2 个既有 FastAPI `on_event` 弃用警告。
- `py_compile` 和相关文件 `git diff --check` 通过；工作区仅有既有 LF/CRLF 转换提示。
- 当前结果证明未来 skill 可以按机器合同接入展示和通用时间策略，不代表真实接口医学准确率或生产性能已经完成验收。

## 下一轮

1. 将解释校验原因、LLM 回退、来源降级、跨来源冲突和首失败层接入请求级 trace 与聚合指标。
2. 建立 skill 注册契约检查，统一校验稳定来源身份、语义元数据、时间策略、证据能力、候选完整性和公开展示字段。
3. 扩充人工复核的真实接口金标准并形成分领域准确率基线。
4. 准确性达到门槛后，再推进并发、缓存、调用预算、超时、熔断和 P95 性能。

## 第四十三轮：请求级 Trace 与通用 Skill 注册契约

- 新增 `microharness/medical/request_trace.py`，从规范 IR 质量、EvidencePlan、`ConditionResult`、来源决策、总体裁决、解释审计、阶段耗时和排队状态构建稳定请求追踪对象。
- Trace 按八层顺序聚合原因码并输出 `first_issue`，同时统计条件四态、来源状态、数据质量、来源不可用、降级、能力缺失、候选不完整、证据冲突和解释回退。
- `web/app.py` 在成功、执行异常、队列满、排队超时和重复请求 ID 响应中附加 `request_trace`，并写入 `[medical_query][trace]` 单行 JSON 日志。
- Trace 构建发生在业务执行完成之后，只读现有规范字段，不改变现有查询理解、路由、取证、时间窗、领域执行、裁决和解释结果。
- `microharness/services/service_catalog.py` 新增 `validate_service_contract()` 和 `validate_service_catalog()`，对任意未来 skill 使用同一机器契约，不维护现有服务 ID 白名单。
- `SKILL.md` 服务要求完整语义契约；配置文件中的历史自定义服务继续允许加载，并通过 `compatible` 警告暴露缺失项。
- `load_services()` 为服务附加内部 `_contract` 报告，运行时配置合并不能覆盖该字段，也不能用空配置擦除 `SKILL.md` 的结构化元数据。
- 已核对外部 HTTP 客户端只使用 `request_map`、`request_wrapper`、方法和 URL 组包，`_contract` 不会进入患者业务参数；外部服务配置查询也不公开该内部字段。
- 新增未来影像、风险评分和基因来源契约测试，以及正常完成、执行异常、队列拒绝三类 `/api/medical/query` 接口追踪测试。
- 本轮没有修改病历元数据、外部元数据接口、模板名称、章节名称、用途、XPath、服务 URL/ID、字段映射、患者标识映射或公开请求参数，无需同步外部病历元数据。

## 第四十三轮验证

- 请求 Trace、服务契约和外部请求隔离聚焦回归：`26 passed`。
- 当前四个已注册服务契约均为 `complete`、`valid=True`。
- `python -m pytest -q -p no:cacheprovider tests`：`441 passed`，仅保留 2 个既有 FastAPI `on_event` 弃用警告。
- `py_compile` 和相关文件 `git diff --check` 通过，仅有工作区既有 LF/CRLF 转换提示。
- 当前结果证明请求异常可以分层定位、未来 skill 可以按统一契约接入，不代表真实接口医学准确率或生产性能已经完成验收。

## 下一轮

1. 扩充人工复核的真实接口金标准，形成八层准确率和首失败层基线。
2. 建立请求 Trace 聚合报表，按原因码、来源、模型、阶段耗时和数据质量统计趋势。
3. 将 skill 契约检查接入部署前验收，明确区分阻断错误和历史兼容警告。
4. 准确性达到门槛后，再推进调用预算、缓存、超时、熔断、并发和 P95 性能优化。

## 第四十四轮：人工复核绑定与分层准确率基线

- `gold_cases.json` 和评测报告升级为 schema `1.2.0`。案例复核状态统一为 `pending`、`routing_only`、`verified` 或 `rejected`；临床结论断言只允许出现在 `verified` 案例中。
- `verified` 复核必须记录 `reviewed_by`、ISO `reviewed_at` 和复核说明。现有已人工确认案例只补充复核来源信息，没有修改患者结论或任何医学标签。
- 新增语义响应 SHA-256 指纹，覆盖归一化、IR、EvidencePlan、路由、规范条件结果和总体裁决，并排除请求 ID、排队和阶段耗时等运行噪声。
- 配置 `source_response_sha256` 后，当前响应发生语义漂移会将临床断言标记为 `BLOCKED/REVIEW_RESPONSE_DRIFT`；非临床的理解、路由、证据和时间断言继续执行，避免把数据变化误报为模型准确率下降。
- 新增 `--review-output` 生成人工复核清单。清单只生成 `pending` 模板，不会根据当前系统输出自动写回或修改金标准；`--fail-on-review` 可在临床复核未绑定、已过期或无法校验时阻断发布。
- 汇总报告新增 `review_metrics.bound_clinical_accuracy`，只统计复核信息完整且指纹仍有效的临床断言；兼容指标继续保留，但不再等同于生产可信准确率。
- `segment_metrics` 按案例类别、IR 领域、时间关系、复核状态、复核绑定、总体四态、来源健康度、模型和首失败层输出案例、断言组和八层指标。
- `trace_metrics` 聚合 Trace 覆盖率、模型版本、首问题层/原因码、来源不可用和降级、解释回退、排队等待以及各阶段平均/P50/P95 耗时。
- 全部实现只读取通用 IR、EvidencePlan、ConditionResult、EvidenceItem 和 request trace 合同，不识别当前诊断、用药、检验、就诊 Skill 名称，也没有为未来 Skill 增加专用分支。
- 本轮没有修改内部或外部病历元数据、模板/章节名称、用途、XPath、服务 URL/ID、字段映射、路由规则、医学裁决逻辑或公开查询接口，无需同步外部病历元数据。

## 第四十四轮验证

- 评测器聚焦测试：`13 passed`。
- 评测器、请求 Trace 和服务契约聚焦回归：`28 passed`，保留 2 个既有 FastAPI `on_event` 弃用警告。
- `python -m pytest -q -p no:cacheprovider tests`：`447 passed`，保留相同 2 个既有弃用警告。
- `py_compile`、实际 `gold_cases.json` schema 校验、CLI 参数检查和相关文件 `git diff --check` 均通过。
- 当前唯一包含临床结论的人工确认案例尚未绑定已保存响应指纹，因此 `bound_clinical_accuracy` 暂无可宣称的生产基线。本轮提升的是评测可信度和漂移保护，不能据此宣称真实医学准确率提高。

## 下一轮

1. 在诊断、用药、检验、就诊和病历数据源同时可用的环境保存固定案例原始响应并生成人工复核清单。
2. 由人工逐项核对患者结论、证据记录、时间窗和解释后，将确认的响应指纹绑定到金标准，形成首个可信分领域准确率基线。
3. 将 Skill 契约检查和 `--fail-on-review` 接入部署前验收，区分契约错误、数据阻塞、复核漂移和模型失败。
4. 临床准确性门槛达到后，再推进 LLM 调用预算、缓存、超时、熔断、并发和 P95 性能优化。

## 第四十四轮实测补充：旧响应追踪兼容与真实基线

- 对 7 条固定案例完成真实接口采集和离线重放，生成原始响应、分层报告和人工复核清单。当前断言结果为 `7 PASS`、`139` 条断言通过，但临床指纹尚未绑定，不能据此宣称生产医学准确率。
- 评测器新增旧响应兼容：原生 `request_trace` 缺失但存在 `timings` 时，复用通用请求追踪构建器生成 `origin=legacy_synthesized` 的只读追踪；报告分别统计原生和兼容追踪，避免把旧实例数据冒充完整 Trace。
- 修复评测脚本直接执行时未将项目根目录加入模块搜索路径的问题，CLI 重放模式现在可加载项目内的通用追踪模块。
- 当前基线总耗时均值约 `27.7s`、P50 约 `19.6s`、P95 约 `58.7s`；最慢案例约 `69.0s`，结构化服务阶段约 `55.4s`。
- 当前四态中 2 条为“无法判断”；兼容追踪显示 2 条来源降级、6 条解释兜底。上述结果等待人工证据复核，不自动修改临床标签或响应指纹。
- 验证结果：评测器 `13 passed`；评测器、请求 Trace、服务契约聚焦回归 `28 passed`；完整测试集 `447 passed`，仅保留 2 个既有 FastAPI `on_event` 弃用警告；`py_compile` 和相关文件 `git diff --check` 通过。
- 本轮没有修改任何病历元数据、本地/外部元数据内容、Skill 路由或医学裁决规则，无需同步外部病历元数据。

## 第四十五轮：候选不确定性分级与严格未提及裁决

- `microharness/medical/evidence.py` 新增 `EvidenceUncertaintyKind` 及统一推导函数，规范来源失败、能力缺失、时间未解析、检索不完整、候选被拒绝和候选语义未决。
- 旧语义召回协议中的证据缺失、非原文证据、实体越界和精确匹配失败原因码兼容映射到 `REJECTED_CANDIDATE`，不解析中文原因文本。
- `adapt_legacy_evidence()` 保留原执行器原因码，并在 metadata 中生成规范 `uncertainty_kind`；旧保存响应仍可通过结构化 `semantic_trace` 重放最新裁决。
- `DomainExecutionResult.to_file_result()` 和来源决策统一输出 `uncertainty_kind`，文档语义执行不再维护一套独立的不确定性判断逻辑。
- 跨来源聚合只允许完整 `NOT_MENTIONED` 覆盖来源失败或被证据协议拒绝的候选。时间未解析、缺少能力、检索不完整和真正语义未决仍返回 `UNKNOWN`。
- 同源不确定性只汇总状态为 `UNKNOWN` 的证据；`selection_complete=false` 等安全信号优先于显式候选分类，避免未来 Skill 输出矛盾字段时形成过强阴性结论。
- 新增任意未来来源 ID 的通用测试，覆盖完整未提及加拒绝候选、同源混合证据、普通语义未决、时间不确定、检索不完整及旧语义轨迹兼容。

## 第四十五轮验证

- 聚焦证据与领域执行测试：`77 passed`。
- 语义召回、文档语义、请求 Trace 和评测器扩展回归：`169 passed`。
- 全部 33 个测试文件分批执行：`454 passed`，无断言失败；保留既有 FastAPI `on_event` 弃用警告和 `.pytest_cache` 权限警告。
- 单进程 `python -m pytest -q` 在 10 分钟上限后超时；分批已覆盖所有测试文件，后续单独定位测试进程资源/线程收尾问题。
- `py_compile` 和相关改动 `git diff --check` 通过。
- 保存的真实烧伤响应重放为 `NOT_MENTIONED/NO_MATCHING_RECORD`，保存的泮托拉唑术前响应仍为 `UNKNOWN/INSUFFICIENT_EVIDENCE`。
- 本轮没有修改病历元数据、本地/外部元数据、模板/章节、用途、XPath、服务 URL/ID、字段映射或路由配置，无需同步外部接口数据。

## 下一轮

1. 将 `uncertainty_kind` 纳入请求 Trace、评测报告和来源健康指标。
2. 扩展通用 Skill 注册契约，校验来源身份、完整性、能力和候选集合声明。
3. 完成人工真实病例复核和响应指纹绑定，不以自动回放结果代替临床准确率。
4. 定位全量单进程测试超时后，再进入性能与并发阶段。

## 第四十六轮：不确定性 Trace 与评测分层

- `microharness/medical/request_trace.py` 将 Trace schema 从 `1.0.0` 升级为 `1.1.0`，复用统一证据不确定性推导函数。
- 来源 Trace 新增全部来源和 UNKNOWN 来源两组不确定性计数；请求摘要日志新增 `source_uncertainty`，不改变原有 outcome、condition、source、timing 和 issue 字段。
- `scripts/evaluate_medical_filter.py` 新增来源不确定性数量、案例数量和 `source_uncertainty` 分层维度；旧 Trace 没有新字段时按空分布兼容，不阻断旧响应评测。
- 新增未来来源测试，验证未知 Skill ID 无需专用代码即可统计 `REJECTED_CANDIDATE`，并验证 `selection_complete=false` 优先归类为 `INCOMPLETE_SEARCH`。
- 保存的烧伤响应经最新裁决后生成 Trace：总体 `NOT_MENTIONED`，子条件为 `MATCHED + NOT_MENTIONED`，UNKNOWN 来源中记录两个 `REJECTED_CANDIDATE`。

## 第四十六轮验证

- Trace 和评测器聚焦测试：`20 passed`。
- 证据、领域执行、Trace、评测、并发和服务契约回归：`111 passed`。
- 全部 33 个测试文件分三批执行：`144 + 109 + 202 = 455 passed`，仅保留 2 个既有 FastAPI `on_event` 弃用警告。
- `py_compile` 和相关改动 `git diff --check` 通过。
- 本轮没有修改病历元数据、本地/外部元数据、模板/章节、用途、XPath、服务 URL/ID、字段映射或路由配置，无需同步外部接口数据。

## 下一轮

1. 扩展通用 Skill 注册和执行结果契约校验。
2. 用不确定性分层驱动人工真实病例复核和缺口优先级。
3. 修复单进程全量测试资源收尾问题。
4. 临床准确性门槛稳定后再进入性能与并发优化。
## Runtime fix: dynamic source-label helper scope

- Root cause: `_source_display_label()` was accidentally nested in `clear_index()`, so `_run_medical_query()` could not resolve it while building `time_window_data.source_label`.
- Fix: moved the helper to module scope and passed `_service_catalog_for_evidence_plan` from the current request. The helper uses dynamic metadata and preserves unknown source IDs without a Skill allowlist.
- Verification: added `test_source_display_label_uses_dynamic_catalog_and_keeps_unknown_source_id`; related query, time-window, medication, lab, execution, evidence, and Trace tests passed (`160 passed`).
- Runtime boundary: current workspace direct execution no longer raises the exception. The process on port `8000` was not reloaded and still returned the old `NameError`; restart it before HTTP replay.
- Metadata impact: none. Local and external medical-record metadata do not require synchronization for this fix.

## 第四十七轮：转归条件的临床阶段裁决

- 新增通用临床阶段模型，将病历证据归一为入院、住院期间、治疗后、术后、出院、出院后和随访等阶段；阶段来自实时病历元数据的 `info_type`、`purpose` 和可选显式阶段字段，不依赖疾病、症状、文档 ID 或 Skill ID 白名单。
- 未明确阶段的“好转/改善/缓解”等转归条件采用 `latest_available_outcome`：当前可用的最晚转归阶段作为主证据阶段，较早阶段保留为上下文证据。因此入院时“未缓解”不会否定出院时“明显改善”，但同一目标阶段内的改善与未改善仍形成确定性冲突并返回 `UNKNOWN`。
- 文档来源按阶段动态分配 `PRIMARY/CONTEXT`；仅提供实体存在性的结构化服务为 `SUPPORTING`。未来 Skill 只有声明 `evidence_capabilities.outcome_state=true` 等价能力时，才可成为转归主证据，无需修改主链路代码。
- 修正“外院诊疗经过”的阶段歧义，避免把入院现病史误识别成住院期间证据。实时外部元数据验证结果为：入院记录现病史/主诉=`admission + CONTEXT`，出院记录出院情况=`discharge + PRIMARY`，目标阶段=`discharge`。
- 新增临床阶段、阶段角色、未来 Skill 能力、跨阶段非冲突、同阶段冲突和结构化转归 IR 测试；相关回归共 `154 passed`，`py_compile` 与 `git diff --check` 通过。
- 当前端口 `8000` 的既有进程仍返回旧的 `CONCLUSIVE_CONFLICT`，需要由服务维护方重载后再做 HTTP 验收。新代码日志应出现 `[Step2-转归阶段] 目标阶段=discharge | 策略=latest_available_outcome`。
- 本轮没有修改本地或外部病历元数据、模板名称、章节名称、用途、XPath、外部接口 URL、服务 ID 或字段映射，无需同步外部元数据。

## 第四十八轮：能力元数据与来源裁决策略标准化

- 新增 source_capability.py，集中生成 supported_capabilities、required_capabilities 和 missing_capabilities；未来 Skill 可通过 semantic 或执行结果声明能力，无需在主链路增加 Skill ID 判断。
- 新增 evidence_policy.py，集中解析来源角色和跨来源冲突。角色优先读取语义元数据，再结合主来源、路由来源和时间锚点上下文推导。
- 能力是角色可裁决的前置条件：即使来源被标记为 PRIMARY，只要缺少时间、数值、给药、转归等必要能力，也不能独立形成符合结论。
- 保持现有保守裁决不变：主证据互相冲突返回 UNKNOWN；主证据符合而辅助证据不符合时保留主结论并记录辅助分歧；主来源不可用时允许完整且有能力的辅助来源接管。
- 新策略不依赖当前诊断、用药、检验、就诊服务 ID，也不依赖病历模板名称。未来 Skill 可声明 role_policy.by_semantic_type 或 default_role。
- 本轮不修改内部或外部病历元数据、模板/章节、purpose、XPath、接口 URL、字段映射和服务请求参数，无需同步外部元数据。
- 性能、并发、单进程测试收尾、真实病例金标准扩充和前端展示优化已记录为暂缓项，待这两项基础能力稳定后再继续。
