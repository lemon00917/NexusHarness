"""
RAG Configuration for NexusHarness
===================================
Configuration management for RAG system settings.

Handles:
- Document chunking parameters
- Search mode selection
- Hybrid search weights
- Configuration persistence

Logging:
    Uses standard print for user-facing messages.
    Set RAG_CONFIG_DEBUG=1 for verbose loading output.
"""

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Dict, Any


# ──────────────────────── Constants ────────────────────────

# Default configuration file path
CONFIG_FILENAME = "rag_config.json"

# Valid query enhancement modes
ENHANCE_QUERY_SIMPLE = "simple"
ENHANCE_QUERY_LLM = "llm"
VALID_ENHANCE_QUERY_MODES = {ENHANCE_QUERY_SIMPLE, ENHANCE_QUERY_LLM}

# Valid chunking modes
CHUNK_MODE_LENGTH = "length"
CHUNK_MODE_CHAPTER = "chapter"
CHUNK_MODE_LLM = "llm"
CHUNK_MODE_FIELD_LLM = "field_llm"
VALID_CHUNK_MODES = {CHUNK_MODE_LENGTH, CHUNK_MODE_CHAPTER, CHUNK_MODE_LLM, CHUNK_MODE_FIELD_LLM}

# Valid search modes
SEARCH_MODE_VECTOR = "vector"
SEARCH_MODE_HYBRID = "hybrid"
VALID_SEARCH_MODES = {SEARCH_MODE_VECTOR, SEARCH_MODE_HYBRID}

# Bounds for configurable parameters
MIN_CHUNK_SIZE = 100
MAX_CHUNK_SIZE = 10000
MIN_OVERLAP = 0
MIN_WEIGHT = 0.0
MAX_WEIGHT = 1.0


# ──────────────────────── Helpers ────────────────────────

def _debug(msg: str) -> None:
    """Print debug message if RAG_CONFIG_DEBUG is set."""
    if os.getenv("RAG_CONFIG_DEBUG"):
        print(f"[Config] {msg}")


# ──────────────────────── Configuration Data Class ────────────────────────

@dataclass
class RAGConfig:
    """
    RAG system configuration with validation.

    Attributes:
        chunk_mode: Strategy for splitting documents
            - "length": Fixed-size chunks with intelligent boundaries
            - "chapter": Split by markdown/HTML headings
        chunk_size: Target characters per chunk (100-10000)
        chunk_overlap: Overlapping characters between chunks
        search_mode: Search strategy
            - "vector": Pure semantic search
            - "hybrid": Combine vector + keyword search
        vector_weight: Weight for vector scores in hybrid mode (0.0-1.0)
        bm25_weight: Weight for BM25 scores in hybrid mode (0.0-1.0)
    """

    # Query enhancement settings
    enhance_query_mode: str = ENHANCE_QUERY_SIMPLE  # "simple" = string join, "llm" = LLM expansion

    # Chunking settings
    chunk_mode: str = CHUNK_MODE_LENGTH
    chunk_size: int = 1500
    chunk_overlap: int = 200

    # Search settings
    search_mode: str = SEARCH_MODE_VECTOR  # Use vector search by default for multilingual support
    vector_weight: float = 1.0
    bm25_weight: float = 0.0

    def __post_init__(self):
        """Validate configuration after initialization."""
        if os.getenv("RAG_CONFIG_STRICT"):
            self.validate()

    def validate(self) -> None:
        """
        Validate all configuration parameters.

        Raises:
            ValueError: If any parameter is invalid
        """
        errors = []

        # Validate enhance query mode
        if self.enhance_query_mode not in VALID_ENHANCE_QUERY_MODES:
            errors.append(
                f"Invalid enhance_query_mode: '{self.enhance_query_mode}'. "
                f"Must be one of: {VALID_ENHANCE_QUERY_MODES}"
            )

        # Validate chunk mode
        if self.chunk_mode not in VALID_CHUNK_MODES:
            errors.append(
                f"Invalid chunk_mode: '{self.chunk_mode}'. "
                f"Must be one of: {VALID_CHUNK_MODES}"
            )

        # Validate chunk size
        if not (MIN_CHUNK_SIZE <= self.chunk_size <= MAX_CHUNK_SIZE):
            errors.append(
                f"chunk_size must be {MIN_CHUNK_SIZE}-{MAX_CHUNK_SIZE}, "
                f"got {self.chunk_size}"
            )

        # Validate chunk overlap
        if self.chunk_overlap < MIN_OVERLAP:
            errors.append(
                f"chunk_overlap must be >= {MIN_OVERLAP}, "
                f"got {self.chunk_overlap}"
            )
        elif self.chunk_overlap >= self.chunk_size:
            errors.append(
                f"chunk_overlap ({self.chunk_overlap}) must be less than "
                f"chunk_size ({self.chunk_size})"
            )

        # Validate search mode
        if self.search_mode not in VALID_SEARCH_MODES:
            errors.append(
                f"Invalid search_mode: '{self.search_mode}'. "
                f"Must be one of: {VALID_SEARCH_MODES}"
            )

        # Validate weights
        if not (MIN_WEIGHT <= self.vector_weight <= MAX_WEIGHT):
            errors.append(
                f"vector_weight must be {MIN_WEIGHT}-{MAX_WEIGHT}, "
                f"got {self.vector_weight}"
            )

        if not (MIN_WEIGHT <= self.bm25_weight <= MAX_WEIGHT):
            errors.append(
                f"bm25_weight must be {MIN_WEIGHT}-{MAX_WEIGHT}, "
                f"got {self.bm25_weight}"
            )

        # Validate hybrid search weights
        if self.search_mode == SEARCH_MODE_HYBRID:
            if self.vector_weight + self.bm25_weight == 0:
                errors.append(
                    "Hybrid search requires at least one weight > 0"
                )

        if errors:
            raise ValueError(
                f"Configuration validation failed ({len(errors)} errors):\n" +
                "\n".join(f"  - {e}" for e in errors)
            )

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RAGConfig":
        """
        Create configuration from dictionary.

        Only includes fields that exist in the dataclass,
        ignoring any extra keys.

        Args:
            data: Dictionary with configuration values

        Returns:
            Validated RAGConfig instance

        Raises:
            ValueError: If required values are invalid
        """
        # Filter only known fields
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered_data = {
            k: v for k, v in data.items()
            if k in known_fields
        }

        return cls(**filtered_data)

    @classmethod
    def create_default(cls) -> "RAGConfig":
        """Create configuration with default values."""
        return cls()

    def clone(self) -> "RAGConfig":
        """Create a deep copy of this configuration."""
        return RAGConfig(**self.to_dict())

    def describe(self) -> str:
        """
        Get a human-readable summary of the configuration.

        Returns:
            Formatted configuration description
        """
        lines = [
            "RAG Configuration:",
            f"  Query Enhancement: {self.enhance_query_mode} mode",
            f"  Chunking: {self.chunk_mode} mode, "
            f"{self.chunk_size} chars, {self.chunk_overlap} overlap",
            f"  Search: {self.search_mode} mode",
        ]

        if self.search_mode == SEARCH_MODE_HYBRID:
            lines.append(
                f"  Weights: vector={self.vector_weight}, "
                f"bm25={self.bm25_weight}"
            )

        return "\n".join(lines)


# ──────────────────────── File I/O Functions ────────────────────────

def get_config_path(config_dir: Optional[Path] = None) -> Path:
    """
    Get the path to the configuration file.

    Args:
        config_dir: Optional directory containing the config file.
                   Defaults to configs/ in project root.

    Returns:
        Path to config file
    """
    if config_dir is None:
        config_dir = Path(__file__).parent.parent.parent / "configs"
    return config_dir / CONFIG_FILENAME


def load_config(config_path: Optional[Path] = None) -> RAGConfig:
    """
    Load RAG configuration from disk.

    If the config file doesn't exist or is corrupted,
    returns default configuration without raising exceptions.

    Args:
        config_path: Path to config file. Uses default path if None.

    Returns:
        RAGConfig instance (defaults if file missing/invalid)
    """
    if config_path is None:
        config_path = get_config_path()

    # Return defaults if no config file exists
    if not config_path.exists():
        _debug(f"No config file found at {config_path}, using defaults")
        return RAGConfig.create_default()

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        config = RAGConfig.from_dict(data)
        _debug(f"Loaded configuration from {config_path}")
        return config

    except FileNotFoundError:
        _debug(f"Config file not found: {config_path}, using defaults")
        return RAGConfig.create_default()

    except json.JSONDecodeError as e:
        _debug(f"Invalid JSON in config file: {e}, using defaults")
        return RAGConfig.create_default()

    except ValueError as e:
        _debug(f"Invalid config values: {e}, using defaults")
        return RAGConfig.create_default()

    except Exception as e:
        _debug(f"Unexpected error loading config: {e}, using defaults")
        return RAGConfig.create_default()


def save_config(
    config: RAGConfig,
    config_path: Optional[Path] = None
) -> None:
    """
    Save RAG configuration to disk.

    Args:
        config: RAGConfig to save
        config_path: Path to save to. Uses default path if None.

    Raises:
        ValueError: If config is invalid
        OSError: If file cannot be written
    """
    if config_path is None:
        config_path = get_config_path()

    # Validate before saving
    config.validate()

    # Create parent directories if needed
    config_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config.to_dict(), f, ensure_ascii=False, indent=2)
        _debug(f"Saved configuration to {config_path}")

    except OSError as e:
        raise OSError(f"Failed to save config to {config_path}: {e}")


def create_default_config_file(config_path: Optional[Path] = None) -> RAGConfig:
    """
    Create a default configuration file if one doesn't exist.

    Args:
        config_path: Path where to create the config file

    Returns:
        The default RAGConfig instance
    """
    if config_path is None:
        config_path = get_config_path()

    config = RAGConfig.create_default()

    if not config_path.exists():
        save_config(config, config_path)
        print(f"[Config] Created default config at {config_path}")

    return config


# ──────────────────────── Convenience Functions ────────────────────────

def get_current_config() -> RAGConfig:
    """
    Get current RAG configuration.

    Alias for load_config().

    Returns:
        Current RAGConfig
    """
    return load_config()


def update_config(**kwargs) -> RAGConfig:
    """
    Update specific configuration values and save.

    Only updates fields that exist in RAGConfig.
    Validates the entire configuration after updates.

    Args:
        **kwargs: Config fields to update (e.g., chunk_size=2000)

    Returns:
        Updated and validated RAGConfig

    Raises:
        ValueError: If updated values are invalid
        AttributeError: If a field doesn't exist in RAGConfig

    Examples:
        # Update single field
        config = update_config(chunk_size=2000)

        # Switch to hybrid search
        config = update_config(
            search_mode="hybrid",
            vector_weight=0.6,
            bm25_weight=0.4
        )
    """
    config = load_config()

    # Validate field names first
    valid_fields = {f.name for f in RAGConfig.__dataclass_fields__.values()}
    for key in kwargs:
        if key not in valid_fields:
            raise AttributeError(
                f"Unknown config field: '{key}'. "
                f"Valid fields: {sorted(valid_fields)}"
            )

    # Update values
    for key, value in kwargs.items():
        setattr(config, key, value)

    # Validate and save
    config.validate()
    save_config(config)

    return config


def reset_config(config_path: Optional[Path] = None) -> RAGConfig:
    """
    Reset configuration to defaults and save.

    Args:
        config_path: Path to config file. Uses default path if None.

    Returns:
        Default RAGConfig
    """
    config = RAGConfig.create_default()
    save_config(config, config_path)
    _debug("Reset to default configuration")
    return config


def preview_config() -> str:
    """
    Get a formatted preview of current configuration.

    Returns:
        Human-readable configuration summary
    """
    config = load_config()
    return config.describe()