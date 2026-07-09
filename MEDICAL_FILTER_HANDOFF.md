# 病历筛选交接说明

本文档用于给新电脑或新开发者快速了解病历筛选接口、当前进度和后续优化点。

## 1. 快速入口

- API 文档：`API文档.md`
- 优化计划：`artifacts/medical_filter_optimization_plan.md`
- 改造记录：`artifacts/medical_filter_refactor_log.md`
- 元数据配置：`configs/medical_catalog.json`
- 外部服务配置：`configs/external_services.json`
- 主接口实现：`web/app.py`
- 路由逻辑：`microharness/medical/query_router.py`
- 解释润色和防漂移：`microharness/medical/reason_polisher.py`
- 检验规则：`microharness/medical/lab_rules.py`
- 结构化时间规则：`microharness/medical/structured_time.py`
- 服务目录：`microharness/services/service_catalog.py`

## 2. 运行前提

1. 安装 `requirements.txt` 中的 Python 依赖。
2. 本地或服务器可访问 Ollama。
3. 默认模型使用 `qwen2.5:3b`，部分规划可用 `deepseek-r1:1.5b`。
4. 外部病历/检验/用药/诊断接口地址在 `configs/external_services.json` 里配置。

启动服务：

```powershell
python -m uvicorn web.app:app --host 0.0.0.0 --port 8000
```

健康验证：

```powershell
python -m py_compile web\app.py microharness\medical\query_router.py microharness\medical\reason_polisher.py
```

## 3. 病历筛选接口

接口：

```http
POST /api/medical/query
Content-Type: application/json
```

示例请求：

```json
{
  "condition": "40岁以上并且背痛，住院期间血红蛋白指标异常",
  "register_no": "0000000120",
  "visit_no": "174",
  "global_patient_id": "00001_120",
  "global_visit_id": "00001_174",
  "router_model": "qwen2.5:3b",
  "judge_model": "qwen2.5:3b",
  "planner_model": "deepseek-r1:1.5b"
}
```

返回重点字段：

- `matched_count`：匹配患者数量。
- `results[].matched`：当前患者是否符合。
- `results[].reason`：用户解释，必须能说明符合/不符合原因。
- `results[].per_condition`：每个子条件的证据链。
- `route_warnings` / `llm_invalid_targets`：LLM 路由异常和修复痕迹。

## 4. 当前判断链路

当前不是单纯让 LLM 直接判断，而是分层执行：

1. Query Planner/Router：LLM 或规则把自然语言拆成子条件和候选证据源。
2. Metadata Router：根据 `medical_catalog.json`、service catalog 和 skill metadata 选择文档/章节/外部服务。
3. Executor：按结构化规则查询用药、检验、诊断、文档章节等证据。
4. Deterministic Rules：时间窗、数值阈值、异常标志、单位、否定、AND/OR 由规则校验。
5. Reason Polisher：LLM 只做展示优化；如果润色改变证据含义，会回退规则解释。

原则：

- 不把疾病、文档、章节写死在判断代码里。
- 能用接口结构化字段判断的，不只靠文档文本。
- LLM 返回未知文档/章节时不静默丢弃，要进入证据链诊断。
- 候选记录不符合时，要说明哪条记录、时间、数值和不符合原因。

## 5. 当前已完成优化

- 复合条件拆分后保留每个子条件的证据链。
- 检验数值比较支持科学计数法和区间范围解析。
- 检验“异常/偏高/偏低”优先使用异常标志和参考范围。
- 用药候选记录会展示开立时间、时间窗和不符合原因。
- 诊断/症状证据源改为依赖 metadata 角色，不按具体疾病名写死。
- LLM 返回非法文档名时保留 `llm_invalid_targets`，便于分析为什么没查到。
- 外部服务配置页支持维护 `base_url` 和服务列表，服务路由仍由配置/skill metadata 驱动。

## 6. 仍需重点关注

- 通用 Query IR 仍需继续标准化为 `domain/entity/temporal/numeric/negation`。
- 文档章节、诊断、结构化接口之间的优先级还需要更清晰的场景配置。
- 需要补固定回归集，覆盖：
  - 术前/术后用药时间窗。
  - 检验数值大于/小于/偏高/偏低/异常。
  - 年龄、住院天数、烧伤、背痛等诊断/症状问题。
  - 入院记录、出院记录、病程记录等多文档章节证据。
- Ollama/RAG 的全局行为已有改动，其他业务如果依赖旧行为，需要单独回归。

## 7. 建议回归问题

```text
术前24小时使用过阿司匹林且术前48小时内中性粒细胞数偏低的患者
术前24小时使用过阿司匹林且术前48小时内中性粒细胞数＞1.5×10⁹/L的患者
40岁以上并且背痛，住院期间血红蛋白指标异常
住院天数小于5天并且烧伤的患者
住院期间血红蛋白指标偏高的患者
```

## 8. 提交与排除说明

本项目中测试脚本、debug 输出和 probe 报告可能存在本地未提交文件。分析正式能力时，以已提交源码和上述文档为准；临时文件只作为人工复盘参考。
