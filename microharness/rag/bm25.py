"""
BM25 Keyword Search for NexusHarness RAG
=========================================
BM25 ranking algorithm for keyword-based document retrieval.
"""

import math
import re
from typing import List, Tuple


class BM25:
    """
    BM25 ranking algorithm implementation.

    BM25 is a probabilistic ranking function used for information retrieval.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        """
        Initialize BM25.

        Args:
            k1: Term frequency saturation parameter (typical: 1.2-2.0)
            b: Length normalization parameter (typical: 0.75)
        """
        self.k1 = k1
        self.b = b
        self.documents: List[str] = []
        self.doc_lengths: List[int] = []
        self.avg_doc_length: float = 0.0
        self.doc_freqs: dict = {}  # term -> doc frequency
        self.vocabulary: dict = {}  # term -> index
        self.N: int = 0  # total documents

    def _tokenize(self, text: str) -> List[str]:
        """Simple tokenization."""
        text = text.lower()
        words = re.findall(r'\w+', text)
        return [w for w in words if len(w) > 2]

    def add_documents(self, texts: List[str]) -> None:
        """
        Add documents to the BM25 index.

        Args:
            texts: List of document texts
        """
        self.documents = texts
        self.N = len(texts)
        self.doc_lengths = []
        self.doc_freqs = {}
        self.vocabulary = {}

        for doc_text in texts:
            tokens = self._tokenize(doc_text)
            self.doc_lengths.append(len(tokens))

            # Count unique terms per doc
            unique_terms = set(tokens)
            for term in unique_terms:
                self.doc_freqs[term] = self.doc_freqs.get(term, 0) + 1

        self.avg_doc_length = sum(self.doc_lengths) / self.N if self.N > 0 else 0

        # Build vocabulary
        self.vocabulary = {term: idx for idx, term in enumerate(sorted(self.doc_freqs.keys()))}

    def search(self, query: str, top_k: int = 10) -> List[Tuple[int, float]]:
        """
        Search documents by query, returning top-k document indices and scores.

        Args:
            query: Search query
            top_k: Number of top results to return

        Returns:
            List of (doc_index, score) tuples sorted by score descending
        """
        if not self.documents or not self.vocabulary:
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        scores = []
        for doc_idx in range(self.N):
            score = self._calculate_score(query_tokens, doc_idx)
            scores.append((doc_idx, score))

        # Sort by score descending
        scores.sort(key=lambda x: x[1], reverse=True)

        # Filter zero scores and return top_k
        return [(idx, score) for idx, score in scores if score > 0][:top_k]

    def _calculate_score(self, query_tokens: List[str], doc_idx: int) -> float:
        """
        Calculate BM25 score for a single document.

        Args:
            query_tokens: Tokenized query
            doc_idx: Document index

        Returns:
            BM25 score
        """
        doc_text = self.documents[doc_idx]
        doc_tokens = self._tokenize(doc_text)
        doc_len = len(doc_tokens)
        doc_tf = {}

        # Count term frequencies in this document
        for token in doc_tokens:
            doc_tf[token] = doc_tf.get(token, 0) + 1

        score = 0.0
        for term in query_tokens:
            if term not in self.doc_freqs:
                continue

            df = self.doc_freqs[term]
            idf = math.log((self.N - df + 0.5) / (df + 0.5) + 1)

            tf = doc_tf.get(term, 0)
            numerator = tf * (self.k1 + 1)
            denominator = tf + self.k1 * (1 - self.b + self.b * doc_len / max(self.avg_doc_length, 1))

            score += idf * (numerator / denominator)

        return score

    def get_scores(self, query: str) -> List[float]:
        """
        Get BM25 scores for all documents matching query.

        Args:
            query: Search query

        Returns:
            List of scores per document
        """
        if not self.documents:
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return [0.0] * len(self.documents)

        scores = []
        for doc_idx in range(self.N):
            scores.append(self._calculate_score(query_tokens, doc_idx))

        return scores