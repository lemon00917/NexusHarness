# NexusHarness 问题追踪文档

## 已修复问题 (Fixed)

### P0 严重问题

| 状态 | 问题 | 文件 | 修复内容 |
|------|------|------|----------|
| ✅ | 重复 LLM 定义 | `microharness/agent/harness.py` | 删除第58行重复代码 |
| ✅ | 嵌套方法死代码 | `microharness/memory/session_manager.py` | 删除定义在函数内部的死代码 |
| ✅ | 导入缺失 | `web/app.py` | 添加 `from microharness.agent.retry import get_retry_executor` |
| ✅ | 编码问题 | `microharness/skills/skill_manager.py` | 添加 `encoding="utf-8", errors="replace"` |
| ✅ | skills 路径错误 | `microharness/skills/skill_manager.py` | 修正路径 `parent.parent.parent / "skills"` |
| ✅ | .env 路径错误 | `microharness/config/config.py` | 修正路径 `parent.parent.parent / ".env"` |

---

## 待修复问题 (Pending)

### P0 严重问题

| 状态 | 问题 | 文件 | 说明 |
|------|------|------|------|
| ✅ | Guard 对 Skill 参数缺乏危险检测 | `microharness/agent/guard.py` | 已增强关键词列表，新增 curl, wget, 命令链等检测 |

### P1 中等问题

| 状态 | 问题 | 文件 | 说明 |
|------|------|------|------|
| × | RAG 混合搜索 chunk/doc 映射错误 | `microharness/rag/rag.py:192-235` | BM25 与向量分数组合时 doc_id 可能错位 |
| × | 全局 config 污染 | `microharness/observability/evaluation.py` | 多线程环境会产生竞态条件 |
| × | Web 全局状态竞态 | `web/app.py` | `pending_approvals`, `approval_results`, `disabled_skills` 无锁保护 |
| × | Audit 日志无限增长 | `microharness/observability/audit.py` | 所有会话追加到同一文件，无轮转 |
| × | Web 模式长期记忆不完整 | `microharness/memory/memory.py` / `web/app.py` | `extract_and_save_memory` 未在 Web 模式被调用 |

### P2/P3 一般问题

| 状态 | 问题 | 文件 | 说明 |
|------|------|------|------|
| × | `build_harness()` 死代码 | `microharness/agent/harness.py` | CLI 和 Web 都没用这个函数 |
| × | 循环依赖风险 | `microharness/config/prompts.py` | 导入 `web.app.disabled_skills` |
| × | 硬编码定价 | `microharness/observability/token_tracker.py` | 模型定价不完整 |
| × | Skill 重载不生效 | `microharness/skills/skill_manager.py` | `_loaded` 标志不清除 |
| × | 文档路径不一致 | 多个文件 | 有的用 `Path`，有的用 `os.path` |

---

## 问题优先级说明

- **P0 (Critical)**: 必须立即修复，影响功能正确性或安全性
- **P1 (Major)**: 重要问题，影响稳定性或正确性
- **P2 (Minor)**: 一般问题，影响可维护性
- **P3 (Trivial)**: 小问题，不影响功能

---

## 图标说明

| 图标 | 含义 |
|------|------|
| ✅ | 已修复 |
| ⚠️ | 待修复（重要/高优先级） |
| × | 待修复（一般/低优先级） |

---

## 统计

| 类别 | 数量 |
|------|------|
| 已修复 | 7 |
| 待修复（高优先级） | 0 |
| 待修复（一般） | 5 |
| 总计 | 12 |

---

## 更新记录

- **2026-05-24**: 初始记录，标记已修复 7 个问题，待修复 5 个问题