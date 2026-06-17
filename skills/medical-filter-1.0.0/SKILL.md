---
name: medical-filter
description: 根据自然语言条件智能筛选病历，支持数值比较、文本匹配和复杂语义判断
metadata: {"emoji":"🔬","safety":"AUTO_APPROVE"}
---

# 病历智能筛选 Skill

## Description
根据用户输入的筛选条件，从已上传的患者病历中智能筛选符合条件的记录。

## Use Cases
- "住院小于5天的患者"
- "诊断为糖尿病的患者"
- "做过手术的患者"
- "血糖大于7的患者"
- "主诉包含头痛的患者"

## 实现原理
1. 根据用户问题，通过字段目录（XML模板）路由到目标文档类型和字段
2. 读取患者上传时预绑定的结构化数据（binding.json）
3. 数值条件直接计算比较，复杂语义交由LLM判断
4. 返回匹配的患者列表及具体数据

## Safety Level
AUTO_APPROVE — 此操作仅读取已上传的病历数据，无写操作风险
