---
name: medical-disease-section-router
description: 根据疾病/症状/人群查询，自动判断需要调取的病历文档类型及对应章节，支持糖尿病、高血压、手术、过敏等15+疾病类别的精准路由
metadata: {"emoji":"🔬","safety":"AUTO_APPROVE"}
---

# 病历疾病-章节路由 Skill

## 功能
输入自然语言查询（疾病/症状/条件），自动路由到：
- 需要检索的病历文档类型
- 文档内对应章节
- XML 字段路径

## 调用方式
```python
from microharness.medical.query_router import get_router

router = get_router()
result = router.route("找出所有糖尿病患者")
# → {"target_medical_doc": ["入院记录","出院记录","日常病程记录"],
#    "target_sections": ["既往史","出院诊断","入院诊断"],
#    "target_xml_paths": ["pastHistory","dischargeDiagnosis","admissionDiagnosis"],
#    "confidence": 0.95}
```

## 支持的疾病/症状类别（15+）
糖尿病、高血压、冠心病、肺炎、骨折、肿瘤、手术、过敏、住院天数、诊断、用药、检查、年龄、性别等

## 路由策略
- **关键词匹配**（快速通道）：命中映射表直接返回，无需 LLM
- **LLM 泛化**（扩展通道）：未命中时用 LLM 推理相似疾病
