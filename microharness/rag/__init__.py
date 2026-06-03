"""
RAG Module
==========
Medical record filtering with RAG + LLM reasoning.
"""

from microharness.ollama import OllamaClient, get_client, set_client
from .record_filter import RecordFilter, FilterResult
from .rag import SimpleRAG, Document, SearchResult

__all__ = [
    "OllamaClient",
    "get_client",
    "set_client",
    "RecordFilter",
    "FilterResult",
    "SimpleRAG",
    "Document",
    "SearchResult",
]