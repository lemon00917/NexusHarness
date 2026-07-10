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
