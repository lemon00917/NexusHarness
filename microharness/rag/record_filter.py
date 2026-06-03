"""
Medical Record Filter
======================
RAG-based medical record filtering with LLM reasoning judgment.
"""

import time
from dataclasses import dataclass
from typing import List, Optional

from microharness.ollama import OllamaClient, get_client
from microharness.rag.rag import SimpleRAG, SearchResult
from microharness.ollama.prompts import format_judge_prompt, format_parse_prompt, format_enhance_query_prompt

# Logging
from microharness.observability.logger import filter_logger, rag_logger, ollama_logger


@dataclass
class ParsedCondition:
    """Structured representation of a filter condition."""
    original: str
    keywords: List[str]
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
        index_dir: str = "cache/rag_index",
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
        self.model = model
        self.enhance_query_mode = enhance_query_mode
        self.enhance_model = enhance_model

        # Initialize RAG - reuse SimpleRAG with custom collection
        self.rag = SimpleRAG(index_dir=index_dir)
        self.rag.load_index()

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
            bm25_weight=self.bm25_weight,
            visit_id=visit_id
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

        Args:
            condition: Natural language condition

        Returns:
            ParsedCondition with keywords and summary
        """
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
            logic = parsed_data.get("logic", "AND")

            return ParsedCondition(
                original=condition,
                keywords=keywords[:8],  # Limit to 8 keywords
                summary=f"[{logic}] " + " | ".join(criteria[:5])
            )
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
        if parsed.keywords:
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
        # 使用检索到的chunk内容判断，如果没有则用文档内容
        retrieved_chunk = candidate.matched_chunk_content or candidate.document.content
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

    def list_records(self) -> List[dict]:
        """List all indexed records."""
        return self.rag.list_documents()

    def filter_batch(
        self,
        condition: str,
        visit_id: Optional[str] = None,
        top_k: int = 20,
        score_threshold: float = 0.0,
    ) -> dict:
        """
        Batch filter: retrieve chunks then judge all together.

        Flow:
            Step 1: Parse condition with LLM (extract keywords/summary)
            Step 2: Build enhanced query from original + parsed keywords
            Step 3: Retrieve top-k chunks via RAG (no dedup, keeping all chunks)
            Step 4: Send ALL chunks to LLM for batch judgment

        Args:
            condition: Natural language filter condition
            visit_id: Optional visit/patient ID to filter documents
            top_k: Number of chunks to retrieve for batch judgment
            score_threshold: Minimum vector similarity score (0.0-1.0) to include chunk

        Returns:
            Dict with matched status, matched docs info, and summary
        """
        import json
        start_time = time.time()
        filter_logger.info(f"=" * 50)
        filter_logger.info(f"批量筛选 | 条件: {condition[:50]}... | 就诊号: {visit_id or '全部'} | top_k: {top_k}")

        # ========== Step 1+2: 解析并构建增强查询 ==========
        # LLM模式: 一步到位，跳过parse直接enhance
        # Simple模式: 先parse提取关键词，再拼接
        step1_start = time.time()
        if self.enhance_query_mode == "llm":
            # LLM模式：直接一步生成增强查询，跳过parse
            enhanced_query = self._build_enhanced_query_llm(condition, None)
            step1_duration = (time.time() - step1_start) * 1000
            step2_duration = 0
            filter_logger.info(f"[Step 1/2] LLM增强查询一步完成 | 增强查询: {enhanced_query[:80]}... | 耗时: {step1_duration:.0f}ms")
        else:
            # Simple模式：先parse关键词，再拼接
            parsed = self._parse_condition(condition)
            step1_duration = (time.time() - step1_start) * 1000
            step2_start = time.time()
            enhanced_query = self._build_enhanced_query_simple(condition, parsed)
            step2_duration = (time.time() - step2_start) * 1000
            filter_logger.info(f"[Step 1/2] 解析+构建完成 | 解析: {step1_duration:.0f}ms | 拼接: {step2_duration:.0f}ms | 关键词: {parsed.keywords}")

        # ========== Step 2/3: 检索 chunks（LLM模式）或 Step 3/3（Simple模式）============
        step_retrieve_start = time.time()
        results = self.rag.search(
            query=enhanced_query,
            top_k=top_k,
            vector_weight=self.vector_weight,
            bm25_weight=self.bm25_weight
        )
        # 适配 search() 返回 SearchResult[document, score, matched_chunk] → 同 filter() 格式
        chunks = [
            _SearchChunkWrapper(r.document.content, r.document.filename, r.score)
            for r in results
        ]
        step_retrieve_duration = (time.time() - step_retrieve_start) * 1000
        filter_logger.info(f"[Step {3 if self.enhance_query_mode != 'llm' else 2}/3] 检索chunks完成 | chunks: {len(chunks)} | 耗时: {step_retrieve_duration:.0f}ms")

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

        # ========== Step 4: LLM批量判断 ==========
        # 构建文档列表字符串（包含chunk索引供LLM引用）
        docs_text = ""
        for i, chunk in enumerate(chunks):
            docs_text += f"\n--- Chunk{i+1} ---\n"
            docs_text += f"[文件名: {chunk.filename}]\n"
            docs_text += f"[Chunk索引: {i+1}]\n"
            docs_text += f"[内容]\n{chunk.content[:2000]}\n"

        from microharness.ollama.prompts import format_batch_judge_prompt
        system_prompt, user_prompt = format_batch_judge_prompt(condition, docs_text)

        try:
            response = self.ollama.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.1
            )
            response = response.strip()

            # 解析JSON响应
            json_text = response
            if "```json" in json_text:
                parts = json_text.split("```json")
                if len(parts) >= 2:
                    json_text = parts[1].split("```")[0]
            elif "```" in json_text:
                parts = json_text.split("```")
                if len(parts) >= 2:
                    json_text = parts[1]

            json_text = json_text.strip()
            try:
                result_data = json.loads(json_text)
            except json.JSONDecodeError as je:
                # 记录原始响应供调试
                filter_logger.error(f"JSON解析失败，原始响应: {json_text[:500]}")
                # 尝试修复常见问题后重试
                import re
                # 尝试修复缺少逗号的问题（常见于chunk_content中有多行文本）
                fixed = json_text
                # 修复：行尾的"}{"改为"},\n{"
                fixed = re.sub(r'(?<=[^,\n{])\n(?=\{\s*"[^"]+":)', ',', fixed)
                try:
                    result_data = json.loads(fixed)
                    filter_logger.info("JSON修复成功")
                except Exception:
                    # 最后尝试：截取第一个完整的JSON对象（从第一个{到最后一个}）
                    first_brace = json_text.find('{')
                    last_brace = json_text.rfind('}')
                    if first_brace >= 0 and last_brace > first_brace:
                        candidate = json_text[first_brace:last_brace + 1]
                        try:
                            result_data = json.loads(candidate)
                            filter_logger.info("JSON截断修复成功")
                        except Exception:
                            result_data = {
                                "matched": False,
                                "matched_docs": [],
                                "summary": f"JSON解析失败: {str(je)[:100]}"
                            }
                    else:
                        result_data = {
                            "matched": False,
                            "matched_docs": [],
                            "summary": f"JSON解析失败: {str(je)[:100]}"
                        }

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
                "content_preview": c.content[:200]
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