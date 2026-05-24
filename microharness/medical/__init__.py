"""
Medical Knowledge Base Module
============================

Provides medical document parsing, chunking, and RAG capabilities.

Usage:
    from microharness.medical import medical_kb

    # Add medical document
    medical_kb.add_document(content, "drug_guide.md", metadata={"medical_type": "药品"})

    # Search
    results = medical_kb.search("阿司匹林", top_k=3, filter_type="药品")

    # Use as agent tool
    from microharness.medical.tools import medical_lookup
"""

from microharness.medical.knowledge_base import MedicalRAG

# Global instance
medical_kb = MedicalRAG()

__all__ = ["medical_kb", "MedicalRAG"]