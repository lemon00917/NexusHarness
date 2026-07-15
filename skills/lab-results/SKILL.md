---
name: lab-results
description: |
  查询患者全部检验/化验项目结果。任何涉及检验指标、化验结果、血常规、生化、白细胞计数、WBC、CRP、血糖、肌酐、电解质、指标偏高/偏低/异常、结果数值比较等查询都应调用此服务。
  返回每条检验项目的项目名称、结果、单位、异常标志、参考范围、检测日期时间、扩展结果等。
metadata:
  emoji: "LAB"
  safety: AUTO_APPROVE
  semantic:
    entity_type: laboratory
    domain: laboratory
    evidence_types: [laboratory_evidence]
  triggers: ["检验","化验","检验指标","化验指标","指标","结果","异常","偏高","偏低","升高","降低","高于","低于","参考范围","血常规","生化","肝功","肾功","电解质","白细胞","白细胞计数","WBC","中性粒","血红蛋白","血小板","CRP","C反应蛋白","血糖","肌酐","尿素","钾","钠","氯"]
  api:
    url: SerachQuery/MES0023
    method: POST
    request_wrapper: params
    rec_prefix: "检验"
    request_map:
      data:
        businessFieldCode: "{{global_visit_id._bfc}}"
        hdcPatientId: "{{global_patient_id}}"
        hdcEncId: "{{global_visit_id}}"
    returns: |
      返回字段说明（供路由选择、时间筛选、语义判断使用）：

      [时间字段] 检测日期时间(inspectionDate+inspectionTime合并为YYYY-MM-DD HH:MM:SS) - temporal_filter用于时间窗口比较。
      [匹配字段] 化验项目描述(inspItemDesc)、缩写(inspItemAbbr)、化验项目代码(inspItemCode) - 与查询中的检验项目语义匹配，例如“白细胞计数”可匹配项目名中的“白细胞计数”或缩写“WBC”。
      [结果字段] 结果(inspectionValue)、定性结果(inspectionResult)、单位(inspResultUnitCode)、结果说明(inspResultDesc)、扩展结果(inspExtraResult) - 用于判断数值、阴阳性、定性描述。
      [异常字段] 异常标志(inspAbnoFlag)、参考范围(inspResultRange) - 用于判断“偏高/升高/偏低/降低/异常/高于参考范围/低于参考范围”。

      判断规则：
      - 项目匹配必须优先依据化验项目描述或缩写；不要从无关结果文本推断项目。
      - “偏高/升高/高于参考范围”优先看异常标志是否为↑、H、高、偏高；也可结合结果数值和参考范围判断。
      - “偏低/降低/低于参考范围”优先看异常标志是否为↓、L、低、偏低；也可结合结果数值和参考范围判断。
      - “>15×10⁹/L”等数值条件应使用结果字段的数值与阈值比较；单位只作为上下文，不做不可靠的跨量纲换算。
      - 若查询含时间窗口，使用检测日期时间与手术/入院/出院等锚点比较。
    keep_fields: ["inspectionId","hdcInspRptId","inspRptId","inspItemCode","inspItemDesc","inspectionValue","inspResultUnitCode","inspectionResult","inspResultDesc","inspAbnoFlag","inspResultRange","inspectionMethod","inspectionEquipment","inspDocCode","inspDocName","inspResultSeqNo","inspectionDate","inspectionTime","bacteriumQuantity","inspResultRemarks","inspItemAbbr","inspRptMcorgFlag","hdcOrdId","hosOrdId","businessFieldCode","businessFieldDesc","inspItemNumber","inspExtraResult"]
    merge:
      - name: "检测日期时间"
        fields: ["inspectionDate", "inspectionTime"]
        sep: " "
---

# 检验指标查询

调用外部检验结果接口，查询患者全部检验/化验项目结果。

## 入参

- global_patient_id: 全局患者ID
- global_visit_id: 全局就诊号
- businessFieldCode: 从global_visit_id截取（下划线前部分）

## 返回字段

inspectionId(全局样本号), hdcInspRptId(全局检验报告号), inspRptId(检验报告号), inspItemCode(化验项目代码), inspItemDesc(化验项目描述), inspectionValue(结果), inspResultUnitCode(单位), inspectionResult(定性结果), inspResultDesc(结果说明), inspAbnoFlag(异常标志), inspResultRange(参考范围), inspectionMethod(检测方法), inspectionEquipment(检测仪器), inspDocCode(检测人代码), inspDocName(检测人名字), inspResultSeqNo(显示序号), inspectionDate(检测日期), inspectionTime(检测时间), bacteriumQuantity(细菌计数), inspResultRemarks(备注), inspItemAbbr(缩写), inspRptMcorgFlag(是否微生物报告), hdcOrdId(平台医嘱明细id), hosOrdId(His医嘱明细id), businessFieldCode(院区代码), businessFieldDesc(院区描述), inspItemNumber(检测项目数量), inspExtraResult(扩展结果)

## 判断规则

1. 项目匹配：优先看化验项目描述和缩写。白细胞计数可以匹配“白细胞计数”“白细胞”“WBC”等项目描述或缩写。
2. 时间筛选：使用检测日期时间字段，与手术、入院、出院等锚点进行时间窗口比较。
3. 异常判断：偏高/升高/高于参考范围看异常标志、结果、参考范围；偏低/降低/低于参考范围同理。
4. 数值比较：从结果字段提取数值，与查询阈值比较；只在单位语义兼容时采信。
5. 定性判断：阳性/阴性/培养结果等优先看定性结果、结果说明和扩展结果。
