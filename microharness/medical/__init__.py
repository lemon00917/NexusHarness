"""
Medical Module
==============
Provides medical document parsing, field catalog, and filtering capabilities.

Usage:
    from microharness.medical.field_catalog import get_catalog
    catalog = get_catalog()
"""

# Lazy imports to avoid pulling in heavy RAG dependencies at module load
_medical_kb = None

def get_medical_kb():
    """Lazy-load MedicalRAG instance."""
    global _medical_kb
    if _medical_kb is None:
        from microharness.medical.knowledge_base import MedicalRAG
        _medical_kb = MedicalRAG()
    return _medical_kb

__all__ = ["get_medical_kb"]
