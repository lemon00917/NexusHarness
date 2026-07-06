---
name: imaging-query
description: 查询患者影像学检查报告（CT、MR、超声等）
metadata:
  emoji: "🩻"
  safety: AUTO_APPROVE
  triggers: ["CT","MR","MRI","X线","超声","影像","放射","B超"]
  api:
    url: ""
    method: POST
    request_map:
      hdcPatientId: "{{global_patient_id}}"
      hdcEncId: "{{global_visit_id}}"
  returns: "检查部位、影像所见、诊断意见"
---

# 影像报告查询

查询患者影像学检查报告。

## 入参
- global_patient_id: 全局患者ID
- global_visit_id: 全局就诊号
