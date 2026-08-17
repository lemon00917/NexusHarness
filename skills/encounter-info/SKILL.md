---
name: encounter-info
description: |
  查询患者就诊基本信息。任何涉及就诊类型、入院/出院时间、住院天数、科室、病区的查询都应调用此服务。
  返回就诊类型、科室、医生、病区、入院日期时间、出院日期时间。
metadata:
  emoji: "🏥"
  safety: AUTO_APPROVE
  semantic:
    entity_type: encounter
    domain: encounter
    evidence_types: [encounter_evidence, demographic_evidence]
    temporal_filter_mode: domain
    presentation:
      record_type: encounter
      record_identity:
        label: 就诊号
        fields: [hosEncId, hdcEncId]
  triggers: ["就诊","住院","出院","入院","就诊科室","就诊医生","病区","就诊状态","就诊类型","科室","病房","住院天数","入院日期","出院日期"]
  api:
    url: SerachQuery/MES0002
    method: POST
    request_wrapper: params
    rec_prefix: "就诊"
    request_map:
      data:
        businessFieldCode: "{{global_visit_id._bfc}}"
        hdcPatientId: "{{global_patient_id}}"
        hdcEncId: "{{global_visit_id}}"
    returns: |
      返回字段说明（供路由选择、时间筛选、语义判断使用）：
      
      [时间字段] 入院日期时间(encStartDate+encStartTime合并) — 用于"入院前""入院后"等时间窗口的参考锚点
      [时间字段] 出院日期时间(encEndDate+encEndTime合并) — 用于"出院后""出院前"等时间窗口的参考锚点
      住院天数 = 出院日期时间 - 入院日期时间 — 数值比较条件直接使用此计算结果
      
      [匹配字段] 就诊类型(encTypeDesc): 住院/门诊/急诊/体检 — 判断就诊性质
      [匹配字段] 就诊科室(encDeptName): 患者所在科室 — 科室相关条件匹配
      [匹配字段] 就诊状态(encStatusDesc): 在就诊/已出院/转科 — 状态相关条件匹配
      [匹配字段] 当前病区(currWardName): 患者所在病区 — 病区相关条件匹配
      
      其他字段：就诊医生(encDocName)
    keep_fields: ["encTypeDesc","encDocName","encDeptName","encStatusDesc","currWardName","encStartDate","encStartTime","encEndDate","encEndTime"]
    merge:
      - name: "入院日期时间"
        fields: ["encStartDate", "encStartTime"]
        sep: " "
      - name: "出院日期时间"
        fields: ["encEndDate", "encEndTime"]
        sep: " "
---

# 就诊信息查询

调用外部就诊接口，查询患者就诊基本信息。

## 入参
- global_patient_id: 全局患者ID
- global_visit_id: 全局就诊号
- businessFieldCode: 从 global_visit_id 截取（下划线前部分）

## 返回字段

### 时间字段（用于 temporal_filter 时间窗口筛选）
- **入院日期时间**: encStartDate + encStartTime 合并，格式 YYYY-MM-DD HH:MM:SS
  - 用于"入院前XX天""入院后XX小时"等时间锚点
- **出院日期时间**: encEndDate + encEndTime 合并，格式 YYYY-MM-DD HH:MM:SS
  - 用于"出院后XX天""出院前XX小时"等时间锚点
- **住院天数** = 出院日期时间 - 入院日期时间（用于"住院<5天"等数值比较）

### 匹配字段（用于 LLM 语义判断）
- **就诊类型**(encTypeDesc): 住院/门诊/急诊/体检
- **就诊科室**(encDeptName): 内分泌科/骨科/心内科 等
- **就诊状态**(encStatusDesc): 在就诊/已出院/转科
- **当前病区**(currWardName): 老年医学科护理单元 等

### 其他字段
- 就诊医生(encDocName)

## 判断规则

1. **时间锚点选择**:
   - 查询含"入院前" → 参考锚点 = 入院日期时间
   - 查询含"出院后" → 参考锚点 = 出院日期时间
   - 查询含"住院天数" → 数值比较 出院-入院
2. **语义匹配**: 就诊类型/科室/病区/状态与查询条件做语义匹配
