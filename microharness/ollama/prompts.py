# coding: utf-8
"""
Ollama Prompts
==============
Prompt templates for LLM interactions.
"""

# ──────────────────────── Judgment Prompts ────────────────────────

JUDGE_SYSTEM_PROMPT = """你是一个极其严谨的医疗病历分析助手。

你的任务是根据病历片段和筛选条件，判断患者是否明确符合条件。

【核心原则】
1. 严格区分"诊断"和"鉴别诊断"：
   - 诊断 = 已确诊的疾病（如"肺癌"、"乳腺癌"）
   - 鉴别诊断 = 疑似但未确诊（如"转移瘤？"、"恶性待排"、"疑似XX"）
   - "XX？" 或 "XX待排" 或 "疑似XX" = 不是确诊 ≠ 符合"患有XX"

2. 严格理解筛选条件中的否定词：
   - "未患有癌症" = 病历中没有确诊癌症的记录
   - "未发生转移" = 没有病理/影像证实转移的证据

【判断标准】
- 明确诊断：有明确的疾病诊断记录（如"乳腺癌"）→ 符合
- 鉴别诊断：仅有"XX？"、"XX待排"、"疑似XX" → 不符合"患有XX"
- 病理/影像证实：病理报告、肿瘤标志物阳性 → 符合
- 知情同意书/检查申请单：只说明要做什么检查 → 不符合
- 病历中完全未提及 → 不符合

【判断示例】
病历：年龄56岁，诊断"胸椎骨折T12"，影像"转移瘤？其他待排"

筛选1：年龄超过50岁且未患有癌症
判断：不符合（年龄✓，但"转移瘤？"是疑似，不是确诊癌症）

筛选2：患有恶性肿瘤
判断：不符合（"转移瘤？"是疑似，不是确诊）

筛选3：年龄超过50岁且患有癌症
判断：不符合（年龄✓，但没有癌症确诊）

输出格式：
- 符合时：只输出"符合"
- 不符合时：输出"不符合：原因..."

原因必须指出病历中的实际记录。"""

JUDGE_USER_PROMPT = """## 筛选条件
{condition}

## 病历内容
{record_content}

## 输出
严格区分诊断和鉴别诊断，判断是否明确符合筛选条件。"""


# ──────────────────────── Condition Parsing Prompts ────────────────────────

PARSE_CONDITION_SYSTEM = """你是一个医疗条件解析助手。将用户的条件拆分为独立的子条件。

【拆分规则】
- 遇到"且"、"和"、"与"、"、" → 拆成多个子条件
- 遇到"或" → 拆开但logic标记为OR
- 例如："年龄超过50岁且患有癌症且做过化疗" → ["年龄超过50岁", "患有癌症", "做过化疗"]
- 例如："年龄超过50岁或患有癌症" → ["年龄超过50岁", "患有癌症"]

【输出格式 - 必须严格遵循JSON】
{
  "criteria": ["子条件1", "子条件2", ...],
  "keywords": ["关键词1", "关键词2", ...],
  "logic": "AND"或"OR"
}

【要求】
1. criteria: 按拆分规则拆出的子条件列表（至少1个）
2. keywords: 3-8个检索关键词
3. 直接输出JSON，不要其他内容"""

PARSE_CONDITION_USER = """筛选条件：{condition}

请拆分子条件并输出JSON："""


# ──────────────────────── Query Enhancement Prompts ────────────────────────

ENHANCE_QUERY_SYSTEM = """你是一个医疗搜索优化助手。

你的任务是将用户的筛选条件改写成更适合RAG向量检索的查询。

【输出格式 - 必须严格遵循JSON】
{
  "expanded_query": "扩展后的检索查询",
  "expansion_terms": ["扩展词1", "扩展词2", ...]
}

【要求】
1. expanded_query: 保留原条件核心语义，用更规范的医学术语表达，控制在50字以内
2. expansion_terms: 列出3-6个同义词/相关词/下位词，用于补充检索
3. 扩展方向：
   - 症状词 → 疾病名（如"胸痛"→"肺癌"、"胸痛"→"冠心病"）
   - 检查名 → 指标名（如"CT"→"肺部CT"、"MRI"→"磁共振"）
   - 药物名 → 通用名/商品名
   - 时间词保留（如"2024年"）
4. 只输出JSON，不要任何其他内容"""


ENHANCE_QUERY_USER = """请将以下筛选条件改写成适合RAG检索的查询：

{condition}

JSON输出："""


def format_judge_prompt(condition: str, record_content: str) -> tuple:
    """
    Format judgment prompt for LLM.

    Args:
        condition: Filter condition description
        record_content: Medical record content

    Returns:
        Tuple of (system_prompt, user_prompt)
    """
    user_prompt = JUDGE_USER_PROMPT.format(
        condition=condition,
        record_content=record_content
    )
    return JUDGE_SYSTEM_PROMPT, user_prompt


def format_parse_prompt(condition: str) -> tuple:
    """
    Format condition parsing prompt for LLM.

    Args:
        condition: Natural language condition

    Returns:
        Tuple of (system_prompt, user_prompt)
    """
    user_prompt = PARSE_CONDITION_USER.format(condition=condition)
    return PARSE_CONDITION_SYSTEM, user_prompt


def format_enhance_query_prompt(condition: str) -> tuple:
    """
    Format query enhancement prompt for LLM.

    Args:
        condition: Original filter condition

    Returns:
        Tuple of (system_prompt, user_prompt)
    """
    user_prompt = ENHANCE_QUERY_USER.format(condition=condition)
    return ENHANCE_QUERY_SYSTEM, user_prompt


# ──────────────────────── Batch Judgment Prompts ────────────────────────

BATCH_JUDGE_SYSTEM_PROMPT = """你是一个医疗病历分析助手，根据病历片段和筛选条件判断患者是否符合条件。

【输出格式 - 最高优先级，违反将导致系统错误】
你必须只输出一行纯JSON，格式如下：
{"matched": true或false, "matched_docs": [{"doc_id": "文档ID", "filename": "文件名", "matched_chunks": [{"chunk_index": 1, "chunk_content": "匹配内容（最多50字）", "matched_condition": "匹配的条件"}], "reason": "匹配原因"}], "unmatched_conditions": ["未满足的条件"], "summary": "一句话总结"}

【判断规则 - 违反任何一条都会导致错误】
1. AND逻辑：所有子条件必须同时满足 → matched=true，任一不满足 → matched=false
2. ★证据不完整=不符合★：条件需要多个数据点时（如"住院时间短于5天"需同时有入院日期和出院日期），缺任意一个即判不符合
3. 严格区分诊断和鉴别诊断："XX？"、"XX待排"、"疑似XX" = 不是确诊 ≠ 符合
4. 日期条件：必须从chunk中精确提取，不能推算、不能猜测
5. ★默认不符合★：证据模糊、不完整、无法确认时，必须输出matched=false"""


BATCH_JUDGE_USER_PROMPT = """筛选条件：{condition}

病历片段：
{documents}

只输出JSON（以{{开头）："""


def format_batch_judge_prompt(condition: str, documents: str) -> tuple:
    """
    Format batch judgment prompt for LLM.

    Args:
        condition: Filter condition description
        documents: Concatenated document chunks with doc_id and filename

    Returns:
        Tuple of (system_prompt, user_prompt)
    """
    user_prompt = BATCH_JUDGE_USER_PROMPT.format(
        condition=condition,
        documents=documents
    )
    return BATCH_JUDGE_SYSTEM_PROMPT, user_prompt


def clean_html_preserve_structure(html_content: str) -> str:
    """
    Clean HTML by removing style/script/comments but preserve structure.
    Returns the raw content with HTML tags for chapter-based chunking.
    """
    import re
    from html.parser import HTMLParser

    class HTMLCleaner(HTMLParser):
        def __init__(self):
            super().__init__()
            self.result = []
            self.skip = False
            self.skip_tags = {'style', 'script', 'head', 'meta', 'link'}

        def handle_starttag(self, tag, attrs):
            if tag in self.skip_tags:
                self.skip = True
            elif tag in {'p', 'br', 'div', 'span', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
                self.result.append('\n')

        def handle_endtag(self, tag):
            if tag in self.skip_tags:
                self.skip = False
            elif tag in {'p', 'div', 'span'}:
                self.result.append('\n')

        def handle_data(self, data):
            if not self.skip:
                self.result.append(data)

    try:
        parser = HTMLCleaner()
        parser.feed(html_content)
        text = ''.join(parser.result)
    except:
        text = html_content

    # Remove excessive whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)

    return text.strip()


LLM_HEADING_SYSTEM = """你是一个医疗病历结构分析助手。你的任务是在病历文本中识别章节标题，并用##标记。

【规则】
1. 识别病历中的主要章节，如：入院记录、现病史、体格检查、辅助检查、初步诊断等
2. 用 ## 标记主要章节（如 ## 入院记录）
3. 用 ### 标记子章节（如 ### 一般情况、### 肺部检查）
4. 保持原文内容不变，只添加 ## 标记
5. 只输出带标记的文本，不要任何解释
6. 如果文本已经有 ## 标记，保持不变
7. 不要添加任何解释性文字

【示例】
输入：入院记录\n姓名：张三\n主诉：胸痛
输出：## 入院记录\n姓名：张三\n主诉：胸痛"""


def format_llm_chunk_prompt(content: str) -> tuple:
    """
    Format LLM prompt for identifying chapter headings in medical records.
    Step 1: Use trafilatura to convert HTML to clean markdown
    Step 2: LLM adds ## headings to that clean markdown
    """
    import trafilatura

    # Step 1: Use trafilatura to convert HTML to clean markdown
    trafilatura_md = trafilatura.extract(
        content,
        include_tables=True,
        include_images=False,
        output_format="markdown",
        favor_recall=True
    )

    if not trafilatura_md or len(trafilatura_md) < 50:
        # Fallback to regex-based conversion
        trafilatura_md = clean_html_preserve_structure(content)

    # Step 2: Limit size for LLM
    truncated = trafilatura_md[:15000] if len(trafilatura_md) > 15000 else trafilatura_md

    return LLM_HEADING_SYSTEM, truncated
