# 病历智能筛选金标准评测

该目录保存人工复核的稳定评测案例，用于量化智能筛选各阶段的正确性。评测器不参与业务判断，也不修改内部或外部病历元数据。

## 评测原则

1. 只有人工确认过的患者结论才填写 `overall_status`、`condition_statuses` 或 `overall_result`。
2. 未确认患者真实结论的案例只断言理解、IR、路由、证据和时间语义，不猜测临床结论。
3. 上游接口失败、数据源不可用、缺少事件锚点或单位时，相关断言标记为 `BLOCKED`，不计为模型假阴性。
4. `FAIL` 表示系统已经返回可比较结果，但与人工金标准不一致。
5. 禁止为了让当前实现通过而修改金标准。变更必须经过人工复核并记录原因。

## 八层模型

报告 schema 版本为 `1.2.0`，每个案例按以下固定顺序输出 `layers`：

| 层 | 含义 | 典型断言 |
| --- | --- | --- |
| `understanding` | 条件理解与拆分 | 条件数、条件文本、查询类型、连接关系、归一化 |
| `ir` | 规范 IR | 领域、谓词、数值比较、量词等字段 |
| `routing` | 证据路由 | 目标服务、目标文档、逻辑来源 ID、来源解析 |
| `evidence` | 证据召回 | 来源、记录 ID、事实字段、最少记录数 |
| `temporal` | 时间窗 | 事件、关系、时长、单位、记录是否在窗内 |
| `condition_adjudication` | 子条件裁决 | 四态、原因码、数据质量、冲突等级 |
| `overall_adjudication` | 总体组合 | AND/OR、总体四态、总体原因码 |
| `explanation` | 解释一致性 | 解释审计、必需文本和禁止文本 |

每层状态为 `PASS`、`FAIL`、`BLOCKED` 或 `NOT_EVALUATED`。`first_failure_layer` 指向最早失败层，避免把上游 IR 错误误归因为下游裁决错误；没有失败但存在阻塞时，`first_blocked_layer` 指向最早阻塞层。`failure_codes` 和 `blocked_codes` 使用稳定通用原因码，便于按模型、条件类型或版本聚合。

## 金标准格式

最小案例包含 `id`、`condition` 和 `expected`。评估器启动前会校验重复案例 ID、列表和对象类型、条件/来源 selector、字段断言以及解释 scope，配置错误会直接终止评测。

通用 selector：

```json
{
  "condition_id": "c1",
  "condition_contains": "风险评分",
  "source_id": "service:future-risk-score",
  "source_contains": "future-risk"
}
```

同一 selector 中通常选择稳定 ID；`*_contains` 仅用于旧响应或文本兼容。嵌套事实通过字段路径断言：

```json
{
  "condition_ir": [
    {
      "condition_id": "c1",
      "fields": {
        "领域": "future_risk",
        "数值比较.operator": ">",
        "数值比较.threshold": 10.0
      }
    }
  ],
  "evidence_assertions": [
    {
      "condition_id": "c1",
      "required_source_ids": ["service:future-risk-score"],
      "fields_any": {
        "unit": "score",
        "metadata.selection_complete": true
      }
    }
  ]
}
```

支持的主要预期字段：

- 理解：`condition_count`、`required_condition_contains`、`query_type`、`connector`、`normalization`
- IR：`condition_ir`
- 路由：`required_services`、`required_documents`、`required_source_ids`、`routing_assertions`
- 证据：`required_evidence_sources`、`required_evidence_source_ids`、`evidence_assertions`
- 时间：`temporal_assertions`
- 裁决：`condition_statuses`、`overall_status`、`overall_result`
- 解释：`explanation_audits`、`required_reason_contains`、`forbidden_reason_contains`

## 人工复核

每个案例必须提供 `review`。允许的状态为：

- `pending`：尚未完成复核，不允许填写临床结论断言。
- `routing_only`：只复核理解、IR、路由、证据或时间语义，不允许填写临床结论断言。
- `verified`：已人工确认临床结论；必须填写 `reviewed_by`、合法的 ISO `reviewed_at` 和 `note`。
- `rejected`：案例不适合进入金标准，必须在 `note` 中说明原因。

`overall_status`、`condition_statuses` 和 `overall_result` 只允许出现在 `verified` 案例中。评测器不会根据当前系统输出自动生成或修改这些标签。

复核可通过 `source_response_sha256` 绑定当时的语义响应。指纹只覆盖归一化、IR、EvidencePlan、路由、规范条件结果和总体裁决，不包含请求 ID、排队时间或阶段耗时等运行噪声。绑定后的响应发生变化时，临床断言标记为 `BLOCKED/REVIEW_RESPONSE_DRIFT`，路由和证据等非临床断言仍正常评测。

生成待复核清单：

```powershell
python scripts/evaluate_medical_filter.py `
  --endpoint http://127.0.0.1:8000/api/medical/query `
  --response-dir evaluation/medical_filter/responses `
  --review-output evaluation/medical_filter/reports/review_manifest.json
```

清单中的 `review_template` 始终为 `pending`，需要人工查看保存的原始响应和证据链后再更新金标准。`--fail-on-review` 可作为严格发布门禁：存在未绑定、已漂移或因基础设施失败无法校验的临床复核时返回退出码 `3`。

## 未来 Skill

评测器不识别具体 skill 名称。新 skill 只需沿用稳定契约即可被评估：

1. IR 中提供条件 ID 和 `evidence_plan_source_ids`。
2. EvidencePlan 中提供稳定 `source_id`、`source_type` 和解析状态。
3. `EvidenceItem` 提供 `metadata.logical_source_id`、记录 ID、规范状态和公开事实。
4. `ConditionResult` 提供四态、原因码、数据质量、来源决策和规范证据。
5. 解释层提供总体、条件或来源级 `解释校验`。

来源身份采用精确匹配，或允许条件前缀形式，例如 `c1:service:future-risk-score` 可满足 `service:future-risk-score`；不会进行任意子串匹配。

## 文件

- `gold_cases.json`：版本化端到端案例和断言。
- `semantic_recall_gold.json`：人工复核的自由文本语义关系和四态案例。
- `../../scripts/evaluate_medical_filter.py`：实时调用和离线回放评测器。
- `reports/`：本地评测报告，已加入 `.gitignore`。
- `responses/`：可选原始响应快照，可能包含病历信息，已加入 `.gitignore`。

## 实时评测

```powershell
python scripts/evaluate_medical_filter.py `
  --endpoint http://127.0.0.1:8000/api/medical/query `
  --output evaluation/medical_filter/reports/baseline.json
```

只运行一个案例并保存原始响应：

```powershell
python scripts/evaluate_medical_filter.py `
  --endpoint http://127.0.0.1:8000/api/medical/query `
  --case-id length_of_stay_and_back_pain `
  --response-dir evaluation/medical_filter/responses `
  --output evaluation/medical_filter/reports/length_of_stay.json
```

## 离线回放

```powershell
python scripts/evaluate_medical_filter.py `
  --replay evaluation/medical_filter/responses/length_of_stay_and_back_pain.json `
  --case-id length_of_stay_and_back_pain
```

`--replay` 支持响应目录、`{case_id: response}` 映射或 `{responses: {case_id: response}}` 文件。CI 或发布门禁需要在断言失败时返回非零退出码，可增加 `--fail-on-assertion`；需要同时校验临床复核绑定时增加 `--fail-on-review`。

报告继续保留 `overall_status_accuracy`、`condition_status_accuracy`、`routing_assertion_accuracy` 和 `evidence_assertion_accuracy`，兼容已有统计。`review_metrics.bound_clinical_accuracy` 只统计已完整复核且指纹仍有效的临床断言。

`segment_metrics` 按案例类别、IR 领域、时间关系、复核状态、复核绑定、总体四态、来源健康度、模型和首失败层输出案例及八层断言指标。`trace_metrics` 聚合请求 Trace 覆盖率、模型、首问题、来源不可用/降级、解释回退、排队等待和各阶段 P50/P95。上述指标用于定位和建立基线，不能代替人工医学复核。

评测器优先读取接口返回的原生 `request_trace`。重放旧版响应时，如果响应没有
`request_trace` 但包含 `timings`，评测器会使用现有规范 IR、证据结果和阶段耗时生成
`origin=legacy_synthesized` 的兼容追踪。报告会分别统计 `native_cases` 和
`legacy_synthesized_cases`；兼容追踪可用于定位慢阶段和已有原因码，但不能证明排队、
运行模型和完整请求生命周期已经被原生观测。
