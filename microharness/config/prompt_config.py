"""
Prompt Configuration Manager
=============================

Manages dynamic prompt templates loaded from JSON config file.

Allows runtime modification of prompts without code changes.
"""

import json
from pathlib import Path
from typing import Dict, Any, Optional, List

# Default config file location
PROMPT_CONFIG_FILE = Path(__file__).parent / "prompts.json"


def load_prompt_config() -> Dict[str, Any]:
    """
    Load prompt configuration from JSON file.

    Returns:
        Dict containing prompt configuration
    """
    if not PROMPT_CONFIG_FILE.exists():
        return get_default_config()

    try:
        with open(PROMPT_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return get_default_config()


def save_prompt_config(config: Dict[str, Any]) -> None:
    """
    Save prompt configuration to JSON file.

    Args:
        config: Configuration dict to save
    """
    PROMPT_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROMPT_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_default_config() -> Dict[str, Any]:
    """
    Get default prompt configuration.

    Returns:
        Default configuration dict
    """
    return {
        "system_prompt": {
            "base_template": "You are a helpful AI assistant inside a safe harness.\n\nAvailable tools:\n{tool_block}\n\nRules:\n{rule_block}\n\nWorkspace: {workspace}",
            "variables": {
                "rule_block": "- You may write files and run Python code inside the sandbox\n- You MUST NOT run shell commands that delete, move, or overwrite files outside the workspace\n- Always explain what you are about to do before doing it\n- If a task is unclear, ask for clarification instead of guessing\n- Keep code clean, readable, and well-commented",
                "workspace": "/tmp/sandbox/"
            }
        },
        "intents": {},
        "memory": {
            "enabled": True,
            "max_records": 5
        },
        "rag": {
            "auto_inject": True,
            "top_k": 2,
            "timeout_ms": 500
        }
    }


def validate_prompt_config(config: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    """
    Validate prompt configuration structure.

    Args:
        config: Configuration to validate

    Returns:
        (is_valid, error_message)
    """
    # Check required top-level keys
    required_keys = ["system_prompt", "intents", "memory", "rag"]
    for key in required_keys:
        if key not in config:
            return False, f"Missing required key: {key}"

    # Validate system_prompt structure
    sp = config.get("system_prompt", {})
    if "base_template" not in sp:
        return False, "Missing system_prompt.base_template"

    # Validate intents structure
    intents = config.get("intents", {})
    for intent_name, intent_config in intents.items():
        if not isinstance(intent_config, dict):
            return False, f"Intent '{intent_name}' must be a dict"

        if "template" not in intent_config:
            return False, f"Intent '{intent_name}' missing template"

        if "keywords" in intent_config and not isinstance(intent_config["keywords"], list):
            return False, f"Intent '{intent_name}' keywords must be a list"

    # Validate memory settings
    memory = config.get("memory", {})
    if "max_records" in memory:
        if not isinstance(memory["max_records"], int) or memory["max_records"] < 0:
            return False, "memory.max_records must be non-negative integer"

    # Validate rag settings
    rag = config.get("rag", {})
    if "top_k" in rag:
        if not isinstance(rag["top_k"], int) or rag["top_k"] < 1:
            return False, "rag.top_k must be positive integer"

    return True, None


def get_intent_by_query(query: str, config: Dict[str, Any] = None) -> tuple[Optional[str], Optional[Dict]]:
    """
    Detect intent based on query keywords.

    Args:
        query: User query string
        config: Prompt config (loads from file if None)

    Returns:
        (intent_name, intent_config) or (None, None) if no match
    """
    if not query:
        return None, None

    if config is None:
        config = load_prompt_config()

    query_lower = query.lower()

    intents = config.get("intents", {})
    for intent_name, intent_config in intents.items():
        if not intent_config.get("enabled", True):
            continue

        keywords = intent_config.get("keywords", [])
        for keyword in keywords:
            if keyword.lower() in query_lower:
                return intent_name, intent_config

    return None, None


def get_intent_templates(config: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    """
    Get list of all intent templates with metadata.

    Args:
        config: Prompt config (loads from file if None)

    Returns:
        List of intent config dicts with name/description/enabled
    """
    if config is None:
        config = load_prompt_config()

    intents = config.get("intents", {})

    result = []
    for intent_name, intent_config in intents.items():
        result.append({
            "id": intent_name,
            "name": intent_config.get("name", intent_name),
            "description": intent_config.get("description", ""),
            "keywords": intent_config.get("keywords", []),
            "template_preview": intent_config.get("template", "")[:100] + "...",
            "enabled": intent_config.get("enabled", True),
            "rag_enabled": intent_config.get("rag", {}).get("enabled", False),
        })

    return result


def update_intent(intent_name: str, intent_config: Dict[str, Any], config: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    Update or add an intent configuration.

    Args:
        intent_name: Name of intent to update
        intent_config: New config for intent
        config: Prompt config (loads from file if None, then saves)

    Returns:
        Updated full configuration
    """
    if config is None:
        config = load_prompt_config()

    config["intents"][intent_name] = intent_config
    save_prompt_config(config)

    return config


def delete_intent(intent_name: str, config: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    Delete an intent configuration.

    Args:
        intent_name: Name of intent to delete
        config: Prompt config (loads from file if None, then saves)

    Returns:
        Updated full configuration
    """
    if config is None:
        config = load_prompt_config()

    if intent_name in config["intents"]:
        del config["intents"][intent_name]
        save_prompt_config(config)

    return config


def format_template(template: str, variables: Dict[str, str]) -> str:
    """
    Format a template string with variables.

    Args:
        template: Template string with {variable} placeholders
        variables: Dict of variable name -> value

    Returns:
        Formatted string
    """
    try:
        return template.format(**variables)
    except KeyError as e:
        # Missing variable - return template as-is
        return template