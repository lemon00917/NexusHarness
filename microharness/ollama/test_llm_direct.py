"""
LLM 医学判断能力直接测试
==========================
不经过 RAG，直接测试 LLM 对医学病历的判断能力

用于：
1. 验证模型本身的能力
2. 隔离 RAG 检索问题
3. 快速迭代 Prompt
"""

import sys
from pathlib import Path
import time

sys.path.insert(0, str(Path(__file__).parent.parent))

from microharness.ollama import OllamaClient
from microharness.ollama.prompts import format_judge_prompt


# ──────────────────────── 测试病历 ────────────────────────

MEDICAL_RECORDS = {
    "diabetes_1": """
    患者：王小明，男性，58岁
    诊断：2型糖尿病
    入院原因：口干、多饮、多尿1周
    既往史：糖尿病病史5年，口服二甲双胍控制
    检查结果：
    - 空腹血糖：8.5 mmol/L（正常值3.9-6.1）
    - 餐后2小时血糖：14.2 mmol/L（正常值<7.8）
    - 糖化血红蛋白：8.2%
    治疗方案：继续口服二甲双胍联合阿卡波糖
    """,

    "diabetes_2": """
    患者：李小红，女性，62岁
    诊断：2型糖尿病性酮症
    入院原因：恶心呕吐3天，呼吸深快1天
    既往史：糖尿病病史10年，注射胰岛素
    检查结果：
    - 随机血糖：19.8 mmol/L
    - 尿酮体：（++）
    - 血气分析：pH 7.28
    治疗方案：胰岛素泵持续注射，补液纠酸
    """,

    "breast_cancer": """
    患者：张美丽，女性，45岁
    诊断：右侧乳腺癌（IIA期）
    入院原因：发现右乳肿物2个月
    既往史：无特殊
    检查结果：
    - 乳腺钼靶：右乳外上象限占位，BI-RADS 4C
    - 病理：浸润性导管癌，ER(+)，PR(+)，HER2(-)
    - 肿瘤标志物：CA-153 28.5 U/mL（偏高）
    治疗方案：右乳癌改良根治术，术后化疗
    """,

    "surgery": """
    患者：刘强壮，男性，55岁
    诊断：腰椎间盘突出症
    入院原因：腰痛伴左下肢放射痛3个月
    手术记录：
    - 手术名称：L4/5椎间盘摘除术+椎间融合器植骨融合术
    - 麻醉方式：全麻
    - 手术时间：2024-03-15 10:30-13:45
    - 术中出血：200ml
    - 手术过程顺利
    术后诊断：L4/5椎间盘突出症
    """,

    "liver_abnormal": """
    患者：赵沉默，男性，48岁
    诊断：酒精性肝病
    入院原因：体检发现肝功能异常2周
    既往史：饮酒史20年，每天白酒约250ml
    检查结果：
    - ALT：86 U/L（正常值9-50）
    - AST：102 U/L（正常值15-40）
    - AST/ALT > 1
    - 腹部彩超：脂肪肝表现
    治疗方案：戒酒，保肝降酶
    """,

    "normal": """
    患者：周平安，男性，35岁
    诊断：急性支气管炎
    入院原因：咳嗽咳痰3天，发热1天
    既往史：体健
    检查结果：
    - 体温：38.2°C
    - 血常规：白细胞11.2×10^9/L
    - 胸片：双肺纹理增粗
    治疗方案：抗感染、止咳化痰对症治疗
    住院天数：3天
    """,
}


# ──────────────────────── 测试用例 ────────────────────────

TEST_CASES = [
    ("diabetes_1", "血糖大于7.0的糖尿病患者", "符合", "空腹血糖8.5明确>7.0"),
    ("diabetes_2", "血糖大于7.0的糖尿病患者", "符合", "随机血糖19.8明确>7.0"),
    ("breast_cancer", "乳腺癌患者", "符合", "诊断明确为乳腺癌"),
    ("surgery", "做过手术的患者", "符合", "有病历显示手术"),
    ("liver_abnormal", "肝功能异常的患者", "符合", "ALT/AST明显升高"),
    ("normal", "血糖大于7.0的糖尿病患者", "不符合", "血糖正常，无糖尿病诊断"),
    ("normal", "乳腺癌患者", "不符合", "性别男，无乳腺癌诊断"),
]


# ──────────────────────── 测试函数 ────────────────────────

def run_single_judgment(
    client: OllamaClient,
    record_key: str,
    condition: str,
    expected: str,
    reason: str
) -> dict:
    """
    测试单条 LLM 判断

    Returns:
        dict with keys: record_key, condition, expected, actual, correct, response, time
    """
    record_content = MEDICAL_RECORDS[record_key]
    system_prompt, user_prompt = format_judge_prompt(condition, record_content)

    start_time = time.time()
    response = client.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.1
    )
    elapsed = time.time() - start_time

    actual = "符合" if ("符合" in response and "不符合" not in response) else "不符合"
    correct = (actual == expected)

    return {
        "record_key": record_key,
        "condition": condition,
        "expected": expected,
        "actual": actual,
        "correct": correct,
        "response": response,
        "time": elapsed,
        "reason": reason,
    }


def run_direct_tests(model: str = "qwen2:1.5b"):
    """运行直接测试"""
    client = OllamaClient(model=model)

    if not client.is_available():
        print("错误: Ollama 服务未启动")
        return

    print("=" * 60)
    print("LLM 医学判断能力直接测试")
    print("=" * 60)
    print(f"模型: {model}")
    print(f"测试用例: {len(TEST_CASES)}")
    print("=" * 60)

    results = []
    for record_key, condition, expected, reason in TEST_CASES:
        result = run_single_judgment(client, record_key, condition, expected, reason)
        results.append(result)

        status = "[OK]" if result["correct"] else "[FAIL]"
        print(f"\n{status} {record_key}: {condition}")
        print(f"    预期: {expected} | 实际: {result['actual']}")
        print(f"    原因: {reason}")
        print(f"    耗时: {result['time']:.2f}s")
        print(f"    回答: {result['response'][:100]}...")

    # 统计
    print("\n" + "=" * 60)
    print("统计结果")
    print("=" * 60)

    correct_count = sum(1 for r in results if r["correct"])
    total = len(results)
    pass_rate = correct_count / total * 100

    print(f"通过: {correct_count}/{total}")
    print(f"通过率: {pass_rate:.1f}%")

    # 按病历分类统计
    print("\n分类统计:")
    categories = {}
    for r in results:
        cat = r["record_key"]
        if cat not in categories:
            categories[cat] = {"correct": 0, "total": 0}
        categories[cat]["total"] += 1
        if r["correct"]:
            categories[cat]["correct"] += 1

    for cat, stats in categories.items():
        rate = stats["correct"] / stats["total"] * 100
        status = "[OK]" if rate >= 70 else "[FAIL]"
        print(f"  {status} {cat}: {rate:.0f}% ({stats['correct']}/{stats['total']})")

    # 错误案例分析
    print("\n错误案例分析:")
    for r in results:
        if not r["correct"]:
            print(f"  - {r['record_key']} + '{r['condition']}'")
            print(f"    预期:{r['expected']} 实际:{r['actual']}")
            print(f"    回答: {r['response'][:200]}")


def test_prompt_variations(model: str = "qwen2:1.5b"):
    """测试不同的 Prompt 变体"""
    client = OllamaClient(model=model)

    if not client.is_available():
        print("错误: Ollama 服务未启动")
        return

    print("\n" + "=" * 60)
    print("Prompt 变体测试")
    print("=" * 60)

    # 用同一个病历测试不同问法
    record_key = "diabetes_1"
    record_content = MEDICAL_RECORDS[record_key]
    expected = "符合"

    # 不同的问法
    conditions = [
        "血糖大于7.0的糖尿病患者",
        "空腹血糖>7.0",
        "糖尿病患者",
        "血糖高",
        "这个患者有糖尿病吗",
        "判断该患者是否符合：糖尿病诊断且血糖>7.0",
    ]

    print(f"\n病历: {record_key}")
    print(f"预期: {expected}")
    print("-" * 60)

    for condition in conditions:
        system_prompt, user_prompt = format_judge_prompt(condition, record_content)

        response = client.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.1
        )

        actual = "符合" if ("符合" in response and "不符合" not in response) else "不符合"
        correct = (actual == expected)
        status = "[OK]" if correct else "[FAIL]"

        print(f"\n{status} 问法: {condition}")
        print(f"    回答: {response[:100]}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="LLM 医学判断能力直接测试")
    parser.add_argument("--model", "-m",
                        default="qwen2:1.5b",
                        help="模型名称")
    parser.add_argument("--prompt-test", "-p",
                        action="store_true",
                        help="测试 Prompt 变体")
    parser.add_argument("--verbose", "-v",
                        action="store_true",
                        help="详细输出")

    args = parser.parse_args()

    if args.prompt_test:
        test_prompt_variations(args.model)
    else:
        run_direct_tests(args.model)


if __name__ == "__main__":
    main()