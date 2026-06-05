# -*- coding: utf-8 -*-
"""
病历智能筛选批量判断测试
========================
对比 qwen2.5:7b 和 llama3:8b 在批量判断模式下的表现
"""

import requests
import json
import time
import sys
from datetime import datetime

RECORDS_DIR = r"C:\Users\Administrator\Desktop\文档下载2026-5-22 17_33_21"
API_URL = "http://localhost:8000"

# 测试用例 (condition, expected, reason)
TEST_CASES = [
    # 1. 多条件组合判断
    ("乳腺癌且肝转移的患者", "符合", "明确乳腺癌且肝转移"),
    ("癌症转移且需要靶向治疗", "符合", "肝转移且靶向维持治疗中"),
    ("年龄超过50岁且患有癌症", "符合", "57岁+乳腺癌"),
    ("非乳腺癌且无转移", "不符合", "该患者是乳腺癌转移"),
    ("乳腺癌或肝癌患者", "符合", "乳腺癌确诊"),
    ("需要化疗或靶向治疗", "符合", "靶向治疗中"),
    ("无糖尿病的癌症患者", "符合", "无糖尿病诊断"),
    ("非手术患者", "不符合", "住院期间行椎体手术"),

    # 2. 时序推理
    ("2024年9月之前发现乳腺肿块", "符合", "2023年11月发现"),
    ("先发现肿瘤后发生骨折", "符合", "时间线明确"),
    ("发现乳腺肿块1年后出现骨转移", "符合", "2023.11→2024.9约1年"),
    ("短期内(3个月内)完成手术和化疗", "不符合", "手术是2024年9月，后续化疗时间不明确"),
    ("处于化疗后康复阶段", "符合", "化疗后靶向维持"),
    ("新确诊未开始治疗", "不符合", "已确诊且在治疗中"),

    # 3. 批量判断
    ("该患者的主要诊断、手术操作和住院天数分别是？", "符合", "诊断:左乳癌肝转移; 手术:椎体手术; 住院:2天"),
    ("该患者是否患有恶性肿瘤？是否做过手术？住院多久？", "符合", "三个问题: 恶性肿瘤(是)、手术(是)、住院天数(2天)"),

    # 4. 对比判断
    ("该患者住院天数是否超过全国平均住院日(7天)？", "不符合", "住院仅2天"),
    ("根据转移情况，该患者肿瘤分期属于晚期吗？", "符合", "肝转移属晚期肿瘤"),

    # 5. 排除/筛选类
    ("非心脑血管疾病患者", "符合", "无心脑血管诊断"),
    ("无肾脏疾病", "符合", "无肾脏相关诊断"),
    ("住院时间短于5天的患者", "符合", "住院2天<5天"),
    ("不需要长期住院的患者", "符合", "住院仅2天"),

    # 6. 嵌套/条件推理
    ("如果患者有肝转移且住院超过7天，需要重点关注肝功能，该患者是否需要？", "不符合", "住院仅2天，不满足住院>7天的条件"),
    ("癌症患者出现病理性骨折提示骨转移可能，该患者是否有骨转移证据？", "符合", "胸椎12病理性骨折，提示骨转移"),
    ("该患者是否存在多发转移的高危因素？", "符合", "已有多发转移"),

    # 7. 边界条件测试
    ("住院天数是否恰好为2天？", "符合", "明确2天"),
    ("是否超过50岁但不超过60岁？", "符合", "57岁"),
    ("住院时间较短的患者", "符合", "2天属较短"),
    ("高龄癌症患者", "符合", "57岁属高龄"),
]


def run_batch_filter(condition, visit_id, model, top_k=20):
    """调用批量判断API"""
    try:
        resp = requests.post(f"{API_URL}/api/rag/filter_batch", json={
            "condition": condition,
            "visit_id": visit_id,
            "model": model,
            "top_k": top_k,
            "score_threshold": 0
        }, timeout=120)
        data = resp.json()
        return data
    except Exception as e:
        return {"error": str(e)}


def eval_result(data, expected):
    """评估判断结果"""
    if data.get("error"):
        return "ERROR", data.get("error", "")

    matched = data.get("matched", False)
    actual = "符合" if matched else "不符合"

    if expected == "符合":
        return ("PASS" if matched else "FAIL"), actual
    else:
        return ("PASS" if not matched else "FAIL"), actual


def run_model_tests(model, visit_id="123"):
    """运行单个模型的全部测试"""
    print(f"\n{'='*70}")
    print(f"模型: {model}")
    print(f"{'='*70}")

    stats = {"passed": 0, "failed": 0, "uncertain": 0, "error": 0}
    results = []

    for i, (condition, expected, reason) in enumerate(TEST_CASES):
        print(f"\n[{i+1}/{len(TEST_CASES)}] 条件: {condition}")
        print(f"  预期: {expected} - {reason}")

        start = time.time()
        data = run_batch_filter(condition, visit_id, model)
        elapsed = time.time() - start

        if data.get("error"):
            print(f"  API错误: {data['error']}")
            stats["error"] += 1
            status = "ERROR"
            actual = "N/A"
        else:
            status, actual = eval_result(data, expected)
            summary = data.get("summary", "")[:60]
            print(f"  实际: {actual} | 耗时: {elapsed:.1f}s")
            print(f"  LLM摘要: {summary}")
            print(f"  结果: [{status}]")

            if status == "PASS":
                stats["passed"] += 1
            elif status == "FAIL":
                stats["failed"] += 1
            else:
                stats["uncertain"] += 1

        results.append({
            "condition": condition,
            "expected": expected,
            "reason": reason,
            "actual": actual,
            "status": status,
            "elapsed": elapsed,
            "summary": data.get("summary", "") if not data.get("error") else data.get("error", "")
        })

        time.sleep(0.5)

    total = len(TEST_CASES)
    pass_rate = stats["passed"] / total * 100 if total > 0 else 0
    print(f"\n{'='*70}")
    print(f"汇总: 通过={stats['passed']} 失败={stats['failed']} 错误={stats['error']}")
    print(f"准确率: {pass_rate:.1f}%")
    print(f"{'='*70}")

    return stats, results


def main():
    models = ["qwen2.5:7b", "llama3:8b"]
    visit_id = "123"
    all_results = {}

    for model in models:
        stats, results = run_model_tests(model, visit_id)
        all_results[model] = {"stats": stats, "results": results}
        time.sleep(2)

    # 打印对比报告
    print(f"\n\n{'#'*70}")
    print("对比报告")
    print(f"{'#'*70}")
    print(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"测试用例: {len(TEST_CASES)} 个")
    print(f"就诊号: {visit_id}")
    print()

    for model in models:
        r = all_results[model]
        s = r["stats"]
        total = len(TEST_CASES)
        pass_rate = s["passed"] / total * 100
        print(f"## {model}")
        print(f"- 通过: {s['passed']}/{total} ({pass_rate:.1f}%)")
        print(f"- 失败: {s['failed']}")
        print(f"- 错误: {s['error']}")
        print()

    print("\n## 详细对比")
    print(f"{'条件':<45} {'预期':<8} {'qwen2.5:7b':<15} {'llama3:8b':<15}")
    print("-" * 75)
    for i, (condition, expected, reason) in enumerate(TEST_CASES):
        r1 = all_results["qwen2.5:7b"]["results"][i]
        r2 = all_results["llama3:8b"]["results"][i]
        r1_status = r1["status"]
        r2_status = r2["status"]
        print(f"{condition:<45} {expected:<8} {r1_status:<15} {r2_status:<15}")

    # 保存报告
    report_file = "test_batch_filter_report.json"
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump({
            "test_time": datetime.now().isoformat(),
            "test_cases_count": len(TEST_CASES),
            "visit_id": visit_id,
            "results": all_results
        }, f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存: {report_file}")


if __name__ == "__main__":
    main()
