# NexusHarness Benchmarks

标准化任务定义目录，用于评估 Agent 质量。

## 目录结构

```
benchmarks/
├── code/           # 代码生成任务
│   ├── fibonacci.json
│   └── hello_world.json
├── tool_use/       # 工具使用任务
│   └── file_ops.json
└── README.md
```

## 任务定义格式

```json
{
  "id": "unique_task_id",
  "category": "code|tool_use|reasoning|regression",
  "task": "任务描述文字",
  "expected_output": "期望输出中的关键内容",
  "validation": {
    "type": "contains|exact|regex|tool_calls|llm_judge|hybrid",
    "value": "...",
    ...
  },
  "metadata": {
    "difficulty": "trivial|easy|medium|hard",
    "expected_steps": 3,
    "description": "任务描述"
  }
}
```

## Validation 类型

| 类型 | 说明 |
|------|------|
| `contains` | 响应包含指定字符串 |
| `exact` | 响应完全匹配 |
| `regex` | 正则表达式匹配 |
| `tool_calls` | 验证工具调用顺序 |
| `llm_judge` | LLM 作为裁判评判 |
| `hybrid` | 组合多种规则 |

## 添加新任务

在对应分类目录下创建 `.json` 文件即可。

## 运行 Benchmark

```bash
# 运行所有 benchmarks
python harness.py benchmark

# 只运行 code 类
python harness.py benchmark --category code

# 指定任务 ID
python harness.py benchmark --tasks fibonacci_001 hello_world_001
```