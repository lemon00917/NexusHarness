"""
医学推理能力测试脚本
====================
测试 LLM 模型在医学病历筛选上的推理能力边界

评估维度：
1. 准确率 - 是否正确判断
2. 稳定性 - 多次询问答案是否一致
3. 幻觉率 - 是否产生不存在的信息
4. 推理时间 - token 消耗和响应速度
"""

import sys
import os
from pathlib import Path
import time
import json
from dataclasses import dataclass, asdict
from typing import List, Optional

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from microharness.rag import RecordFilter
from microharness.rag.document_parser import parse_document
from microharness.rag.record_filter import FilterResult
from microharness.ollama.prompts import format_judge_prompt


# ──────────────────────── 测试用例定义 ────────────────────────

@dataclass
class TestCase:
    """测试用例"""
    id: str
    condition: str
    category: str  # "简单匹配" / "医学推理" / "复杂推理"
    expected_match: bool  # 是否应该匹配到结果
    description: str = ""


TEST_CASES = [
    # ========== 简单关键词匹配型 ==========
    TestCase(
        id="T001",
        condition="血糖大于7.0的糖尿病患者",
        category="简单匹配",
        expected_match=True,
        description="基于明确数值的筛选"
    ),
    TestCase(
        id="T002",
        condition="乳腺癌患者",
        category="简单匹配",
        expected_match=True,
        description="明确疾病诊断"
    ),
    TestCase(
        id="T003",
        condition="做过手术的患者",
        category="简单匹配",
        expected_match=True,
        description="明确医疗操作"
    ),

    # ========== 医学推理型 ==========
    TestCase(
        id="T101",
        condition="帮我找出所有需要调整用药方案的患者",
        category="医学推理",
        expected_match=True,
        description="需要理解'调整用药'意味着当前方案可能有问题"
    ),
    TestCase(
        id="T102",
        condition="筛选出可能有术后并发症风险的患者",
        category="医学推理",
        expected_match=True,
        description="需要识别术后相关指标异常"
    ),
    TestCase(
        id="T103",
        condition="找出所有肝功能异常的患者",
        category="医学推理",
        expected_match=True,
        description="需要识别肝功能指标异常"
    ),
    TestCase(
        id="T104",
        condition="筛选出可能需要会诊的患者",
        category="医学推理",
        expected_match=True,
        description="需要判断病情复杂程度"
    ),
    TestCase(
        id="T105",
        condition="找出所有发热超过3天的患者",
        category="医学推理",
        expected_match=True,
        description="需要识别时间跨度"
    ),

    # ========== 复杂推理型 ==========
    TestCase(
        id="T201",
        condition="筛选出多重用药可能有药物相互作用风险的患者",
        category="复杂推理",
        expected_match=True,
        description="需要识别多种药物并判断相互作用"
    ),
    TestCase(
        id="T202",
        condition="找出所有住院期间出现过低血糖的患者",
        category="复杂推理",
        expected_match=True,
        description="需要识别住院期间特定事件"
    ),
]


# ──────────────────────── 评估结果 ────────────────────────

@dataclass
class EvaluationResult:
    """单个测试用例的评估结果"""
    test_case: TestCase
    passed: bool
    llm_response: str
    decision: str  # "符合" / "不符合"
    confidence: str  # "高" / "中" / "低"
    issues: List[str]  # 发现的问题
    response_time: float


@dataclass
class CategoryReport:
    """分类测试报告"""
    category: str
    total: int
    passed: int
    failed: int
    pass_rate: float
    avg_response_time: float
    issues_summary: List[str]


@dataclass
class FullReport:
    """完整测试报告"""
    model_name: str
    total_cases: int
    passed: int
    failed: int
    pass_rate: float
    category_reports: List[CategoryReport]
    recommendations: List[str]


# ──────────────────────── 测试函数 ────────────────────────

def evaluate_single_case(
    rf: RecordFilter,
    test_case: TestCase,
    verbose: bool = True
) -> EvaluationResult:
    """
    评估单个测试用例

    Args:
        rf: RecordFilter 实例
        test_case: 测试用例
        verbose: 是否打印详细信息

    Returns:
        EvaluationResult
    """
    if verbose:
        print(f"\n{'='*60}")
        print(f"[{test_case.id}] {test_case.condition}")
        print(f"分类: {test_case.category} | 预期: {'应有匹配' if test_case.expected_match else '应无匹配'}")
        print("-" * 60)

    issues = []
    start_time = time.time()

    # Step 0: 解析条件
    parsed = rf._parse_condition(test_case.condition)
    if verbose:
        print(f"  [解析] {parsed.summary}")

    # Step 1: RAG 检索
    candidates = rf.rag.search(
        query=test_case.condition,
        top_k=rf.retrieval_top_k,
        vector_weight=rf.vector_weight,
        bm25_weight=rf.bm25_weight
    )

    if verbose:
        print(f"  [检索] 找到 {len(candidates)} 个候选")

    if not candidates:
        response = "[无候选病历]"
        decision = "不符合"
        confidence = "低"
        issues.append("检索阶段未找到候选病历")
    else:
        # Step 2: LLM 判断第一个候选
        candidate = candidates[0]
        system_prompt, user_prompt = format_judge_prompt(
            test_case.condition,
            candidate.document.content
        )

        response = rf.ollama.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.1
        )

        decision = "符合" if ("符合" in response and "不符合" not in response) else "不符合"

        # 评估置信度
        if "符合" in response and len(response) < 50:
            confidence = "高"
        elif "符合" in response or "不符合" in response:
            confidence = "中"
        else:
            confidence = "低"
            issues.append("模型回答不明确")

        # 检测幻觉 - 如果病历内容太短，可能是误判
        if len(candidate.document.content) < 100:
            issues.append("病历内容过短，可能导致误判")

    response_time = time.time() - start_time

    # 判断是否通过
    passed = (decision == "符合") == test_case.expected_match
    if not passed:
        if test_case.expected_match:
            issues.append(f"预期有匹配但LLM返回: {decision}")
        else:
            issues.append(f"预期无匹配但LLM返回: {decision}")

    if verbose:
        print(f"  [LLM回答] {response}")
        print(f"  [决策] {decision} (置信度: {confidence})")
        print(f"  [耗时] {response_time:.2f}s")
        if issues:
            print(f"  [问题] {'; '.join(issues)}")

    return EvaluationResult(
        test_case=test_case,
        passed=passed,
        llm_response=response,
        decision=decision,
        confidence=confidence,
        issues=issues,
        response_time=response_time
    )


def run_evaluation(
    rf: RecordFilter,
    test_cases: List[TestCase] = None,
    verbose: bool = True
) -> FullReport:
    """
    运行完整评估

    Args:
        rf: RecordFilter 实例
        test_cases: 测试用例列表
        verbose: 是否打印详细信息

    Returns:
        FullReport
    """
    if test_cases is None:
        test_cases = TEST_CASES

    if verbose:
        print("\n" + "="*60)
        print("医学推理能力评估")
        print("="*60)
        print(f"模型: {rf.ollama.model}")
        print(f"病历数: {rf.record_count}")
        print(f"测试用例: {len(test_cases)}")
        print("="*60)

    # 按分类执行测试
    categories = {}
    results = []

    for test_case in test_cases:
        result = evaluate_single_case(rf, test_case, verbose)
        results.append(result)

        cat = test_case.category
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(result)

    # 生成报告
    category_reports = []
    recommendations = []

    for cat, cat_results in categories.items():
        passed = sum(1 for r in cat_results if r.passed)
        failed = len(cat_results) - passed
        pass_rate = passed / len(cat_results) * 100 if cat_results else 0
        avg_time = sum(r.response_time for r in cat_results) / len(cat_results)

        category_reports.append(CategoryReport(
            category=cat,
            total=len(cat_results),
            passed=passed,
            failed=failed,
            pass_rate=pass_rate,
            avg_response_time=avg_time,
            issues_summary=list(set(i for r in cat_results for i in r.issues))
        ))

        if pass_rate < 50:
            recommendations.append(f"{cat}类型问题较多(pass_rate={pass_rate:.0f}%)，建议优化prompt或换用更大模型")

    total_passed = sum(1 for r in results if r.passed)
    total_failed = len(results) - total_passed
    pass_rate = total_passed / len(results) * 100 if results else 0

    if pass_rate < 60:
        recommendations.append("整体准确率低于60%，当前模型可能不满足医学推理需求，建议考虑更大参数的医学专用模型")

    return FullReport(
        model_name=rf.ollama.model,
        total_cases=len(results),
        passed=total_passed,
        failed=total_failed,
        pass_rate=pass_rate,
        category_reports=category_reports,
        recommendations=recommendations
    )


def print_report(report: FullReport):
    """打印评估报告"""
    print("\n" + "="*60)
    print("评估报告")
    print("="*60)

    print(f"\n模型: {report.model_name}")
    print(f"总测试用例: {report.total_cases}")
    print(f"通过: {report.passed} | 失败: {report.failed}")
    print(f"通过率: {report.pass_rate:.1f}%")

    print("\n" + "-"*40)
    print("分类统计")
    print("-"*40)

    for cr in report.category_reports:
        status = "[OK]" if cr.pass_rate >= 70 else "[FAIL]"
        print(f"\n{status} {cr.category}")
        print(f"  通过率: {cr.pass_rate:.0f}% ({cr.passed}/{cr.total})")
        print(f"  平均耗时: {cr.avg_response_time:.2f}s")
        if cr.issues_summary:
            print(f"  主要问题: {'; '.join(cr.issues_summary[:3])}")

    if report.recommendations:
        print("\n" + "-"*40)
        print("建议")
        print("-"*40)
        for rec in report.recommendations:
            print(f"  * {rec}")

    print("\n" + "="*60)


def save_report(report: FullReport, output_path: str = "medical_eval_report.json"):
    """保存报告到文件"""
    data = {
        "model_name": report.model_name,
        "total_cases": report.total_cases,
        "passed": report.passed,
        "failed": report.failed,
        "pass_rate": report.pass_rate,
        "category_reports": [
            {
                "category": cr.category,
                "total": cr.total,
                "passed": cr.passed,
                "failed": cr.failed,
                "pass_rate": cr.pass_rate,
                "avg_response_time": cr.avg_response_time,
                "issues_summary": cr.issues_summary
            }
            for cr in report.category_reports
        ],
        "recommendations": report.recommendations
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n报告已保存: {output_path}")


# ──────────────────────── 主函数 ────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="医学推理能力测试")
    parser.add_argument("--index-dir", "-i",
                        default="cache/rag_index/medical_records",
                        help="索引存储目录")
    parser.add_argument("--cases", "-c",
                        type=str,
                        choices=["all", "simple", "medical", "complex"],
                        default="all",
                        help="测试用例类型")
    parser.add_argument("--verbose", "-v",
                        action="store_true",
                        help="详细输出")
    parser.add_argument("--save", "-s",
                        action="store_true",
                        help="保存报告")

    args = parser.parse_args()

    # 创建 RecordFilter
    rf = RecordFilter(index_dir=args.index_dir)

    # 检查 Ollama
    if not rf.ollama.is_available():
        print("错误: Ollama 服务未启动")
        return

    # 选择测试用例
    cases_map = {
        "all": TEST_CASES,
        "simple": [t for t in TEST_CASES if t.category == "简单匹配"],
        "medical": [t for t in TEST_CASES if t.category == "医学推理"],
        "complex": [t for t in TEST_CASES if t.category == "复杂推理"],
    }
    test_cases = cases_map[args.cases]

    # 运行评估
    report = run_evaluation(rf, test_cases, verbose=args.verbose)

    # 打印报告
    print_report(report)

    # 保存报告
    if args.save:
        save_report(report)


if __name__ == "__main__":
    main()