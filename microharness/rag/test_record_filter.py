"""
病历筛选测试脚本
================
用于测试本地小模型 + RAG 病历筛选功能
"""

import sys
import os
from pathlib import Path
import time

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from microharness.rag import RecordFilter
from microharness.rag.document_parser import parse_document
from microharness.rag.record_filter import FilterResult
from microharness.ollama.prompts import format_judge_prompt


def index_records(records_dir: str, index_dir: str = "cache/rag_index/medical_records") -> int:
    """
    索引目录下所有病历文档

    Args:
        records_dir: 病历文档目录
        index_dir: 索引存储目录

    Returns:
        索引的文档数量
    """
    records_path = Path(records_dir)
    if not records_path.exists():
        print(f"目录不存在: {records_dir}")
        return 0

    rf = RecordFilter(index_dir=index_dir)

    count = 0
    for html_file in records_path.glob("*.html"):
        try:
            content = html_file.read_text(encoding="utf-8")
            parsed = parse_document(content.encode(), html_file.name)
            rf.add_record(parsed, html_file.name)
            count += 1
            if count % 10 == 0:
                print(f"已索引 {count} 个文档...")
        except Exception as e:
            print(f"索引失败 {html_file.name}: {e}")

    print(f"索引完成: {count} 个文档")
    return count


def test_filter():
    """测试病历筛选功能"""
    rf = RecordFilter(index_dir="rag_index/medical_records")

    # 检查 Ollama 是否可用
    if not rf.ollama.is_available():
        print("=" * 60)
        print("错误: Ollama 服务未启动")
        print("请先运行:")
        print("  1. ollama serve")
        print("  2. ollama pull qwen2:1.5b")
        print("=" * 60)
        return

    print(f"\n当前索引: {rf.record_count} 条病历")

    if rf.record_count == 0:
        print("没有索引病历，请先运行 index_records()")
        return

    # 测试筛选条件
    test_conditions = [
        "血糖大于7.0的糖尿病患者",
        "年龄超过60岁的患者",
        "乳腺癌患者",
        "做过手术的患者",
    ]

    for condition in test_conditions:
        print(f"\n{'='*60}")
        print(f"筛选条件: {condition}")
        print("=" * 60)

        # ===== Step 0: 解析条件 =====
        print(f"\n[Step 0] LLM 解析条件")
        print(f"  输入: {condition}")
        parsed = rf._parse_condition(condition)
        print(f"  解析摘要: {parsed.summary}")
        print(f"  关键词: {parsed.keywords}")

        # ===== Step 1: 构建增强查询 =====
        print(f"\n[Step 1] 构建增强查询")
        enhanced_query = rf._build_enhanced_query(condition, parsed)
        print(f"  增强查询: {enhanced_query}")

        # ===== Step 2: RAG 检索 =====
        print(f"\n[Step 2] RAG 检索")
        start_time = time.time()
        candidates = rf.rag.search(
            query=enhanced_query,
            top_k=rf.retrieval_top_k,
            vector_weight=rf.vector_weight,
            bm25_weight=rf.bm25_weight
        )
        retrieval_time = time.time() - start_time
        print(f"  检索到 {len(candidates)} 个候选")
        print(f"  检索耗时: {retrieval_time:.2f}s")
        for i, c in enumerate(candidates[:5]):
            print(f"    [{i+1}] {c.document.filename} (得分: {c.score:.3f})")

        # ===== Step 3: LLM 判断 =====
        print(f"\n[Step 3] LLM 推理判断")
        judge_start = time.time()
        results = []
        for i, candidate in enumerate(candidates):
            print(f"\n  --- 候选 {i+1}/{len(candidates)}: {candidate.document.filename} ---")
            print(f"  病历摘要: {candidate.document.content[:200]}...")

            # 调用 LLM 判断
            system_prompt, user_prompt = format_judge_prompt(condition, candidate.document.content)
            print(f"\n  [LLM Prompt]")
            print(f"  System: {system_prompt[:100]}...")
            print(f"  User: {user_prompt[:200]}...")

            response = rf.ollama.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.1
            )

            print(f"\n  [LLM 决策]")
            print(f"  模型回答: {response}")
            matched = "符合" in response and "不符合" not in response
            print(f"  决策结果: {'✓ 符合' if matched else '✗ 不符合'}")

            results.append(FilterResult(
                doc_id=candidate.document.doc_id,
                filename=candidate.document.filename,
                content=candidate.document.content[:500],
                score=candidate.score,
                matched=matched,
                reason=None if matched else response
            ))

        judge_time = time.time() - judge_start
        print(f"\n  判断耗时: {judge_time:.2f}s")

        # ===== 最终结果 =====
        matched_results = [r for r in results if r.matched]
        print(f"\n{'='*60}")
        print(f"最终结果: 找到 {len(matched_results)} 条符合条件的病历")
        print("=" * 60)
        for r in matched_results:
            print(f"  ✓ {r.filename} (得分: {r.score:.3f})")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="病历筛选测试")
    parser.add_argument("--records-dir", "-r",
                        default=r"C:\Users\Administrator\Desktop\文档下载2026-5-22 17_33_21",
                        help="病历文档目录")
    parser.add_argument("--index-dir", "-i",
                        default="rag_index/medical_records",
                        help="索引存储目录")
    parser.add_argument("--test-only", "-t",
                        action="store_true",
                        help="仅测试筛选，不重新索引")

    args = parser.parse_args()

    if not args.test_only:
        print(f"正在索引病历文档: {args.records_dir}")
        print(f"索引存储目录: {args.index_dir}")
        index_records(args.records_dir, args.index_dir)

    test_filter()


if __name__ == "__main__":
    main()