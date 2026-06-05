"""
复杂医学推理测试
================
使用真实病历文档进行复杂推理能力测试

复杂测试类型：
1. 多条件组合判断
2. 时序推理
3. 批量判断
4. 对比判断
5. 排除/筛选类
6. 嵌套推理
7. 边界条件
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from microharness.ollama import OllamaClient
from microharness.ollama.prompts import format_judge_prompt
from microharness.rag.document_parser import parse_document


# ──────────────────────── 配置 ────────────────────────

RECORDS_DIR = r"C:\Users\Administrator\Desktop\文档下载2026-5-22 17_33_21"


# ──────────────────────── 复杂测试用例 ────────────────────────

COMPLEX_TEST_CASES = [
    # ========== 1. 多条件组合判断 ==========
    (
        "3x6606234x1(出院记录_3_789_16).html",
        "出院记录-乳腺癌肝转移",
        "多条件组合",
        [
            # AND条件 - 两个条件同时满足
            ("乳腺癌且肝转移的患者", "符合", "明确乳腺癌且肝转移"),
            ("癌症转移且需要靶向治疗", "符合", "肝转移且靶向维持治疗中"),
            ("年龄超过50岁且患有癌症", "符合", "57岁+乳腺癌"),
            ("非乳腺癌且无转移", "不符合", "该患者是乳腺癌转移"),

            # OR条件 - 满足任一即可
            ("乳腺癌或肝癌患者", "符合", "乳腺癌确诊"),
            ("需要化疗或靶向治疗", "符合", "靶向治疗中"),

            # NOT条件 - 排除某类
            ("无糖尿病的癌症患者", "符合", "无糖尿病诊断"),
            ("非手术患者", "不符合", "住院期间行椎体手术"),
        ]
    ),

    # ========== 2. 时序推理 ==========
    (
        "3x6606234x1(出院记录_3_789_16).html",
        "出院记录-乳腺癌肝转移",
        "时序推理",
        [
            # 时间顺序判断
            ("2024年9月之前发现乳腺肿块", "符合", "2023年11月发现"),
            ("先发现肿瘤后发生骨折", "符合", "时间线明确"),

            # 时间间隔推理
            ("发现乳腺肿块1年后出现骨转移", "符合", "2023.11→2024.9约1年"),
            ("短期内(3个月内)完成手术和化疗", "不符合", "手术是2024年9月，后续化疗时间不明确"),

            # 治疗阶段判断
            ("处于化疗后康复阶段", "符合", "化疗后靶向维持"),
            ("新确诊未开始治疗", "不符合", "已确诊且在治疗中"),
        ]
    ),

    # ========== 3. 批量判断 ==========
    (
        "3x6606234x1(出院记录_3_789_16).html",
        "出院记录-乳腺癌肝转移",
        "批量判断",
        [
            # 一个问题含多个判断点
            ("该患者的主要诊断、手术操作和住院天数分别是？", "符合",
             "诊断:左乳癌肝转移; 手术:椎体手术; 住院:2天"),

            ("该患者是否患有恶性肿瘤？是否做过手术？住院多久？", "符合",
             "三个问题: 恶性肿瘤(是)、手术(是)、住院天数(2天)"),
        ]
    ),

    # ========== 4. 对比判断 ==========
    (
        "3x6606234x1(出院记录_3_789_16).html",
        "出院记录-乳腺癌肝转移",
        "对比判断",
        [
            # 与标准对比
            ("该患者住院天数是否超过全国平均住院日(7天)？", "不符合", "住院仅2天"),

            # 与疾病严重程度标准对比
            ("根据转移情况，该患者肿瘤分期属于晚期吗？", "符合", "肝转移属晚期肿瘤"),
        ]
    ),

    # ========== 5. 排除/筛选类 ==========
    (
        "3x6606234x1(出院记录_3_789_16).html",
        "出院记录-乳腺癌肝转移",
        "排除筛选",
        [
            # 排除特定类型
            ("非心脑血管疾病患者", "符合", "无心脑血管诊断"),
            ("无肾脏疾病", "符合", "无肾脏相关诊断"),

            # 筛选某特征
            ("住院时间短于5天的患者", "符合", "住院2天<5天"),
            ("不需要长期住院的患者", "符合", "住院仅2天"),

            # 排除知情类文档
            ("排除授权委托书，只看医疗记录", "不符合",
             "这是出院记录，不是委托书"),
        ]
    ),

    # ========== 6. 嵌套/条件推理 ==========
    (
        "3x6606234x1(出院记录_3_789_16).html",
        "出院记录-乳腺癌肝转移",
        "条件推理",
        [
            # IF-THEN类推理
            ("如果患者有肝转移且住院超过7天，需要重点关注肝功能，该患者是否需要？", "不符合",
             "住院仅2天，不满足住院>7天的条件"),

            ("癌症患者出现病理性骨折提示骨转移可能，该患者是否有骨转移证据？", "符合",
             "胸椎12病理性骨折，提示骨转移"),

            # 多层嵌套
            ("对于化疗后的癌症患者，如果出现靶向治疗耐药需要换药，该患者是否需要换药？", "不确定",
             "病历未提及耐药，靶向维持治疗中表明有效"),

            # 风险评估类
            ("该患者是否存在多发转移的高危因素？", "符合", "已有多发转移"),
        ]
    ),

    # ========== 7. 边界条件测试 ==========
    (
        "3x6606234x1(出院记录_3_789_16).html",
        "出院记录-乳腺癌肝转移",
        "边界条件",
        [
            # 边界数值
            ("住院天数是否恰好为2天？", "符合", "明确2天"),
            ("是否超过50岁但不超过60岁？", "符合", "57岁"),

            # 模糊条件
            ("住院时间较短的患者", "符合", "2天属较短"),
            ("高龄癌症患者", "符合", "57岁属高龄"),

            # 不确定边界
            ("病情严重程度为中等", "不确定", "无法量化判断"),
        ]
    ),
]


# ──────────────────────── 辅助函数 ────────────────────────

def load_record(filepath: Path) -> str:
    """加载并解析病历文件"""
    content = filepath.read_text(encoding="utf-8")
    return parse_document(content.encode(), filepath.name)


def safe_print(text):
    try:
        print(text)
    except:
        print(str(text)[:200])


# ──────────────────────── 测试函数 ────────────────────────

def run_tests(model=None):
    if model is None:
        model = "llama3:8b"
        # model = "qwen2:1.5b"
        # model = "qwen2.5:7b"
        # model = "qwen2:7b-instruct"
    client = OllamaClient(model=model)

    if not client.is_available():
        safe_print("错误: Ollama 服务未启动")
        return

    safe_print("=" * 70)
    safe_print("复杂医学推理测试 (真实病历)")
    safe_print("=" * 70)
    safe_print(f"病历目录: {RECORDS_DIR}")
    safe_print(f"模型: {client.model}")
    safe_print("=" * 70)

    records_path = Path(RECORDS_DIR)
    total_stats = {"passed": 0, "failed": 0, "uncertain": 0}

    for filename, record_desc, test_type, test_conditions in COMPLEX_TEST_CASES:
        filepath = records_path / filename
        if not filepath.exists():
            safe_print(f"\n[SKIP] 文件不存在: {filename}")
            continue

        safe_print(f"\n{'='*70}")
        safe_print(f"[{test_type}] {record_desc}")
        safe_print("-" * 70)

        # 加载病历
        record_content = load_record(filepath)
        safe_print(f"病历长度: {len(record_content)} 字符")

        type_stats = {"passed": 0, "failed": 0, "uncertain": 0}

        for condition, expected, reason in test_conditions:
            safe_print(f"\n  条件: {condition}")
            safe_print(f"  预期: {expected} - {reason}")

            system_prompt, user_prompt = format_judge_prompt(condition, record_content)

            try:
                response = client.chat(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=0.1
                )
            except Exception as e:
                response = f"[ERROR: {str(e)}]"

            actual = "符合" if ("符合" in response and "不符合" not in response) else "不符合"

            if expected == "不确定":
                status = "[?]"
                type_stats["uncertain"] += 1
                total_stats["uncertain"] += 1
            elif actual == expected:
                status = "[PASS]"
                type_stats["passed"] += 1
                total_stats["passed"] += 1
            else:
                status = "[FAIL]"
                type_stats["failed"] += 1
                total_stats["failed"] += 1

            safe_print(f"  LLM回答: {response}")
            safe_print(f"  结果: {status} 实际={actual} 预期={expected}")

        # 分类统计
        total = type_stats["passed"] + type_stats["failed"]
        pass_rate = type_stats["passed"] / total * 100 if total > 0 else 0
        safe_print(f"\n  [{test_type}] 通过率: {pass_rate:.0f}% ({type_stats['passed']}/{total})")

    # 汇总
    total = total_stats["passed"] + total_stats["failed"]
    total_pass_rate = total_stats["passed"] / total * 100 if total > 0 else 0

    safe_print(f"\n{'='*70}")
    safe_print("汇总")
    safe_print(f"总计: 通过={total_stats['passed']} 失败={total_stats['failed']} 不确定={total_stats['uncertain']}")
    safe_print(f"确定率: {total_pass_rate:.1f}%")
    safe_print("=" * 70)


def main():
    import sys
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen2:7b-instruct"
    run_tests(model=model)


if __name__ == "__main__":
    main()