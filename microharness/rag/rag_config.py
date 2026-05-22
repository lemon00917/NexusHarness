"""
RAG Configuration for NexusHarness
===================================
Manages RAG settings including chunking and search modes.
"""

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


DEFAULT_CONFIG = {
    "chunk_mode": "length",      # "length" or "chapter"
    "chunk_size": 1500,           # characters per chunk
    "chunk_overlap": 200,         # overlap between chunks
    "search_mode": "vector",     # "vector" or "hybrid"
    "vector_weight": 0.7,         # weight for vector search in hybrid mode
    "bm25_weight": 0.3,           # weight for BM25 in hybrid mode
}


@dataclass
class RAGConfig:
    """RAG configuration settings."""
    chunk_mode: str = "length"
    chunk_size: int = 1500
    chunk_overlap: int = 200
    search_mode: str = "vector"
    vector_weight: float = 0.7
    bm25_weight: float = 0.3

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RAGConfig":
        return cls(**data)


def get_config_path() -> Path:
    """Get the path to the RAG config file."""
    return Path("rag_config.json")


def load_config() -> RAGConfig:
    """
    Load RAG configuration from disk.

    Returns:
        RAGConfig with loaded or default values
    """
    config_path = get_config_path()

    if not config_path.exists():
        return RAGConfig()

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return RAGConfig.from_dict(data)
    except (json.JSONDecodeError, TypeError, KeyError):
        return RAGConfig()


def save_config(config: RAGConfig) -> None:
    """
    Save RAG configuration to disk.

    Args:
        config: RAGConfig to save
    """
    config_path = get_config_path()

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config.to_dict(), f, ensure_ascii=False, indent=2)


def get_current_config() -> RAGConfig:
    """Get current RAG configuration (alias for load_config)."""
    return load_config()


def update_config(**kwargs) -> RAGConfig:
    """
    Update specific config values and save.

    Args:
        **kwargs: Config fields to update

    Returns:
        Updated RAGConfig
    """
    config = load_config()

    for key, value in kwargs.items():
        if hasattr(config, key):
            setattr(config, key, value)

    save_config(config)
    return config