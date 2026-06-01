"""
基于真实病历的 LLM 医学判断测试
================================
使用桌面目录下的真实病历文档进行测试

病历实际内容分析：
- 3x3055291x1(首次病程记录): 吴秀荣，56岁，脊柱外科，胸背部疼痛入院
- 3x6605341x1(住院病案首页): 吴秀荣，57岁，住院信息
- 3x6606234x1(出院记录): 吴秀荣，57岁，乳腺中心，左乳癌肝转移化疗后，靶向维持治疗中，住院2天
- 3x3060612x1(手术记录): 吴秀荣，56岁，脊柱外科，乳腺肿瘤，胸椎骨折手术
- 3x3052763x1(授权委托书): 吴秀荣，56岁，脊柱外科，仅法律文书，无医学诊断
- 3x3060009x1(手术同意书): 吴秀荣，56岁，脊柱外科，乳腺肿瘤手术知情同意
"""

import sys
from pathlib import Path
import time

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from microharness.ollama import OllamaClient
from microharness.ollama.prompts import format_judge_prompt
from microharness.rag.document_parser import parse_document


# ──────────────────────── 配置 ────────────────────────

RECORDS_DIR = r"C:\Users\Administrator\Desktop\文档下载2026-5-22 17_33_21"

# 测试文件 + 正确的预期结果
# (filename, 描述, [(条件, 预期结果, 说明)])
TEST_CASES = [
    # ========== 出院记录 - 乳腺癌肝转移患者 ==========
    (
        "3x6606234x1(出院记录_3_789_16).html",
        "出院记录-吴秀荣-左乳癌肝转移",
        [
            # 基础诊断
            ("乳腺癌患者", "符合", "明确诊断左乳癌"),
            ("癌症转移患者", "符合", "左乳癌肝转移"),
            ("做过手术的患者", "符合", "住院期间行经皮椎体球囊扩张术"),

            # 复杂条件 - 需要医学推理
            ("需要化疗的癌症患者", "符合", "出院记录提到化疗后靶向维持治疗"),
            ("靶向治疗中的患者", "符合", "明确靶向维持治疗中"),
            ("多发转移的癌症患者", "符合", "左乳癌肝转移，多发转移"),
            ("住院天数超过7天", "不符合", "住院仅2天"),
            ("肝功能异常", "不确定", "出院记录未明确提及肝功能指标"),

            # 边界情况
            ("健康患者", "不符合", "明确癌症诊断"),
            ("需要胰岛素治疗的糖尿病患者", "不符合", "无糖尿病诊断"),
        ]
    ),

    # ========== 授权委托书 - 无医学诊断 ==========
    (
        "3x3052763x1(授权委托书_3_717_6).html",
        "授权委托书-吴秀荣",
        [
            # 各种条件都应不符合
            ("乳腺癌患者", "不符合", "授权委托书无医学诊断信息"),
            ("癌症转移患者", "不符合", "无诊断"),
            ("做过手术的患者", "不符合", "仅是委托书，无手术记录"),
            ("需要化疗的患者", "不符合", "无任何诊疗信息"),
            ("健康患者", "不符合", "无法判断为健康，无医学证据"),

            # 应该能识别出这不是医疗文档
            ("有医学诊断的患者", "不符合", "文档内容仅包含委托信息"),
        ]
    ),

    # ========== 手术记录 ==========
    (
        "3x3060612x1(手术记录_3_777_31).html",
        "手术记录-吴秀荣-乳腺肿瘤手术",
        [
            ("乳腺癌患者", "符合", "诊断包含乳腺肿瘤"),
            ("做过手术的患者", "符合", "明确手术记录"),
            ("脊柱外科患者", "符合", "科室为脊柱外科三组"),
            ("癌症转移患者", "不确定", "术前诊断提到乳腺肿瘤，未明确转移"),
            ("住院天数超过7天", "不确定", "手术记录未提及总住院天数"),
        ]
    ),

    # ========== 手术同意书 ==========
    (
        "3x3060009x1(手术同意书-通用_3_840_8).html",
        "手术同意书-吴秀荣-乳腺肿瘤",
        [
            ("乳腺癌患者", "符合", "术前诊断包含乳腺肿瘤"),
            ("做过手术的患者", "不确定", "知情同意书，未执行手术"),
            ("需要手术的患者", "符合", "拟行手术名称明确"),
            ("癌症转移患者", "不确定", "未明确提及转移"),
        ]
    ),

    # ========== 首次病程记录 - 脊柱外科 ==========
    (
        "3x3055291x1(首次病程记录_3_765_15).html",
        "首次病程记录-吴秀荣-脊柱外科",
        [
            ("乳腺癌患者", "不确定", "首次病程未明确提及癌症诊断"),
            ("糖尿病患者", "不符合", "病历未提及糖尿病"),
            ("胸背部疼痛患者", "符合", "主诉明确胸背部疼痛"),
            ("需要手术的患者", "不确定", "首次病程未提及手术计划"),
        ]
    ),
]


# ──────────────────────── 加载病历 ────────────────────────

def load_record(filepath: Path) -> str:
    """加载并解析病历文件"""
    content = filepath.read_text(encoding="utf-8")
    return parse_document(content.encode(), filepath.name)


def safe_print(text):
    """安全打印，处理编码问题"""
    try:
        print(text)
    except:
        print(str(text)[:200])


def run_tests():
    """运行测试"""
    client = OllamaClient(model="qwen2:7b-instruct")

    if not client.is_available():
        safe_print("错误: Ollama 服务未启动")
        return

    safe_print("=" * 70)
    safe_print("基于真实病历的 LLM 医学判断测试 (修正版)")
    safe_print("=" * 70)
    safe_print(f"病历目录: {RECORDS_DIR}")
    safe_print(f"模型: {client.model}")
    safe_print("=" * 70)

    records_path = Path(RECORDS_DIR)

    total_stats = {"passed": 0, "failed": 0, "uncertain": 0}
    case_stats = {}

    for filename, record_desc, test_conditions in TEST_CASES:
        filepath = records_path / filename
        if not filepath.exists():
            safe_print(f"\n[SKIP] 文件不存在: {filename}")
            continue

        safe_print(f"\n{'='*70}")
        safe_print(f"病历: {record_desc}")
        safe_print(f"文件: {filename}")
        safe_print("-" * 70)

        # 加载病历
        record_content = load_record(filepath)
        safe_print(f"病历长度: {len(record_content)} 字符")

        # 执行测试
        case_stats[record_desc] = {"passed": 0, "failed": 0, "uncertain": 0}

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
            actual_stripped = actual.strip()

            # 判断结果
            if expected == "不确定":
                status = "[?]"
                case_stats[record_desc]["uncertain"] += 1
                total_stats["uncertain"] += 1
            elif actual_stripped == expected:
                status = "[PASS]"
                case_stats[record_desc]["passed"] += 1
                total_stats["passed"] += 1
            else:
                status = "[FAIL]"
                case_stats[record_desc]["failed"] += 1
                total_stats["failed"] += 1

            safe_print(f"  LLM原始回答:{response}")
            safe_print(f"  结果: {status} 实际={actual_stripped} 预期={expected}")

    # 汇总统计
    safe_print("\n" + "=" * 70)
    safe_print("测试结果汇总")
    safe_print("=" * 70)

    for record_desc, stats in case_stats.items():
        total = stats["passed"] + stats["failed"] + stats["uncertain"]
        pass_rate = stats["passed"] / (stats["passed"] + stats["failed"]) * 100 if (stats["passed"] + stats["failed"]) > 0 else 0
        safe_print(f"\n{record_desc}")
        safe_print(f"  通过: {stats['passed']}/{total}  (不确定: {stats['uncertain']})")
        safe_print(f"  确定率: {pass_rate:.0f}%")

    total = total_stats["passed"] + total_stats["failed"]
    total_pass_rate = total_stats["passed"] / total * 100 if total > 0 else 0

    safe_print(f"\n{'='*70}")
    safe_print(f"总计: 通过={total_stats['passed']} 失败={total_stats['failed']} 不确定={total_stats['uncertain']}")
    safe_print(f"确定率: {total_pass_rate:.1f}% (排除不确定)")
    safe_print("=" * 70)


def test_run_tests():
    run_tests()


def main():
    run_tests()


if __name__ == "__main__":
    main()