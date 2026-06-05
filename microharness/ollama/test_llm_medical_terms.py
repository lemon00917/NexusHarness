"""
医学术语识别测试
================
测试LLM对医学术语、缩写、单位等的识别和理解能力

术语类型：
1. 检验指标缩写 (HbA1c, Glu, AST, ALT, WBC, Plt, Cr, BUN, TG, TC, HDL, LDL)
2. 疾病缩写 (T2DM, T1DM, HTN, CAD, MI, CVA, COPD, CKD, SLE, RA, CA, HCC)
3. 手术操作缩写 (PTCA, PCI, CABG, TURP, TURBT, THA, TKA)
4. 药物名称 (二甲双胍、阿司匹林、氯吡格雷、胰岛素、阿托伐他汀)
5. 症状描述 (胸闷、心悸、呼吸困难、水肿、蛋白尿、血尿)
6. 单位换算 (mmol/L, mg/dL, μg/L, 10^9/L)
7. 影像学描述 (CT, MRI, X线,超声, CTA, MRA)
8. 病理分期 (TNM分期、Grade、分化程度)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from microharness.ollama import OllamaClient
from microharness.ollama.prompts import format_judge_prompt
from microharness.rag.document_parser import parse_document


# ──────────────────────── 测试病历 ────────────────────────

RECORDS_DIR = r"C:\Users\Administrator\Desktop\文档下载2026-5-22 17_33_21"

# 检验报告病历（含丰富检验指标）
LAB_RECORD = """检验报告
姓名: 王建国
性别: 男
年龄: 65岁
科室: 内分泌科

检验项目及结果:
- 空腹血糖(Glu): 8.5 mmol/L (参考值: 3.9-6.1)
- 糖化血红蛋白(HbA1c): 7.8% (参考值: 4.0-6.0)
- 谷丙转氨酶(ALT): 45 U/L (参考值: 9-50)
- 谷草转氨酶(AST): 38 U/L (参考值: 15-40)
- 血肌酐(Cr): 98 μmol/L (参考值: 44-97)
- 尿素氮(BUN): 6.8 mmol/L (参考值: 2.6-7.5)
- 白细胞(WBC): 6.5×10^9/L (参考值: 4-10)
- 血小板(Plt): 180×10^9/L (参考值: 100-300)
- 甘油三酯(TG): 2.3 mmol/L (参考值: <1.7)
- 总胆固醇(TC): 5.8 mmol/L (参考值: <5.2)
- 高密度脂蛋白(HDL): 1.1 mmol/L (参考值: >1.0)
- 低密度脂蛋白(LDL): 3.9 mmol/L (参考值: <3.4)

临床印象: 2型糖尿病，血脂异常，肝肾功能基本正常
"""

# 手术病历
SURGERY_RECORD = """手术记录
姓名: 李明
性别: 男
年龄: 58岁
科室: 心内科

术前诊断: 冠心病 不稳定型心绞痛
术中诊断: 冠心病 冠脉多支病变
手术名称: 经皮冠状动脉介入治疗(PCI) 冠脉支架植入术
手术时间: 2024-06-15 10:30-11:45
术者: 张华
麻醉方式: 局麻

造影结果: 左前降支(LAD)近段狭窄85%，右冠状动脉(RCA)中段狭窄75%
手术过程: 先对LAD行PTCA后植入支架1枚，复查造影示残余狭窄<10%
"""

# 病程记录（症状描述）
COURSE_RECORD = """首次病程记录
姓名: 张红
性别: 女
年龄: 72岁
科室: 肾内科

主诉: 泡沫尿伴双下肢水肿2周
现病史: 患者2周前无明显诱因出现泡沫尿，尿中带血，伴胸闷心悸，活动后呼吸困难
既往史: 高血压病史10年，糖尿病史8年，慢性肾病史3年

体格检查:
- 血压: 165/95 mmHg
- 心率: 88次/分，律不齐
- 双下肢重度凹陷性水肿
- 腹部移动性浊音阳性

辅助检查:
- 尿常规: 蛋白尿(+++), 隐血(++), 红细胞15-20/HP
- 肾功能: Cr 156 μmol/L, BUN 12.5 mmol/L
- 估算GFR: 38 mL/min/1.73m²

初步诊断:
1. 慢性肾脏病(CKD) 3-4期
2. 肾病综合征
3. 2型糖尿病糖尿病肾病
4. 高血压3级(很高危)
5. 冠心病 心功能2-3级

鉴别诊断: 需与狼疮性肾炎(SLE)、ANCA相关血管炎等鉴别
"""


# ──────────────────────── 术语测试用例 ────────────────────────

MEDICAL_TERM_TESTS = [
    # ========== 1. 检验指标缩写 ==========
    (
        LAB_RECORD,
        "检验报告-糖尿病患者",
        "检验指标",
        [
            # 血糖相关
            ("HbA1c偏高的患者", "符合", "HbA1c 7.8%超参考值6.0%"),
            ("空腹血糖受损的患者", "符合", "空腹血糖8.5超过6.1"),
            ("血糖控制不佳的糖尿病患者", "符合", "Glu 8.5 + HbA1c 7.8均超标"),

            # 肝功能
            ("ALT升高的患者", "不符合", "ALT 45在参考范围9-50内"),
            ("AST异常的病人", "不符合", "AST 38略高但可接受"),

            # 肾功能
            ("血肌酐升高的患者", "符合", "Cr 98超过参考上限97"),
            ("肾功能正常的患者", "不符合", "肌酐偏高，肾功能轻度受损"),

            # 血脂
            ("高甘油三酯血症患者", "符合", "TG 2.3超过1.7"),
            ("高胆固醇血症患者", "符合", "TC 5.8超过5.2"),
            ("LDL偏高的患者", "符合", "LDL 3.9超过3.4"),
            ("HDL偏低的患者", "不符合", "HDL 1.1>1.0达标"),

            # 血常规
            ("白细胞减少的患者", "不符合", "WBC 6.5在正常范围"),
            ("血小板减少的患者", "不符合", "Plt 180正常"),
        ]
    ),

    # ========== 2. 疾病缩写 ==========
    (
        COURSE_RECORD,
        "病程记录-肾病患者",
        "疾病缩写",
        [
            ("2型糖尿病患者", "符合", "明确T2DM诊断"),
            ("慢性肾脏病患者", "符合", "CKD 3-4期"),
            ("高血压患者", "符合", "高血压3级"),
            ("冠心病的患者", "符合", "冠心病 心功能不全"),
            ("心功能不全患者", "符合", "心功能2-3级"),

            # SLE等需要鉴别
            ("SLE患者", "不确定", "需鉴别但未确诊"),
            ("ANCA相关血管炎患者", "不确定", "需鉴别但未确诊"),
        ]
    ),

    # ========== 3. 手术操作缩写 ==========
    (
        SURGERY_RECORD,
        "手术记录-心内科",
        "手术操作",
        [
            ("接受PCI手术的患者", "符合", "明确行PCI+支架"),
            ("接受PTCA的患者", "符合", "先对LAD行PTCA"),
            ("做过冠脉支架植入术", "符合", "冠脉支架植入术"),
            ("接受CABG的患者", "不符合", "未提及搭桥手术"),
            ("多支冠脉病变", "符合", "造影示冠脉多支病变"),
            ("单支血管病变", "不符合", "LAD和RCA两支均狭窄"),
        ]
    ),

    # ========== 4. 药物名称 ==========
    (
        LAB_RECORD,
        "检验报告-糖尿病患者",
        "药物名称",
        [
            ("服用二甲双胍的患者", "不确定", "糖尿病诊断但未提及用药"),
            ("服用阿司匹林的患者", "不确定", "未提及抗血小板药物"),
            ("服用他汀类药物的患者", "不确定", "血脂异常但未提及用药"),
            ("需要降糖治疗的患者", "符合", "糖尿病+HbA1c超标"),
        ]
    ),

    # ========== 5. 症状描述 ==========
    (
        COURSE_RECORD,
        "病程记录-肾病患者",
        "症状描述",
        [
            ("泡沫尿患者", "符合", "主诉泡沫尿"),
            ("血尿患者", "符合", "尿中带血，隐血++"),
            ("水肿患者", "符合", "双下肢重度凹陷性水肿"),
            ("胸闷患者", "符合", "伴胸闷心悸"),
            ("呼吸困难患者", "符合", "活动后呼吸困难"),
            ("蛋白尿患者", "符合", "蛋白尿(+++)"),
            ("无症状患者", "不符合", "明确多项症状"),
        ]
    ),

    # ========== 6. 单位换算 ==========
    (
        LAB_RECORD,
        "检验报告-糖尿病患者",
        "单位理解",
        [
            ("血糖超过7.0的患者", "符合", "8.5 > 7.0"),
            ("HbA1c超过7.0%的患者", "符合", "7.8% > 7.0%"),
            ("TG超过2.0 mmol/L", "符合", "2.3 > 2.0"),
            ("TC超过6.0的患者", "不符合", "TC 5.8 < 6.0"),
        ]
    ),

    # ========== 7. 影像学 ==========
    (
        COURSE_RECORD,
        "病程记录-肾病患者",
        "影像学",
        [
            ("CT检查患者", "不确定", "未提及CT"),
            ("超声检查患者", "不确定", "未明确超声"),
            ("需要影像学检查的患者", "符合", "辅助检查未完善"),
        ]
    ),

    # ========== 8. 分期分级 ==========
    (
        COURSE_RECORD,
        "病程记录-肾病患者",
        "分期分级",
        [
            ("CKD 3-4期患者", "符合", "明确CKD 3-4期"),
            ("心功能2-3级患者", "符合", "心功能2-3级"),
            ("高血压3级患者", "符合", "高血压3级"),
            ("病情较轻的患者", "不符合", "多脏器损害属重症"),
        ]
    ),

    # ========== 9. 综合判断 ==========
    (
        LAB_RECORD,
        "检验报告-综合",
        "综合判断",
        [
            ("代谢综合征患者", "符合", "糖尿病+高TG+低HDL+血脂异常"),
            ("心血管高危患者", "符合", "糖尿病+血脂异常+老龄"),
            ("仅单一指标异常", "不符合", "多指标异常"),
        ]
    ),
]


# ──────────────────────── 测试函数 ────────────────────────

def safe_print(text):
    try:
        print(text)
    except:
        print(str(text)[:200])


def run_tests(model=None):
    if model is None:
        model = "qwen2:7b-instruct"
    client = OllamaClient(model=model)

    if not client.is_available():
        safe_print("错误: Ollama 服务未启动")
        return

    safe_print("=" * 70)
    safe_print("医学术语识别测试")
    safe_print("=" * 70)
    safe_print(f"模型: {client.model}")
    safe_print("=" * 70)

    total_stats = {"passed": 0, "failed": 0, "uncertain": 0}

    for record_content, record_desc, test_type, test_conditions in MEDICAL_TERM_TESTS:
        safe_print(f"\n{'='*70}")
        safe_print(f"[{test_type}] {record_desc}")
        safe_print("-" * 70)

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