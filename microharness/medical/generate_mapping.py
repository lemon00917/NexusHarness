"""
Auto-generate disease/symptom → section mapping using LLM.

Usage:
    python -m microharness.medical.generate_mapping

Reads the XML field catalog, uses Ollama to infer what medical conditions
each field typically documents, and outputs an expanded DISEASE_SECTION_MAP.

Output: prints JSON to stdout, ready to paste into query_router.py
"""

import json
from collections import defaultdict
from microharness.medical.field_catalog import get_catalog
from microharness.ollama import OllamaClient

PROMPT = """你是临床医学专家。以下是病历文档的一个字段。

字段名: {field_desc}
文档类型: {doc_title}
XML路径: {xml_path}

请列出这个字段最常记录的疾病、症状或医学条件（5-15个）。
只输出JSON数组，不要其他文字。

示例：
字段名: "出院诊断" → ["糖尿病","高血压","冠心病","肺炎","骨折","肿瘤","脑梗死","胆囊结石","肾功能不全","贫血"]
字段名: "主诉" → ["头痛","发热","咳嗽","胸痛","腹痛","恶心","呕吐","呼吸困难","乏力","腰痛"]
字段名: "手术名称" → ["阑尾切除术","胆囊切除术","冠脉搭桥","椎体成形术","全髋置换","骨折内固定"]
"""


def generate():
    catalog = get_catalog()
    templates = catalog.get("templates", {})

    # Collect all unique field descriptors across all templates
    # Key: (field_desc, xml_tag) → set of diseases
    field_map = defaultdict(set)
    field_doc_map = defaultdict(list)

    for doc_type, info in templates.items():
        doc_title = info.get("title", doc_type)
        for f in info.get("fields", []):
            desc = f["desc"]
            xml_tag = f["path"].split("/")[-1] if "/" in f["path"] else f["path"]
            key = desc
            field_doc_map[key].append(doc_title)

    # Use LLM to fill in diseases for each unique field
    client = OllamaClient(model="qwen2.5:7b", timeout=120)
    result = {}

    unique_fields = sorted(field_doc_map.keys())
    print(f"共 {len(unique_fields)} 个唯一字段，正在通过 LLM 生成映射...")

    for i, field_desc in enumerate(unique_fields):
        docs = field_doc_map[field_desc]
        doc_title = docs[0] if docs else ""

        prompt = PROMPT.format(field_desc=field_desc, doc_title=doc_title, xml_path="")

        try:
            resp = client.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3
            )
            # Parse JSON array from response
            cleaned = resp.strip()
            for fence in ("```json", "```"):
                if fence in cleaned:
                    parts = cleaned.split(fence)
                    if len(parts) >= 2:
                        cleaned = parts[1].split("```")[0] if "```" in parts[1] else parts[1]
                        cleaned = cleaned.strip()
                        break

            items = json.loads(cleaned)
            if isinstance(items, list) and len(items) > 0:
                result[field_desc] = {
                    "diseases": items,
                    "docs": docs,
                }
                print(f"  [{i+1}/{len(unique_fields)}] {field_desc} → {len(items)} 个条目")
            else:
                print(f"  [{i+1}/{len(unique_fields)}] {field_desc} → 跳过（空结果）")

        except Exception as e:
            print(f"  [{i+1}/{len(unique_fields)}] {field_desc} → 失败: {e}")

    # Invert: disease → docs + sections
    disease_map = defaultdict(lambda: {"docs": set(), "sections": set()})
    for field_desc, info in result.items():
        for disease in info["diseases"]:
            disease_map[disease]["docs"].update(info["docs"])
            disease_map[disease]["sections"].add(field_desc)

    # Convert sets to sorted lists
    output = {}
    for disease, info in sorted(disease_map.items()):
        output[disease] = {
            "docs": sorted(info["docs"]),
            "sections": sorted(info["sections"]),
        }

    print(f"\n生成 {len(output)} 个疾病/症状映射")
    print("=" * 60)
    print("复制下面的内容到 query_router.py 的 DISEASE_SECTION_MAP:")
    print("=" * 60)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    generate()
