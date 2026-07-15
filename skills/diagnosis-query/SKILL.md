---
name: diagnosis-query
description: |
  查询患者全部诊断记录。任何涉及疾病/症状存在性的查询（如"患有高血压""存在烧伤""背痛的患者""入院前就胸背部"）都应调用此服务。
  返回每条诊断的名称、类型（入院/出院/门诊/补充/术前/术后）、级别、日期、医生、ICD编码。
metadata:
  emoji: "🩺"
  safety: AUTO_APPROVE
  semantic:
    entity_type: diagnosis
    domain: diagnosis
    evidence_types: [diagnosis_evidence, disease_symptom_evidence, symptom_evidence]
  triggers: ["诊断","确诊","疑诊","入院诊断","出院诊断","是什么病","得了","患有","存在","疾病","病症"]
  api:
    url: SerachQuery/MES0004
    method: POST
    request_wrapper: params
    rec_prefix: "诊断"
    temporal_semantics:
      field: diagTypeDesc
      rules:
        - query_terms: ["入院前", "入院时", "入院"]
          values: ["入院诊断"]
          reason: "诊断类型为入院诊断，可作为入院时已存在的结构化证据"
        - query_terms: ["出院"]
          values: ["出院诊断"]
          reason: "诊断类型为出院诊断，可作为出院时诊断结论的结构化证据"
        - query_terms: ["术前", "手术前"]
          values: ["术前诊断"]
          reason: "诊断类型为术前诊断，可作为手术前诊断的结构化证据"
        - query_terms: ["术后", "手术后"]
          values: ["术后诊断"]
          reason: "诊断类型为术后诊断，可作为手术后诊断的结构化证据"
        - query_terms: ["住院期间", "住院期内", "本次住院"]
          values: ["补充诊断", "入院诊断", "出院诊断"]
          reason: "诊断类型提示该诊断属于本次住院诊断证据"
    request_map:
      data:
        businessFieldCode: "{{global_visit_id._bfc}}"
        hdcPatientId: "{{global_patient_id}}"
        hdcEncId: "{{global_visit_id}}"
    returns: |
      返回字段说明（供路由选择、时间筛选、语义判断使用）：
      
      [时间字段] 诊断日期(diagnoseDate)、诊断时间(diagnoseTime) — 可与入院日期等参考时间做比较
      [匹配字段] 诊断名称(diagnoseName) — 与查询关键词语义匹配，不要求字面一致（如"胸背部"可匹配"背痛""胸椎骨折"等）
      [分类字段] 诊断类型(diagTypeDesc) — 诊断的时间属性，决定诊断与查询时间锚点的对应关系：
        - 入院诊断: 入院时已存在的疾病 → 对应"入院前""患有""存在""既往有"等查询
        - 出院诊断: 出院时的最终结论 → 对应"出院""确诊""最终诊断"等查询
        - 门诊诊断: 门诊时做出 → 对应"门诊"查询
        - 补充诊断: 住院期间补充发现 → 对应"住院期间发现"查询
        - 术前诊断: 手术前确认 → 对应"术前""手术前"查询
        - 术后诊断: 手术后确认 → 对应"术后""手术后"查询
      
      其他字段：诊断级别(diagLevelDesc)、诊断医生(diagDocName)、ICD编码(diagIcdCode)、诊断分类(diagCategory)
---

# 诊断查询

调用外部诊断接口，查询患者全部诊断记录。

## 入参
- global_patient_id: 全局患者ID
- global_visit_id: 全局就诊号
- businessFieldCode: 从 global_visit_id 截取（下划线前部分）

## 返回字段
diagnoseName(诊断名称), diagTypeDesc(诊断类型), diagLevelDesc(诊断级别), diagnoseDate(诊断日期), diagnoseTime(诊断时间), diagDocName(诊断医生), diagIcdCode(ICD码), diagnoseCode(诊断代码), diagnoseRemarks(诊断备注), diagStatusDesc(诊断状态), diagCategory(诊断分类)

## 诊断类型（diagTypeDesc）说明

每种诊断类型对应不同的临床场景。判断患者是否符合条件时，应根据查询中的时间锚点选择对应的诊断类型：

| 诊断类型 | 含义 | 对应查询场景 |
|----------|------|-------------|
| **入院诊断** | 患者入院时经检查确认的诊断，反映入院时已存在的疾病或症状 | "入院前XX"、"入院时XX"、"既往有XX"、"有XX病史"、"患有XX"、"存在XX"、"XX的患者" |
| **出院诊断** | 患者出院时的最终诊断结论，是确诊的权威来源 | "出院诊断XX"、"确诊XX"、"最终诊断XX"、"出院时XX"、"XX好转/治愈" |
| **门诊诊断** | 门诊就诊时做出的诊断 | "门诊诊断XX"、"门诊发现XX" |
| **补充诊断** | 住院期间补充发现、追加的诊断 | "住院期间发现XX"、"补充诊断XX" |
| **术前诊断** | 手术前确认的诊断，手术的适应症依据 | "术前诊断XX"、"手术前诊断XX" |
| **术后诊断** | 手术后确认的诊断，术后病理或修正诊断 | "术后诊断XX"、"手术后诊断XX"、"术后病理XX" |

### 判断规则

1. **无明确时间锚点的查询**（如"患有高血压的患者"、"存在烧伤的患者"）：
   - 优先看**入院诊断**（代表入院时已存在的疾病）
   - 其次是**出院诊断**（代表确诊结论）
   - 诊断名称与查询关键词语义匹配即可，不要求字面完全相同（如"胸背部"匹配"背痛"）

2. **"入院前"查询**（如"入院前就胸背痛的患者"）：
   - 主要看**入院诊断**，诊断日期应早于或等于入院日期
   - 诊断名称做语义匹配：查询中的症状描述（如"胸背部"）与诊断名称（如"背痛"）语义一致即可判定匹配

3. **"出院"查询**（如"出院诊断为骨折的患者"）：
   - 主要看出院诊断

4. **"术前/术后"查询**：
   - 分别看术前诊断/术后诊断

5. **语义匹配原则**：
   - 诊断名称不需要与查询关键词字面完全一致
   - 例如："胸背部"可以匹配"背痛"、"胸痛"、"胸背痛"、"胸椎骨折"等
   - 例如："发烧"可以匹配"发热"、"高热"、"体温升高"等
   - 由 LLM 判断语义相关性，不要做纯字符串匹配
