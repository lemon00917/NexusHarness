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
    domain: medication
    semantic_class: 用药医嘱
    evidence_types: [medication_evidence]
    predicate: administered
    evidence_capabilities:
      ordered: true
      administered: true
      status: true
    fields:
      record_id: [medPrescNo, 处方号]
      entity: [orderName, 药物名称, 药品名称, 医嘱名称]
      category: [ordCatDesc, ordSubCatDesc, 医嘱大类, 医嘱子类]
      ordered_at: [开立日期时间, orderDateTime, orderDate, 开立时间]
      administered_at: [administeredAt, administrationDateTime, executeDateTime, 给药时间, 执行时间]
      start_at: [startDateTime, 医嘱开始时间, 开始时间]
      end_at: [endDateTime, 医嘱结束时间, 停止时间]
      status: [ordStatusDesc, 医嘱状态描述, 医嘱状态]
      dose: [medicineDosage, 单次剂量, 剂量]
      dose_unit: [medDosUnitDesc, 剂量单位]
      frequency: [medFreqDesc, 频次]
      route: [medUsageDesc, 用药途径, 给药途径]
      form: [medDoseFormDesc, 剂型]
      duration: [medDurDesc, 疗程]
      quantity: [orderQuantity, 数量]
      remarks: [orderRemarks, 医嘱备注, 备注]
    predicate_policies:
      administered:
        event_time_role: ordered_at
        required_status: true
        accepted_status_values: [核实, 执行]
        rejected_status_values: [作废, 撤销]
  triggers: ["用药","药物","药品","医嘱","处方","注射","口服","外用","西药","中药","输液","抗生素","激素","维生素","注射液","片","胶囊","颗粒","剂量","开药","开了","开过","开立","开立过","开具","下过医嘱","用了","使用","使用过","服用","服过","吃了","吃过","用过","用过什么药"]
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
      [状态字段] 医嘱状态描述(ordStatusDesc) — 按当前项目配置的有效/无效状态判断医嘱是否有效
      [匹配字段] 药物名称(orderName) — 与查询关键词语义匹配（如"葡萄糖"匹配"葡萄糖注射液"、"5%葡萄糖"等）
      [匹配字段] 医嘱大类(ordCatDesc)、医嘱子类(ordSubCatDesc) — 辅助判断用药类型（西药/中药/输液等）
      
      其他字段：单次剂量(medicineDosage)、剂量单位(medDosUnitDesc)、频次(medFreqDesc)、
      用药途径(medUsageDesc)、剂型(medDoseFormDesc)、疗程(medDurDesc)、处方号(medPrescNo)
    keep_fields: ["orderName","ordCatDesc","ordSubCatDesc","medicineDosage","medDosUnitDesc","medFreqDesc","medUsageDesc","medDoseFormDesc","medDurDesc","orderDate","orderTime","ordStatusDesc","ordReqExecDate","ordReqExecTime","ordStopDate","ordStopTime","medPrescNo","hdcOrdId","orderQuantity","orderRemarks"]
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

### 状态字段（用于医嘱有效性判断）
- **医嘱状态描述**(ordStatusDesc): 根据当前项目 `predicate_policies` 配置判断有效或无效

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
2. **“使用过”业务口径**: 目标药品医嘱的开立时间命中目标时间窗，且医嘱状态描述属于当前项目配置的有效状态
3. **时间筛选**: 使用"开立日期时间"字段与参考时间比较；请求执行时间仅作为原始字段保留，不视为实际给药时间
4. **注意**: 此服务返回患者所有用药记录，需结合时间窗口和医嘱状态共同判断
