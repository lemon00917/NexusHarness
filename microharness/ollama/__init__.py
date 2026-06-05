"""
Ollama Package
=============
Local LLM inference via Ollama API.
"""

from .client import OllamaClient, get_client, set_client
from .prompts import (
    JUDGE_SYSTEM_PROMPT,
    JUDGE_USER_PROMPT,
    PARSE_CONDITION_SYSTEM,
    PARSE_CONDITION_USER,
    format_judge_prompt,
    format_parse_prompt,
)

__all__ = [
    "OllamaClient",
    "get_client",
    "set_client",
    "JUDGE_SYSTEM_PROMPT",
    "JUDGE_USER_PROMPT",
    "PARSE_CONDITION_SYSTEM",
    "PARSE_CONDITION_USER",
    "format_judge_prompt",
    "format_parse_prompt",
]