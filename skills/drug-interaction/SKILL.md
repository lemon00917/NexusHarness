---
name: drug-interaction
description: |
  查询患者全部用药医嘱记录。任何涉及具体药物名称的查询（如"开了葡萄糖""服用阿司匹林""注射青霉素""用过维生素"）都应调用此服务。
  返回每条用药的药品名称、剂量、频次、途径、开立日期时间。
metadata:
  emoji: "💊"
  safety: AUTO_APPROVE
  semantic:
    entity_type: drug
    semantic_class: 用药医嘱
    predicate: used
  triggers: ["用药","药物","药品","医嘱","处方","注射","口服","外用","西药","中药","输液","抗生素","激素","维生素","注射液","片","胶囊","颗粒","剂量","开了","用了","使用","使用过","服用","吃了","用过","用过什么药"]
  api:
    url: SerachQuery/MES0005
    method: POST
    request_wrapper: params
    rec_prefix: "用药"
    request_map:
      data:
        businessFieldCode: "{{global_visit_id._bfc}}"
        hdcPatientId: "{{global_patient_id}}"
        hdcEncId: "{{global_visit_id}}"
    returns: |
      返回字段说明（供路由选择、时间筛选、语义判断使用）：
      
      [时间字段] 开立日期时间(orderDate+orderTime合并为YYYY-MM-DD HH:MM:SS) — temporal_filter用于时间窗口比较
      [匹配字段] 药物名称(orderName) — 与查询关键词语义匹配（如"葡萄糖"匹配"葡萄糖注射液"、"5%葡萄糖"等）
      [匹配字段] 医嘱大类(ordCatDesc)、医嘱子类(ordSubCatDesc) — 辅助判断用药类型（西药/中药/输液等）
      
      其他字段：单次剂量(medicineDosage)、剂量单位(medDosUnitDesc)、频次(medFreqDesc)、
      用药途径(medUsageDesc)、剂型(medDoseFormDesc)、疗程(medDurDesc)、处方号(medPrescNo)
    keep_fields: ["orderName","ordCatDesc","ordSubCatDesc","medicineDosage","medDosUnitDesc","medFreqDesc","medUsageDesc","medDoseFormDesc","medDurDesc","orderDate","orderTime","medPrescNo","orderQuantity","orderRemarks"]
    merge:
      - name: "开立日期时间"
        fields: ["orderDate", "orderTime"]
        sep: " "
---

# 用药医嘱查询

调用外部用药接口，查询患者全部用药医嘱记录。

## 入参
- global_patient_id: 全局患者ID
- global_visit_id: 全局就诊号
- businessFieldCode: 从 global_visit_id 截取（下划线前部分）

## 返回字段

### 时间字段（用于 temporal_filter 时间窗口筛选）
- **开立日期时间**: orderDate + orderTime 合并，格式 YYYY-MM-DD HH:MM:SS

### 匹配字段（用于 LLM 语义判断）
- **药物名称**(orderName): 与查询关键词语义匹配，不要求字面完全一致
  - 例："葡萄糖" 可匹配 "葡萄糖注射液"、"5%葡萄糖"、"葡萄糖氯化钠注射液" 等
  - 例："阿司匹林" 可匹配 "阿司匹林肠溶片"、"拜阿司匹林" 等
- **医嘱大类**(ordCatDesc): 西药/中药/输液 等
- **医嘱子类**(ordSubCatDesc): 注射类/口服类 等

### 其他字段
- 单次剂量(medicineDosage)、剂量单位(medDosUnitDesc)
- 频次(medFreqDesc): bid/tid/qd 等
- 用药途径(medUsageDesc): 口服/静脉注射/肌肉注射 等
- 剂型(medDoseFormDesc): 注射液/片剂/胶囊 等
- 疗程(medDurDesc)、处方号(medPrescNo)

## 判断规则

1. **药物匹配**: 药物名称与查询关键词做语义匹配，不要求字面完全一致
2. **时间筛选**: temporal_filter 使用"开立日期时间"字段与参考时间比较
3. **注意**: 此服务返回患者所有用药记录，需结合 temporal_filter 做时间约束
