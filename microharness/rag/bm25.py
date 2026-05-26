"""
BM25 Keyword Search for NexusHarness RAG
=========================================
BM25 ranking algorithm for keyword-based document retrieval.
"""

import math
import re
from typing import List, Tuple, Dict, Optional


class BM25:
    """
    BM25 ranking algorithm for information retrieval.

    BM25 is a probabilistic ranking function that computes relevance scores
    between queries and documents based on term frequency, inverse document
    frequency, and document length normalization.

    Attributes:
        k1: Controls term frequency saturation (higher = more weight to repeated terms)
        b: Controls length normalization (0 = no normalization, 1 = full normalization)
    """

    # Smoothing constants for IDF calculation
    IDF_SMOOTH = 0.5  # Prevents division by zero

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        """
        Initialize BM25 with configurable parameters.

        Args:
            k1: Term frequency saturation parameter (typical range: 1.2-2.0)
            b: Length normalization parameter (typical value: 0.75)
        """
        # Hyperparameters
        self.k1 = k1
        self.b = b

        # Document store
        self._documents: List[str] = []
        self._doc_lengths: List[int] = []
        self._doc_term_freqs: List[Dict[str, int]] = []
        self._num_docs: int = 0

        # Document statistics
        self._avg_doc_length: float = 0.0

        # Term statistics
        self._doc_freqs: Dict[str, int] = {}  # term -> number of documents containing term
        self._vocabulary: Dict[str, int] = {}  # term -> sorted index

    # ──────────────────────── Public Methods ────────────────────────

    def add_documents(self, texts: List[str]) -> None:
        """
        Index a collection of documents for searching.

        This method processes all documents, computing term frequencies
        and document frequencies needed for BM25 scoring.

        Args:
            texts: List of document strings to index
        """
        if not texts:
            raise ValueError("Cannot index empty document list")

        self._num_docs = len(texts)
        self._documents = texts
        self._doc_lengths = []
        self._doc_term_freqs = []
        self._doc_freqs = {}

        # Process each document
        for doc_text in texts:
            tokens = self._tokenize(doc_text)
            term_counts = self._count_terms(tokens)

            # Store document statistics
            self._doc_lengths.append(len(tokens))
            self._doc_term_freqs.append(term_counts)

            # Update global document frequencies
            for term in term_counts:
                self._doc_freqs[term] = self._doc_freqs.get(term, 0) + 1

        # Compute average document length
        total_length = sum(self._doc_lengths)
        self._avg_doc_length = total_length / self._num_docs

        # Build sorted vocabulary for term lookup
        self._vocabulary = {
            term: idx
            for idx, term in enumerate(sorted(self._doc_freqs.keys()))
        }

    def search(self, query: str, top_k: int = 10) -> List[Tuple[int, float]]:
        """
        Search for documents matching the query.

        Args:
            query: Free text search query
            top_k: Maximum number of results to return

        Returns:
            List of (document_index, score) tuples, sorted by score descending
        """
        if self._is_empty():
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        # Score all documents
        results = [
            (doc_idx, self._score_document(query_tokens, doc_idx))
            for doc_idx in range(self._num_docs)
        ]

        # Sort by score (highest first) and return top-k
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def get_scores(self, query: str) -> List[float]:
        """
        Get BM25 scores for all documents.

        Args:
            query: Search query text

        Returns:
            List of scores, one per indexed document
        """
        if self._is_empty():
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return [0.0] * self._num_docs

        return [
            self._score_document(query_tokens, doc_idx)
            for doc_idx in range(self._num_docs)
        ]

    @property
    def stats(self) -> Dict:
        """Get index statistics."""
        return {
            "num_documents": self._num_docs,
            "vocabulary_size": len(self._vocabulary),
            "avg_doc_length": round(self._avg_doc_length, 2),
            "parameters": {"k1": self.k1, "b": self.b}
        }

    @property
    def vocabulary(self) -> Dict[str, int]:
        """Get the vocabulary mapping (term -> index)."""
        return dict(self._vocabulary)

    # ──────────────────────── Private Methods ────────────────────────

    def _is_empty(self) -> bool:
        """Check if the index is empty."""
        return self._num_docs == 0

    def _tokenize(self, text: str) -> List[str]:
        """
        Tokenize text supporting both English and Chinese.

        Strategy:
        - English: Extract alphabetic words, filter short ones (< 3 chars)
        - Chinese: Split into individual characters
        - Mixed: Process both, combine results

        Examples:
            "Machine Learning 101" -> ["machine", "learning"]
            "机器学习" -> ["机", "器", "学", "习"]
        """
        text = text.lower()

        # Extract English words (alphabetic sequences)
        english_tokens = [
            word for word in re.findall(r'[a-z]+', text)
            if len(word) > 2  # Filter out short words like "a", "an", "is"
        ]

        # Extract Chinese characters
        chinese_tokens = [
            char for char in text
            if self._is_chinese_char(char)
        ]

        return english_tokens + chinese_tokens

    @staticmethod
    def _is_chinese_char(char: str) -> bool:
        """Check if a character is a Chinese character."""
        return '\u4e00' <= char <= '\u9fff'

    @staticmethod
    def _count_terms(tokens: List[str]) -> Dict[str, int]:
        """
        Count term frequencies in a token list.

        Args:
            tokens: List of token strings

        Returns:
            Dictionary mapping term -> frequency
        """
        term_counts = {}
        for token in tokens:
            term_counts[token] = term_counts.get(token, 0) + 1
        return term_counts

    def _compute_idf(self, term: str) -> float:
        """
        Compute Inverse Document Frequency for a term.

        IDF measures how rare a term is across all documents.
        Uses smoothing to handle edge cases.

        Formula:
            IDF = log((N - df + 0.5) / (df + 0.5) + 1)

        Where:
            N = total documents
            df = documents containing the term

        Returns:
            IDF score (0.0 if term not in vocabulary)
        """
        if term not in self._doc_freqs:
            return 0.0

        df = self._doc_freqs[term]  # Document frequency
        smoothing = self.IDF_SMOOTH

        # Compute IDF with smoothing
        # - "N - df + 0.5" gives higher weight to rare terms
        # - "df + 0.5" prevents division by zero
        # - "+ 1" ensures non-negative values
        idf = math.log(
            (self._num_docs - df + smoothing) /
            (df + smoothing) + 1
        )

        return idf

    def _compute_tf(self, term_freq: int, doc_length: int) -> float:
        """
        Compute normalized Term Frequency using BM25 formula.

        The normalization controls how much term repetition matters
        and adjusts for document length.

        Formula:
            TF = tf * (k1 + 1) / (tf + k1 * length_norm)

        Where:
            tf = raw term frequency
            length_norm = 1 - b + b * (doc_length / avg_length)

        Returns:
            Normalized term frequency score
        """
        if term_freq == 0:
            return 0.0

        # Length normalization factor
        # - When b=0: no normalization (all docs treated same)
        # - When b=1: full normalization (long docs penalized)
        length_ratio = doc_length / max(self._avg_doc_length, 1)
        length_norm = 1 - self.b + self.b * length_ratio

        # BM25 term frequency formula
        numerator = term_freq * (self.k1 + 1)
        denominator = term_freq + self.k1 * length_norm

        return numerator / denominator

    def _score_document(self, query_tokens: List[str], doc_idx: int) -> float:
        """
        Calculate BM25 relevance score for a single document.

        Score = sum over query terms:
            IDF(term) * TF(term, document)

        Args:
            query_tokens: Tokenized query
            doc_idx: Index of the document to score

        Returns:
            BM25 relevance score (higher = more relevant)
        """
        doc_length = self._doc_lengths[doc_idx]
        doc_term_freqs = self._doc_term_freqs[doc_idx]

        total_score = 0.0

        # Accumulate score for each query term
        for term in query_tokens:
            # Skip terms not in vocabulary
            if term not in self._doc_freqs:
                continue

            # Get term statistics
            term_freq_in_doc = doc_term_freqs.get(term, 0)
            if term_freq_in_doc == 0:
                continue

            # Compute BM25 components
            idf = self._compute_idf(term)
            tf = self._compute_tf(term_freq_in_doc, doc_length)

            # Accumulate weighted score
            total_score += idf * tf

        return total_score