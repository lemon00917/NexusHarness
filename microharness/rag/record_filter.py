"""
Medical Record Filter
======================
RAG-based medical record filtering with LLM reasoning judgment.
"""

import json
import re
import time
from dataclasses import dataclass
from typing import List, Optional

from microharness.ollama import OllamaClient, get_client
from microharness.rag.rag import SimpleRAG, SearchResult
from microharness.ollama.prompts import format_judge_prompt, format_parse_prompt, format_enhance_query_prompt

# Logging
from microharness.observability.logger import filter_logger, rag_logger, ollama_logger


# ──────────────────────────────────────────────────
# Robust JSON extraction (small model tolerant)
# ──────────────────────────────────────────────────

def _try_parse_json(text: str) -> dict:
    """Multi-strategy JSON parser for LLM responses.

    Tries progressively more aggressive extraction methods:
    1. Direct json.loads
    2. Strip markdown fences + json.loads
    3. Balanced-brace extraction + json.loads
    4. Fix trailing commas / unquoted keys + json.loads
    5. Regex-extract "matched" field from free text (last resort)

    Returns a dict with at least {"matched": bool}.
    """
    raw = text

    # Strategy 1: direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Strategy 2: strip markdown fences
    cleaned = raw.strip()
    for fence in ("```json", "```"):
        if fence in cleaned:
            parts = cleaned.split(fence)
            if len(parts) >= 2:
                inner = parts[1].split("```")[0] if "```" in parts[1] else parts[1]
                cleaned = inner.strip()
                break

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Strategy 3: balanced-brace extraction
    start = cleaned.find("{")
    if start >= 0:
        depth = 0
        end = -1
        for i in range(start, len(cleaned)):
            if cleaned[i] == "{":
                depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end > start:
            candidate = cleaned[start:end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

    # Strategy 4: fix common JSON errors in the extracted fragment
    candidate = cleaned[start:end + 1] if (start >= 0 and end > start) else cleaned
    # Remove trailing commas before } or ]
    fixed = re.sub(r",(\s*[}\]])", r"\1", candidate)
    # Fix unquoted keys (word: → "word":)
    fixed = re.sub(r'([{,])\s*(\w+)\s*:', r'\1"\2":', fixed)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # Strategy 5: regex fallback — extract matched field from any text
    result = {"matched": False, "matched_docs": [], "summary": "JSON解析失败"}
    m = re.search(r'"matched"\s*:\s*(true|false)', raw)
    if m:
        result["matched"] = (m.group(1) == "true")

    # Try to extract matched_docs and summary fragments
    summary_m = re.search(r'"summary"\s*:\s*"([^"]{1,200})"', raw)
    if summary_m:
        result["summary"] = summary_m.group(1)
    else:
        # Fallback: grab last sentence that mentions 符合/不符合
        for keyword in ("不符合", "符合"):
            idx = raw.rfind(keyword)
            if idx > 0:
                snippet = raw[max(0, idx - 30):idx + 30].replace("\n", " ")
                result["summary"] = f"提取: ...{snippet}..."
                break

    # Chinese semantic fallback: if no JSON matched field found,
    # try to infer from Chinese text patterns
    if not m:
        # Look for conclusion-like patterns near the end of text
        tail = raw[-500:] if len(raw) > 500 else raw
        # "不符合" (negation) takes priority — check first
        if re.search(r'不符合', tail):
            result["matched"] = False
        elif re.search(r'(?<!不)符合', tail):
            result["matched"] = True
        # Strong positive indicators
        if re.search(r'(明确|确诊|证实).*(符合|满足|匹配)', tail):
            result["matched"] = True
        # Strong negative indicators
        if re.search(r'不(符合|满足|匹配)', tail):
            result["matched"] = False

    return result


@dataclass
class ParsedCondition:
    """Structured representation of a filter condition."""
    original: str
    keywords: List[str]
    criteria: List[str]  # sub-conditions for multi-route search
    summary: str


@dataclass
class FilterResult:
    """Result of filtering a single record."""
    doc_id: str
    filename: str
    content: str  # 完整文档内容
    score: float  # 向量相似度
    matched: bool  # LLM判断结果
    reason: Optional[str] = None  # LLM判断理由
    retrieved_chunk: Optional[str] = None  # RAG检索到的chunk内容
    matched_keywords: Optional[List[str]] = None  # RAG检索时匹配到的关键词


@dataclass
class _SearchChunkWrapper:
    """Wrapper to adapt SearchResult.document to the chunk interface used in filter_batch."""
    content: str
    filename: str
    score: float
    chunk_id: Optional[str] = None  # ChromaDB chunk ID (doc_id_chunk_N)
    visit_id: Optional[str] = None


from pathlib import Path as PathFn

def _get_record_filter_index_dir():
    """Get absolute path for rag_index, resolved relative to record_filter.py location."""
    return str(PathFn(__file__).parent.parent.parent / "web" / "rag_index")


# ──────────────────────────────────────────────────
# Medical synonym expansion for query enhancement
# ──────────────────────────────────────────────────

# Core medical concept → retrieval synonyms
# Expands "患有癌症" → "患有癌症 肿瘤 恶性肿瘤 癌" to improve BM25 + vector hit rate
_MEDICAL_SYNONYMS: dict = {
    "癌症": ["肿瘤", "恶性肿瘤", "癌", "占位", "肿物"],
    "化疗": ["化学治疗", "抗肿瘤治疗", "药物治疗", "药物化疗"],
    "手术": ["切除术", "根治术", "术", "手术"],
    "转移": ["扩散", "继发", "转移性", "M1"],
    "骨折": ["病理性骨折", "骨折", "骨破坏"],
    "高血压": ["血压升高", "高血压病", "BP高"],
    "糖尿病": ["血糖升高", "DM", "糖尿病"],
}
_SYNONYM_PATTERNS: dict = {}  # compiled regex cache


def _expand_medical_query(query: str) -> str:
    """Append medical synonyms to a query string for broader retrieval.

    Example: "患有癌症" → "患有癌症 肿瘤 恶性肿瘤 癌 占位 肿物"
    """
    expanded = query
    for term, synonyms in _MEDICAL_SYNONYMS.items():
        if term in query:
            for s in synonyms:
                if s not in expanded:
                    expanded += " " + s
    if expanded != query:
        filter_logger.info(f"[同义词扩展] {query[:40]} → {expanded[:80]}")
    return expanded


class RecordFilter:
    """
    Medical record filter with RAG retrieval and LLM reasoning.

    Flow:
        1. User provides natural language condition
        2. Retrieve candidate records via RAG
        3. For each candidate, use LLM to judge if it matches
        4. Return matched records
    """

    def __init__(
        self,
        index_dir: str = _get_record_filter_index_dir(),
        collection_name: str = "medical_records",
        ollama_client: Optional[OllamaClient] = None,
        retrieval_top_k: int = 100,
        vector_weight: float = 0.7,
        bm25_weight: float = 0.3,
        model: Optional[str] = None,
        enhance_query_mode: str = "simple",
        enhance_model: Optional[str] = None
    ):
        """
        Initialize RecordFilter.

        Args:
            index_dir: Directory for RAG index persistence
            collection_name: ChromaDB collection name for medical records
            ollama_client: Ollama client instance (uses default if None)
            retrieval_top_k: Number of candidates to retrieve for judgment
            vector_weight: Weight for vector search (0.0-1.0)
            bm25_weight: Weight for BM25 search (0.0-1.0)
            model: Ollama model to use for LLM judgment (uses default if None)
            enhance_query_mode: "simple" = string join, "llm" = LLM expansion
            enhance_model: Ollama model for LLM query expansion (uses default if None)
        """
        self.collection_name = collection_name
        self.retrieval_top_k = retrieval_top_k
        self.vector_weight = vector_weight
        self.bm25_weight = bm25_weight

        # Parse cache: condition → ParsedCondition (max 128 entries)
        self._parse_cache: dict = {}
        self._parse_cache_max = 128
        self.model = model
        self.enhance_query_mode = enhance_query_mode
        self.enhance_model = enhance_model

        # Initialize RAG - reuse SimpleRAG with custom collection
        self.rag = SimpleRAG(index_dir=index_dir)
        self.rag.load_index()

        # Reranker client (lazy init)
        self._reranker_client: Optional['OllamaClient'] = None
        self._reranker_model = "dengcao/Qwen3-Reranker-0.6B:F16"

        # Initialize Ollama client (use specified model or default)
        if ollama_client:
            self.ollama = ollama_client
        elif model:
            from microharness.ollama import OllamaClient
            self.ollama = OllamaClient(model=model)
        else:
            self.ollama = get_client()

    def filter(self, condition: str, visit_id: Optional[str] = None, only_matched: bool = True) -> List[FilterResult]:
        """
        Filter records based on condition.

        Flow:
            Step 1: Parse condition with LLM (extract keywords/summary)
            Step 2: Build enhanced query from original + parsed keywords
            Step 3: Retrieve candidate records via RAG
            Step 4: Judge each candidate with LLM

        Args:
            condition: Natural language filter condition
            visit_id: Optional visit/patient ID to filter documents
            only_matched: If True, only return matched records

        Returns:
            List of FilterResult objects
        """
        start_time = time.time()
        filter_logger.info(f"=" * 50)
        filter_logger.info(f"开始筛选 | 条件: {condition[:50]}... | 就诊号: {visit_id or '全部'} | only_matched: {only_matched}")
        filter_logger.info(f"使用模型: {self.model or 'default'}")

        # ========== Step 1: 解析条件 ==========
        step1_start = time.time()
        filter_logger.info(f"[Step 1/4] 解析条件...")
        parsed = self._parse_condition(condition)
        step1_duration = (time.time() - step1_start) * 1000
        filter_logger.info(f"[Step 1/4] 解析完成 | 耗时: {step1_duration:.0f}ms | 关键词: {parsed.keywords}")
        filter_logger.info(f"  结构化描述: {parsed.summary[:80]}...")

        # ========== Step 2: 构建增强查询 ==========
        step2_start = time.time()
        filter_logger.info(f"[Step 2/4] 构建增强查询...")
        enhanced_query = self._build_enhanced_query(condition, parsed)
        step2_duration = (time.time() - step2_start) * 1000
        filter_logger.info(f"[Step 2/4] 查询构建完成 | 耗时: {step2_duration:.0f}ms")
        filter_logger.info(f"  增强查询: {enhanced_query[:80]}...")

        # ========== Step 3: RAG检索 ==========
        step3_start = time.time()
        filter_logger.info(f"[Step 3/4] RAG检索 (向量{self.vector_weight:.0%} + BM25{self.bm25_weight:.0%})...")
        candidates = self.rag.search(
            query=enhanced_query,
            top_k=self.retrieval_top_k,
            vector_weight=self.vector_weight,
            bm25_weight=self.bm25_weight
        )
        step3_duration = (time.time() - step3_start) * 1000
        filter_logger.info(f"[Step 3/4] 检索完成 | 候选: {len(candidates)} | 耗时: {step3_duration:.0f}ms")
        for i, c in enumerate(candidates):
            filter_logger.info(f"  候选{i+1}: {c.document.filename} (score={c.score:.3f})")

        if not candidates:
            filter_logger.info(f"筛选完成 | 无候选文档 | 总耗时: {(time.time() - start_time) * 1000:.0f}ms")
            return []

        # ========== Step 4: LLM判断 ==========
        step4_start = time.time()
        filter_logger.info(f"[Step 4/4] LLM判断每个候选文档...")
        results = []
        ollama_calls = 0
        ollama_errors = 0

        for candidate in candidates:
            judge_start = time.time()
            result = self._judge_record(candidate, condition, parsed)
            judge_duration = (time.time() - judge_start) * 1000

            if result.reason and "Error:" in result.reason:
                ollama_errors += 1

            results.append(result)
            ollama_calls += 1

            filter_logger.info(
                f"  判断{i+1}/{len(candidates)}: {result.filename} | "
                f"匹配: {'YES' if result.matched else 'NO'} | 耗时: {judge_duration:.0f}ms"
            )

        step4_duration = (time.time() - step4_start) * 1000
        filter_logger.info(f"[Step 4/4] 判断完成 | 匹配: {sum(1 for r in results if r.matched)}/{len(results)} | 耗时: {step4_duration:.0f}ms")

        # Filter results if needed
        if only_matched:
            results = [r for r in results if r.matched]

        total_duration = (time.time() - start_time) * 1000
        matched_count = len(results)

        filter_logger.info(f"=" * 50)
        filter_logger.info(
            f"筛选完成 | "
            f"条件: {condition[:30]}... | "
            f"候选: {len(candidates)} | 匹配: {matched_count} | "
            f"Step1: {step1_duration:.0f}ms | Step3: {step3_duration:.0f}ms | Step4: {step4_duration:.0f}ms | 总耗时: {total_duration:.0f}ms"
        )
        filter_logger.info(f"=" * 50)

        return {
            "results": results,
            "enhanced_query": enhanced_query,
            "enhance_query_mode": self.enhance_query_mode,
            "step_timings": {
                "step1_parse_ms": step1_duration,
                "step3_retrieve_ms": step3_duration,
                "step4_judge_ms": step4_duration,
                "total_ms": total_duration
            }
        }

    def _parse_condition(self, condition: str) -> ParsedCondition:
        """
        Use LLM to parse natural language condition into structured format.
        Results are cached per condition string (max 128 entries).

        Args:
            condition: Natural language condition

        Returns:
            ParsedCondition with keywords and summary
        """
        # Check cache first
        if condition in self._parse_cache:
            filter_logger.info(f"[Parse] 缓存命中 | 条件: {condition[:50]}...")
            return self._parse_cache[condition]

        system_prompt, user_prompt = format_parse_prompt(condition)

        try:
            response = self.ollama.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.1
            )

            # Parse JSON response from LLM
            llm_response = response.strip()

            # Try to extract JSON from response
            import json
            json_text = llm_response
            if "```json" in json_text:
                parts = json_text.split("```json")
                if len(parts) >= 2:
                    json_text = parts[1].split("```")[0]
            elif "```" in json_text:
                parts = json_text.split("```")
                if len(parts) >= 2:
                    json_text = parts[1]

            json_text = json_text.strip()
            parsed_data = json.loads(json_text)

            keywords = parsed_data.get("keywords", [])
            criteria = parsed_data.get("criteria", [])
            if not criteria:
                # Fallback: LLM didn't split → split by delimiters
                criteria = [c.strip() for c in re.split(r'且|和|与|、|,|，|或', condition) if c.strip()]
                filter_logger.info(f"[Parse] LLM未拆分criteria，兜底拆分: {criteria}")
            logic = parsed_data.get("logic", "AND")

            result = ParsedCondition(
                original=condition,
                keywords=keywords[:8],
                criteria=criteria[:10],
                summary=f"[{logic}] " + " | ".join(criteria[:5])
            )
            # Cache successful parse (LRU eviction)
            if len(self._parse_cache) >= self._parse_cache_max:
                # Remove oldest entry (first key)
                oldest = next(iter(self._parse_cache))
                del self._parse_cache[oldest]
            self._parse_cache[condition] = result
            return result
        except Exception as e:
            # Fallback: use simple extraction
            import re
            keywords = []
            llm_response = ""  # 定义变量避免后续引用错误
            for line in condition.split('\n'):
                matches = re.findall(r'[一-鿿]+', line)
                for m in matches:
                    if len(m) > 1 and len(keywords) < 8:
                        keywords.append(m)

            return ParsedCondition(
                original=condition,
                keywords=keywords or condition.split(),
                criteria=[],  # fallback has no sub-conditions
                summary=f"解析失败，使用原始条件: {str(e)[:50]}"
            )

    def _build_enhanced_query(self, original: str, parsed: ParsedCondition) -> str:
        """
        Build enhanced query combining original + parsed keywords.

        Args:
            original: Original user condition
            parsed: Parsed condition result

        Returns:
            Enhanced query string for RAG retrieval
        """
        if self.enhance_query_mode == "llm":
            return self._build_enhanced_query_llm(original, parsed)
        else:
            return self._build_enhanced_query_simple(original, parsed)

    def _build_enhanced_query_simple(self, original: str, parsed: ParsedCondition) -> str:
        """Simple string-join approach."""
        enhanced_parts = [original]
        if parsed and parsed.keywords:
            enhanced_parts.extend(parsed.keywords[:8])
        return " ".join(enhanced_parts)

    def _build_enhanced_query_llm(self, original: str, parsed: ParsedCondition) -> str:
        """LLM-based query expansion."""
        try:
            system_prompt, user_prompt = format_enhance_query_prompt(original)
            response = self.ollama.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.1,
                model=self.enhance_model
            )

            import json
            llm_response = response.strip()
            if "```json" in llm_response:
                parts = llm_response.split("```json")
                if len(parts) >= 2:
                    llm_response = parts[1].split("```")[0]
            elif "```" in llm_response:
                parts = llm_response.split("```")
                if len(parts) >= 2:
                    llm_response = parts[1]

            data = json.loads(llm_response.strip())
            expanded_query = data.get("expanded_query", original)
            expansion_terms = data.get("expansion_terms", [])

            # Combine expanded query + expansion terms
            parts = [expanded_query]
            parts.extend(expansion_terms[:6])
            return " ".join(parts)

        except Exception as e:
            filter_logger.warning(f"LLM增强查询失败，回退到simple模式: {e}")
            return self._build_enhanced_query_simple(original, parsed)

    def _judge_record(self, candidate: SearchResult, condition: str, parsed: 'ParsedCondition') -> FilterResult:
        """
        Use LLM to judge if a record matches the condition.

        Args:
            candidate: SearchResult from RAG
            condition: Filter condition
            parsed: ParsedCondition with keywords for tracking

        Returns:
            FilterResult with matched=True/False
        """
        # 使用检索到的chunk内容判断（通过matched_chunk获取实际chunk内容）
        if candidate.matched_chunk and candidate.matched_chunk in candidate.document.metadata.get("_chunk_contents", {}):
            retrieved_chunk = candidate.document.metadata["_chunk_contents"][candidate.matched_chunk]
        else:
            retrieved_chunk = candidate.document.content
        # 使用chunk内容（限制长度避免过长）
        content_for_judge = retrieved_chunk[:2000] if retrieved_chunk else candidate.document.content[:2000]

        system_prompt, user_prompt = format_judge_prompt(condition, content_for_judge)

        try:
            response = self.ollama.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.1
            )

            response = response.strip()
            # Parse response: "符合" or "不符合：原因..."
            if response == "符合":
                matched = True
                reason = None
            elif response.startswith("不符合"):
                matched = False
                reason = response[4:] if len(response) > 4 else "不符合"
            else:
                # Fallback: old format check
                matched = "符合" in response and "不符合" not in response
                reason = None if matched else response

        except Exception as e:
            matched = False
            reason = f"LLM调用失败: {str(e)}"

        # 检查哪些关键词匹配上了
        matched_keywords = []
        if parsed and parsed.keywords:
            for kw in parsed.keywords:
                if kw in retrieved_chunk:
                    matched_keywords.append(kw)

        return FilterResult(
            doc_id=candidate.document.doc_id,
            filename=candidate.document.filename,
            content=candidate.document.content,  # 完整文档内容
            score=candidate.score,  # 向量相似度
            matched=matched,
            reason=reason,
            retrieved_chunk=retrieved_chunk,  # RAG检索到的chunk
            matched_keywords=matched_keywords  # 匹配上的关键词
        )

    def add_record(self, content: str, filename: str, visit_id: str, metadata: Optional[dict] = None) -> str:
        """
        Add a medical record to the index.

        Args:
            content: Record content
            filename: Filename for reference
            visit_id: Visit/patient ID (required)
            metadata: Optional metadata

        Returns:
            Document ID
        """
        return self.rag.add_document(content, filename, visit_id, metadata)

    def add_records_from_dir(self, dir_path: str, visit_id: str) -> int:
        """
        Load all records from a directory.

        Args:
            dir_path: Path to directory containing record files
            visit_id: Visit/patient ID to bind to all loaded documents

        Returns:
            Number of records loaded
        """
        return self.rag.load_documents_from_dir(dir_path, visit_id)

    def _filter_batch_finish(
        self,
        result_data: dict,
        enhanced_query: str,
        step1_duration: float,
        step_retrieve_duration: float,
        start_time: float,
        chunks: list,
    ) -> dict:
        """Shared finish logic for filter_batch results."""
        total_duration = (time.time() - start_time) * 1000
        filter_logger.info(f"批量筛选完成 | 匹配: {result_data.get('matched', False)} | 总耗时: {total_duration:.0f}ms")
        filter_logger.info(f"=" * 50)

        result_data["enhanced_query"] = enhanced_query
        result_data["enhance_query_mode"] = self.enhance_query_mode
        result_data["step_timings"] = {
            "step1_enhance_ms": step1_duration,
            "step2_retrieve_ms": step_retrieve_duration,
            "step3_judge_ms": (time.time() - start_time) * 1000 - step1_duration - step_retrieve_duration,
            "total_ms": total_duration
        }
        result_data["all_chunks"] = [
            {
                "chunk_index": i + 1,
                "chunk_id": getattr(c, 'chunk_id', c.filename),
                "filename": c.filename,
                "score": round(c.score, 4),
                "content_preview": c.content[:1000]
            }
            for i, c in enumerate(chunks)
        ]
        return result_data

    def _multi_route_search(
        self,
        criteria: List[str],
        sub_top_k: int,
        vector_weight: float = 0.7,
        bm25_weight: float = 0.3,
    ) -> list:
        """
        Multi-route search: each criterion searches sub_top_k chunks,
        then round-robin merge + chunk-level dedup.

        Args:
            criteria: List of sub-condition strings
            sub_top_k: Number of chunks per criterion
            vector_weight: Weight for vector similarity
            bm25_weight: Weight for BM25 keyword matching

        Returns:
            Tuple of (merged chunks list, expanded_queries dict)
        """
        if not criteria:
            return [], {}

        all_results: List[List] = []
        expanded_queries: dict = {}

        for criterion in criteria:
            query = _expand_medical_query(criterion)
            expanded_queries[criterion] = query
            results = self.rag.search(
                query=query,
                top_k=sub_top_k,
                vector_weight=vector_weight,
                bm25_weight=bm25_weight,
                deduplicate=False,
            )
            chunks = [
                _SearchChunkWrapper(
                    content=r.matched_chunk_content or r.document.content,
                    filename=r.document.filename,
                    score=r.score,
                    chunk_id=r.chunk_id,
                    visit_id=r.document.metadata.get("visit_id"),
                )
                for r in results
            ]
            all_results.append(chunks)
            filter_logger.info(
                f"[多路检索] 子条件: {criterion[:40]}... | 检索到 {len(chunks)} 个chunks"
            )

        # Round-robin merge + chunk dedup
        seen: set = set()
        merged: list = []
        max_len = max(len(c) for c in all_results) if all_results else 0
        for i in range(max_len):
            for chunks in all_results:
                if i < len(chunks):
                    c = chunks[i]
                    cid = c.chunk_id or f"{c.filename}:{hash(c.content[:80])}"
                    if cid not in seen:
                        seen.add(cid)
                        merged.append(c)

        filter_logger.info(
            f"[多路检索] 合并完成 | {len(criteria)}路 × {sub_top_k} → "
            f"{len(merged)} 个去重chunks"
        )
        return merged, expanded_queries

    def _rerank_chunks(
        self,
        chunks: list,
        condition: str,
        top_k: int = 5,
    ) -> list:
        """
        Re-rank chunks using Qwen3-Reranker via Ollama chat API.

        The reranker is trained to output "yes"/"no" for document relevance.
        Chunks judged "no" are filtered out; "yes" chunks are kept in original order.

        Args:
            chunks: List of _SearchChunkWrapper from multi-route search
            condition: Original filter condition
            top_k: Max chunks to return after reranking

        Returns:
            Filtered list of _SearchChunkWrapper
        """
        if not chunks:
            return chunks

        from concurrent.futures import ThreadPoolExecutor, as_completed

        # Lazy init reranker client
        if self._reranker_client is None:
            from microharness.ollama import OllamaClient
            self._reranker_client = OllamaClient(model=self._reranker_model, timeout=60)

        reranker_prompt = (
            "<Instruct>: Given a medical condition, determine whether the medical record "
            "document contains evidence relevant to the condition.\n"
            f"<Query>: {condition}\n"
            "<Document>: {}"
        )

        def _judge_one(chunk):
            """Call reranker for a single chunk, returns (chunk_id, is_relevant)."""
            doc_text = chunk.content[:3000]
            user_msg = reranker_prompt.format(doc_text)
            try:
                response = self._reranker_client.chat(
                    messages=[
                        {"role": "system",
                         "content": "Judge whether the Document meets the requirements based on "
                                    "the Query and the Instruct provided. Note that the answer "
                                    'can only be "yes" or "no".'},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0,
                    model=self._reranker_model,
                )
                is_yes = "yes" in response.strip().lower()
                return (chunk.chunk_id or chunk.filename), is_yes
            except Exception as e:
                filter_logger.warning(f"[Reranker] 调用失败: {e}，保留chunk")
                return (chunk.chunk_id or chunk.filename), True  # Keep on error

        # Parallel rerank — max 2 concurrent to avoid Ollama overload
        max_workers = min(2, len(chunks))
        id_kept: dict = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_judge_one, c): c for c in chunks}
            for future in as_completed(futures):
                chunk_id, kept = future.result()
                id_kept[chunk_id] = kept

        # Keep only "yes" chunks, preserve original order
        reranked = [c for c in chunks if id_kept.get(c.chunk_id or c.filename, True)]

        yes_count = sum(1 for v in id_kept.values() if v)
        filter_logger.info(
            f"[Reranker] 完成 | {len(chunks)} chunks → {yes_count} yes/{len(chunks)-yes_count} no → "
            f"保留 {len(reranked)} 个"
        )

        return reranked[:top_k]

    def list_records(self) -> List[dict]:
        """List all indexed records."""
        return self.rag.list_documents()

    def filter_batch(
        self,
        condition: str,
        visit_id: Optional[str] = None,
        top_k: int = 20,
        score_threshold: float = 0.0,
        merge_mode: str = "combined",
        sub_top_k: int = 3,
    ) -> dict:
        """
        Batch filter: retrieve chunks then judge all together.

        Flow:
            Step 1: Parse condition with LLM (extract keywords/summary)
            Step 2: Build enhanced query from original + parsed keywords
            Step 3: Retrieve top-k chunks via RAG (no dedup, keeping all chunks)
            Step 4: Group chunks by visit_id (if merge_mode="combined")
            Step 5: Send chunks to LLM for batch judgment

        Args:
            condition: Natural language filter condition
            visit_id: Optional visit/patient ID to filter documents
            top_k: Number of chunks to retrieve for batch judgment
            score_threshold: Minimum vector similarity score (0.0-1.0) to include chunk
            merge_mode: "combined" = multi-route search + round-robin merge + LLM judge,
                       "per_doc" = single-query search + judge each doc independently
            sub_top_k: (combined mode only) chunks retrieved per sub-condition, default 2

        Returns:
            Dict with matched status, matched docs info, and summary
        """
        import json
        start_time = time.time()
        filter_logger.info(f"=" * 50)
        search_info = f"sub_top_k={sub_top_k}" if merge_mode == "combined" else f"top_k={top_k}"
        filter_logger.info(f"批量筛选 | 条件: {condition[:50]}... | 就诊号: {visit_id or '全部'} | {search_info} | mode={merge_mode}")

        # ========== Step 1: 解析条件 + 构建检索 ==========
        step1_start = time.time()

        if merge_mode == "combined":
            # combined: 始终解析条件获取 criteria，用于多路检索
            parsed = self._parse_condition(condition)
            step1_duration = (time.time() - step1_start) * 1000
            enhanced_query = ""  # combined 不需要增强查询，用 criteria 多路检索
            step2_duration = 0
            filter_logger.info(
                f"[Step 1/3] 条件解析完成 | 耗时: {step1_duration:.0f}ms | "
                f"criteria: {len(parsed.criteria)}个 | keywords: {parsed.keywords}"
            )
        elif self.enhance_query_mode == "llm":
            # per_doc + llm: 直接增强查询，跳过 parse
            enhanced_query = self._build_enhanced_query_llm(condition, None)
            step1_duration = (time.time() - step1_start) * 1000
            step2_duration = 0
            parsed = None
            filter_logger.info(f"[Step 1/2] LLM增强查询一步完成 | 增强查询: {enhanced_query[:80]}... | 耗时: {step1_duration:.0f}ms")
        else:
            # per_doc + simple: 先 parse 再拼接
            parsed = self._parse_condition(condition)
            step1_duration = (time.time() - step1_start) * 1000
            step2_start = time.time()
            enhanced_query = self._build_enhanced_query_simple(condition, parsed)
            step2_duration = (time.time() - step2_start) * 1000
            filter_logger.info(f"[Step 1/2] 解析+构建完成 | 解析: {step1_duration:.0f}ms | 拼接: {step2_duration:.0f}ms | 关键词: {parsed.keywords}")

        # ========== Step 2: 检索 chunks ==========
        step_retrieve_start = time.time()

        if merge_mode == "combined" and parsed and parsed.criteria:
            # 多路检索：每个子条件检索 sub_top_k → 轮询合并去重
            chunks, expanded_queries = self._multi_route_search(
                criteria=parsed.criteria,
                sub_top_k=sub_top_k,
                vector_weight=self.vector_weight,
                bm25_weight=self.bm25_weight,
            )
            enhanced_query = (
                json.dumps(expanded_queries, ensure_ascii=False)
                if expanded_queries else ""
            )
        else:
            # per_doc 或 combined 无 criteria 兜底：单路检索
            results = self.rag.search(
                query=enhanced_query or condition,
                top_k=top_k,
                vector_weight=self.vector_weight,
                bm25_weight=self.bm25_weight,
                deduplicate=False,
            )
            chunks = [
                _SearchChunkWrapper(
                    content=r.matched_chunk_content or r.document.content,
                    filename=r.document.filename,
                    score=r.score,
                    chunk_id=r.chunk_id,
                    visit_id=r.document.metadata.get("visit_id"),
                )
                for r in results
            ]

        step_retrieve_duration = (time.time() - step_retrieve_start) * 1000
        filter_logger.info(f"[Step 2/3] 检索完成 | chunks: {len(chunks)} | 耗时: {step_retrieve_duration:.0f}ms")

        # 内容质量过滤（保留，只过滤明显无意义chunk）
        original_count = len(chunks)
        chunks = [
            c for c in chunks
            if len(c.content.strip()) >= 10
            and c.content.strip() not in ('暂无', '未查', '无', '-', '—')
            and not all(ch in ' \t\n\r' for ch in c.content)
        ]
        if original_count > len(chunks):
            filter_logger.info(f"[Step 3/3] 内容质量过滤 | 剔除{original_count - len(chunks)}个低质量chunks")

        # 阈值过滤
        if score_threshold > 0:
            original_count = len(chunks)
            chunks = [c for c in chunks if c.score >= score_threshold]
            filter_logger.info(f"[Step 3/3] 阈值过滤 | score>={score_threshold} | {original_count} -> {len(chunks)} chunks")

        if not chunks:
            return {
                "matched": False,
                "matched_docs": [],
                "summary": "无相关文档"
            }

        # ========== Step 4: 构建判断文本 ==========
        # per_doc = 每个文档各自的 chunks 拼接后独立 LLM 判断
        # combined = 所有文档所有 chunks 合并后一次 LLM 判断
        if merge_mode == "per_doc":
            # 按 (visit_id, filename) 分组，每个文档单独判断
            from collections import defaultdict
            doc_groups = defaultdict(list)
            for chunk in chunks:
                key = (chunk.visit_id or "_unknown_", chunk.filename)
                doc_groups[key].append(chunk)

            # 每个文档的 chunks 拼接成一份文本
            doc_texts = []
            for (vid, fname), doc_chunks in doc_groups.items():
                text = f"[就诊号: {vid}]\n[文档: {fname}]\n"
                for c in doc_chunks:
                    text += f"【{c.filename}】\n{c.content[:3000]}\n\n"
                doc_texts.append((vid, fname, text))
            filter_logger.info(f"[Step 4/5] per_doc模式 | 文档数: {len(doc_texts)}")
        else:
            # combined: 先按文档合并chunks，再汇总所有文档
            from collections import defaultdict
            visit_groups = defaultdict(list)
            for chunk in chunks:
                vid = chunk.visit_id or "_unknown_"
                visit_groups[vid].append(chunk)
            doc_texts = None
            docs_text = ""
            for i, (vid, group_chunks) in enumerate(visit_groups.items()):
                # 按文档名合并chunks
                from collections import defaultdict
                doc_contents = defaultdict(list)
                for chunk in group_chunks:
                    doc_contents[chunk.filename].append(chunk)
                docs_text += f"\n=== 患者/就诊记录 {i+1} | 就诊号: {vid} ===\n"
                for fname, file_chunks in doc_contents.items():
                    merged = "\n".join(c.content[:3000] for c in file_chunks)
                    docs_text += f"【{fname}】\n{merged}\n\n"
            filter_logger.info(f"[Step 4/5] combined模式 | 患者数: {len(visit_groups)}")

        # ========== Step 5: LLM批量判断 ==========
        try:
            from microharness.ollama.prompts import format_batch_judge_prompt
            if merge_mode == "per_doc":
                # 每个文档单独调用 LLM
                doc_results = []
                for vid, fname, text in doc_texts:
                    system_prompt, user_prompt = format_batch_judge_prompt(condition, text)
                    response = self.ollama.chat(
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        temperature=0.1
                    )
                    rd = _try_parse_json(response.strip())
                    doc_results.append({"visit_id": vid, "filename": fname, "result": rd})
                    filter_logger.info(f"  文档 {fname}: matched={rd.get('matched')}")

                # 汇总：任一文档 matched=True 则整体 matched
                any_matched = any(r["result"].get("matched") for r in doc_results)
                result_data = {
                    "matched": any_matched,
                    "matched_docs": [r for r in doc_results if r["result"].get("matched")],
                    "summary": f"per_doc模式：{len(doc_results)}个文档，{'有' if any_matched else '无'}匹配"
                }
                # 直接跳到汇总步骤，避免执行 combined 模式的 response 解析代码
                return self._filter_batch_finish(result_data, enhanced_query, step1_duration, step_retrieve_duration, start_time, chunks)
            else:
                # combined: 一次调用
                system_prompt, user_prompt = format_batch_judge_prompt(condition, docs_text)
                response = self.ollama.chat(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                ],
                temperature=0.1
            )
            response = response.strip()
            result_data = _try_parse_json(response)
            if result_data.get("summary") == "JSON解析失败":
                filter_logger.error(f"JSON解析失败，原始响应: {response[:500]}")
        except Exception as e:
            filter_logger.error(f"LLM批量判断失败: {e}")
            result_data = {
                "matched": False,
                "matched_docs": [],
                "summary": f"LLM调用失败: {str(e)}"
            }

        total_duration = (time.time() - start_time) * 1000
        filter_logger.info(f"批量筛选完成 | 匹配: {result_data.get('matched', False)} | 总耗时: {total_duration:.0f}ms")
        filter_logger.info(f"=" * 50)

        # 附加增强查询信息
        result_data["enhanced_query"] = enhanced_query
        result_data["enhance_query_mode"] = self.enhance_query_mode

        # 附加各步骤耗时
        result_data["step_timings"] = {
            "step1_enhance_ms": step1_duration,
            "step2_retrieve_ms": step_retrieve_duration,
            "step3_judge_ms": (time.time() - start_time) * 1000 - step1_duration - step_retrieve_duration,
            "total_ms": total_duration
        }

        # 附加所有 chunks 分数信息到返回值
        result_data["all_chunks"] = [
            {
                "chunk_index": i + 1,
                "chunk_id": getattr(c, 'chunk_id', c.filename),  # 兼容 SearchResult 和 _SearchChunkWrapper
                "filename": c.filename,
                "score": round(c.score, 4),
                "content_preview": c.content[:1000]
            }
            for i, c in enumerate(chunks)
        ]

        return result_data

    @property
    def record_count(self) -> int:
        """Total number of indexed records."""
        return self.rag.document_count

    @property
    def is_ready(self) -> bool:
        """Check if filter is ready (has records and LLM available)."""
        return self.rag.is_ready and self.ollama.is_available()


# Default instance
filter = RecordFilter()