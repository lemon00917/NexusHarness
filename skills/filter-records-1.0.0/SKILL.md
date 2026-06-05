---
name: filter-records
description: 根据自然语言条件筛选病历记录
---
# 病历智能筛选 Skill

## Description
此Skill用于根据用户描述的筛选条件，从病历数据库中智能筛选符合条件的病历记录。

## Use Cases
当用户想从病历库中筛选特定条件的病历时使用，例如：
- "找出所有糖尿病患者"
- "筛选血糖大于7的病人"
- "查找住院超过7天的患者"
- "筛选乳腺癌肝转移的病人"

## Safety Level
AUTO_APPROVE

## Commands

### 筛选病历
```bash
python -c "from microharness.rag.record_filter import RecordFilter; rf = RecordFilter(); results = rf.filter('${condition}'); print(f'找到{len(results)}条记录')"
```