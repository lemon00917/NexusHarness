"""
Skill Manager Module
====================
Auto-discovers, loads, and registers skill tools from skills/ directory.

This module integrates with skill_common for parsing and provides
LangChain Tool registration for skill execution.

Usage:
    from microharness.skills.skill_manager import (
        load_skills,
        get_skills,
        get_skill_safety_map
    )

    # Load all skills
    load_skills()

    # Get registered tools
    tools = get_skills()

    # Check safety levels
    safety_map = get_skill_safety_map()
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Callable, Any, Union, Tuple

from langchain_core.tools import Tool

from microharness.skills.skill_common import (
    # Constants
    PATH_PLACEHOLDER,
    SKILL_COMMAND_TIMEOUT,
    SAFETY_AUTO_APPROVE,
    SAFETY_ALWAYS_CONFIRM,
    SAFETY_KEYWORD_CHECK,
    VALID_SAFETY_LEVELS,
    # Functions
    parse_skill_md,
    logger as common_logger,
)

# Re-export constants for backward compatibility
__all__ = [
    "SAFETY_AUTO_APPROVE",
    "SAFETY_ALWAYS_CONFIRM",
    "SAFETY_KEYWORD_CHECK",
    "VALID_SAFETY_LEVELS",
    "SKILL_COMMAND_TIMEOUT",
    "get_skills",
    "get_skill_safety_map",
    "load_skills",
    "clear_skills",
    "remove_skill",
    "is_skill_loaded",
    "get_skill_by_name",
]

# ── Logger Setup ─────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)

# ── Global State ──────────────────────────────────────────────────────

_registered_tools: List[Tool] = []
_skill_safety_levels: Dict[str, str] = {}
_loaded = False


# ── Public API ─────────────────────────────────────────────────────────

def get_skills() -> List[Tool]:
    """
    Return all registered skill tools.

    Returns:
        List of LangChain Tool objects
    """
    return list(_registered_tools)


def get_skill_safety_map() -> Dict[str, str]:
    """
    Return the complete safety level map for all skill tools.

    Returns:
        Dictionary mapping tool names to safety levels
    """
    return dict(_skill_safety_levels)


def load_skills(skills_dir: Optional[Union[str, Path]] = None, force: bool = False) -> None:
    """
    Discover and load all skills from the skills directory.

    Args:
        skills_dir: Path to skills directory. Defaults to <project>/skills/
        force: If True, reload even if already loaded
    """
    global _registered_tools, _skill_safety_levels, _loaded

    if _loaded and not force:
        logger.debug("[SkillManager] Skills already loaded, use force=True to reload")
        return

    # Reset state if forcing reload
    if force:
        clear_skills()

    skills_path = _resolve_skills_path(skills_dir)
    if not skills_path.exists():
        logger.warning(f"[SkillManager] Skills directory not found: {skills_path}")
        return

    skill_dirs = _find_skill_directories(skills_path)
    logger.info(f"[SkillManager] Found {len(skill_dirs)} skill directory(ies)")

    loaded_count = 0
    for skill_dir in skill_dirs:
        if _load_skill_from_directory(skill_dir):
            loaded_count += 1

    _loaded = True
    logger.info(f"[SkillManager] Successfully loaded {loaded_count} skill(s)")


def clear_skills() -> None:
    """Clear all registered skills. Useful for testing or reloading."""
    global _registered_tools, _skill_safety_levels, _loaded
    _registered_tools = []
    _skill_safety_levels = {}
    _loaded = False
    logger.debug("[SkillManager] All skills cleared")


def remove_skill(name: str) -> bool:
    """
    Remove a specific skill tool by name.

    Args:
        name: Name of the tool to remove

    Returns:
        True if tool was found and removed, False otherwise
    """
    if not name:
        logger.warning("[SkillManager] Cannot remove skill with empty name")
        return False

    global _registered_tools, _skill_safety_levels
    original_len = len(_registered_tools)
    _registered_tools = [t for t in _registered_tools if t.name != name]
    removed = len(_registered_tools) < original_len

    if removed:
        _skill_safety_levels.pop(name, None)
        logger.info(f"[SkillManager] Removed tool: {name}")

    return removed


def is_skill_loaded(skill_name: str) -> bool:
    """
    Check if a skill is currently loaded.

    Args:
        skill_name: Skill name or slug

    Returns:
        True if any tool from this skill is loaded
    """
    if not skill_name:
        return False

    # Normalize skill name to tool name prefix
    tool_prefix = skill_name.lower().replace("-", "_").replace(" ", "_")
    return any(t.name.startswith(tool_prefix) for t in _registered_tools)


def get_skill_by_name(tool_name: str) -> Optional[Tool]:
    """
    Get a tool by its exact name.

    Args:
        tool_name: Exact name of the tool

    Returns:
        Tool object or None if not found
    """
    for tool in _registered_tools:
        if tool.name == tool_name:
            return tool
    return None


def reload_skill(skill_name: str, skills_dir: Optional[Union[str, Path]] = None) -> bool:
    """
    Reload a specific skill.

    Args:
        skill_name: Name or slug of the skill to reload
        skills_dir: Optional custom skills directory

    Returns:
        True if skill was reloaded successfully
    """
    skills_path = _resolve_skills_path(skills_dir)
    skill_dir = skills_path / skill_name

    if not skill_dir.exists():
        logger.error(f"[SkillManager] Skill directory not found: {skill_name}")
        return False

    # Remove existing tools from this skill
    tool_prefix = skill_name.lower().replace("-", "_").replace(" ", "_")
    tools_to_remove = [t.name for t in _registered_tools if t.name.startswith(tool_prefix)]
    for tool_name in tools_to_remove:
        remove_skill(tool_name)

    # Reload the skill
    return _load_skill_from_directory(skill_dir)


# ── Private Helper Functions ─────────────────────────────────────────

def _resolve_skills_path(skills_dir: Optional[Union[str, Path]]) -> Path:
    """
    Resolve the skills directory path.

    Args:
        skills_dir: Optional custom path

    Returns:
        Absolute path to skills directory
    """
    if skills_dir is None:
        # Skills directory is at project root (parent of microharness package)
        # Structure: project_root / microharness / skills / skill_manager.py
        return Path(__file__).parent.parent.parent / "skills"
    return Path(skills_dir).expanduser().resolve()


def _find_skill_directories(skills_path: Path) -> List[Path]:
    """
    Find all valid skill directories.

    Args:
        skills_path: Path to skills directory

    Returns:
        List of skill directory paths
    """
    if not skills_path.exists():
        return []

    return [
        d for d in skills_path.iterdir()
        if d.is_dir()
        and not d.name.startswith('_')
        and d.name != '__pycache__'
        and not d.name.startswith('.')
    ]


def _load_skill_from_directory(skill_dir: Path) -> bool:
    """
    Load a single skill from its directory.

    Args:
        skill_dir: Path to skill directory

    Returns:
        True if skill was loaded successfully, False otherwise
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        logger.warning(f"[SkillManager] SKILL.md not found in {skill_dir.name}, skipping")
        return False

    try:
        skill_data = parse_skill_md(skill_md)
        _register_skill(skill_data, skill_dir)
        return True
    except FileNotFoundError as e:
        logger.error(f"[SkillManager] File error loading '{skill_dir.name}': {e}")
    except ValueError as e:
        logger.error(f"[SkillManager] Format error loading '{skill_dir.name}': {e}")
    except Exception as e:
        logger.error(f"[SkillManager] Unexpected error loading '{skill_dir.name}': {e}", exc_info=True)

    return False


def _get_safety_level(skill_data) -> str:
    """
    Extract safety level from skill metadata.

    Args:
        skill_data: Parsed skill data (SkillData or dict)

    Returns:
        Safety level string
    """
    metadata = skill_data.metadata if hasattr(skill_data, 'metadata') else skill_data.get("metadata", {})
    if not isinstance(metadata, dict):
        return SAFETY_KEYWORD_CHECK

    clawdbot = metadata.get("clawdbot", {})
    if not isinstance(clawdbot, dict):
        return SAFETY_KEYWORD_CHECK

    safety = clawdbot.get("safety", "")
    return safety if safety in VALID_SAFETY_LEVELS else SAFETY_KEYWORD_CHECK


def _make_tool_name(skill_name: str, block_index: int = 0) -> str:
    """
    Convert skill name to valid tool name.

    Args:
        skill_name: Original skill name
        block_index: Index of bash block (0 for first)

    Returns:
        Sanitized tool name safe for LangChain
    """
    # Convert to lowercase and replace separators
    base = skill_name.lower().replace("-", "_").replace(" ", "_")

    # Remove any invalid characters (keep only alphanumeric and underscore)
    base = re.sub(r'[^\w_]', '', base)

    # Ensure name is not empty
    if not base:
        base = "skill"

    if block_index == 0:
        return base
    return f"{base}_{block_index + 1}"


def _register_skill(skill_data, skill_dir: Path) -> None:
    """
    Register a skill by creating tools from its bash blocks.

    Args:
        skill_data: Parsed skill data (SkillData or dict)
        skill_dir: Path to skill directory
    """
    global _registered_tools, _skill_safety_levels

    bash_blocks = skill_data.bash_blocks if hasattr(skill_data, 'bash_blocks') else skill_data.get("bash_blocks", [])
    if not bash_blocks:
        logger.warning(f"[SkillManager] No bash blocks in skill '{skill_data.name}', skipping")
        return

    safety = _get_safety_level(skill_data)
    skill_name = skill_data.name
    blocks_count = len(bash_blocks)

    for i, bash_block in enumerate(bash_blocks):
        tool = _create_tool(skill_data, bash_block, i, skill_dir)

        # Skip if tool already registered
        if any(t.name == tool.name for t in _registered_tools):
            logger.info(f"[SkillManager] Skipped duplicate tool: {tool.name}")
            continue

        _registered_tools.append(tool)
        _skill_safety_levels[tool.name] = safety
        logger.info(f"[SkillManager] Registered: {tool.name} (safety={safety}, block={i+1}/{blocks_count})")


def _build_param_schema(params: List[str]) -> Optional[Dict[str, Any]]:
    """
    Build JSON schema for tool parameters.

    Args:
        params: List of parameter names

    Returns:
        JSON schema dict or None if no parameters
    """
    if not params:
        return None

    return {
        "type": "object",
        "properties": {
            param: {
                "type": "string",
                "description": f"Parameter: {param}"
            } for param in params
        },
        "required": params,
        "additionalProperties": False,
    }


def _prepare_command(command: str, params: Dict[str, str], skill_dir: Path) -> str:
    """
    Prepare command by substituting parameters and fixing paths.

    Args:
        command: Command template with {param} placeholders
        params: Parameter values dictionary
        skill_dir: Skill directory for path substitution

    Returns:
        Command with substituted parameters

    Raises:
        ValueError: If required parameter is missing or empty
    """
    # Substitute {param} placeholders
    cmd = command
    for param, value in params.items():
        if not value:  # Handles None or empty string
            raise ValueError(f"Missing required parameter: {param}")
        cmd = cmd.replace(f"{{{param}}}", str(value))

    # Fix path placeholder to actual skill directory
    skill_dir_str = str(skill_dir).replace('\\', '/')
    cmd = re.sub(rf'{re.escape(PATH_PLACEHOLDER)}[^/]+', skill_dir_str, cmd)

    # Validate command length
    if len(cmd) > 10000:
        raise ValueError(f"Command too long: {len(cmd)} characters")

    return cmd


def _execute_command(cmd: str, cwd: Path) -> str:
    """
    Execute a command via subprocess and return output.

    Args:
        cmd: Command to execute
        cwd: Working directory

    Returns:
        Command output or error message
    """
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd),
            timeout=SKILL_COMMAND_TIMEOUT,
            check=False
        )

        output = (result.stdout or "").strip()

        if result.returncode != 0:
            error_msg = result.stderr.strip() or output or "Unknown error"
            return f"Command failed (exit {result.returncode}):\n{error_msg}"

        return output if output else "✓ Command executed successfully (no output)"

    except FileNotFoundError:
        return "Error: 'bash' not found. Please ensure bash is installed."
    except subprocess.TimeoutExpired:
        return f"Timeout: execution exceeded {SKILL_COMMAND_TIMEOUT} seconds."
    except Exception as e:
        logger.error(f"[SkillManager] Unexpected error executing command: {e}", exc_info=True)
        return f"Unexpected error: {e}"


def _normalize_input_dict(input_dict: Any) -> Dict[str, str]:
    """
    Normalize various input types to a dictionary.

    Args:
        input_dict: Input from LangChain (dict, model, or string)

    Returns:
        Dictionary of string parameters
    """
    # Handle Pydantic models (LangChain)
    if hasattr(input_dict, 'model_dump'):
        return input_dict.model_dump()
    # Handle single string input
    elif isinstance(input_dict, str):
        return {"input": input_dict}
    # Handle dictionary input
    elif isinstance(input_dict, dict):
        # Convert all values to strings
        return {k: str(v) for k, v in input_dict.items() if v is not None}
    # Fallback for other types
    else:
        return {"input": str(input_dict)}


def _create_tool_executor(command: str, skill_dir: Path) -> Callable[[Any], str]:
    """
    Create an executor function for a skill command.

    Args:
        command: Command template with placeholders
        skill_dir: Skill directory

    Returns:
        Executor function that takes input and returns output
    """
    def executor(input_dict: Any) -> str:
        # Normalize input to dictionary
        params = _normalize_input_dict(input_dict)

        try:
            cmd = _prepare_command(command, params, skill_dir)
            return _execute_command(cmd, skill_dir)
        except ValueError as e:
            return f"Parameter error: {e}"

    return executor


def _get_attr(obj, key: str, default=None):
    """Get attribute or dict key, with optional default."""
    if hasattr(obj, key):
        return getattr(obj, key)
    return obj.get(key, default) if isinstance(obj, dict) else default


def _create_tool(
    skill_data,
    bash_block,
    block_index: int,
    skill_dir: Path
) -> Tool:
    """
    Create a LangChain Tool from a bash block.

    Args:
        skill_data: Parsed skill data (SkillData or dict)
        bash_block: Bash block (BashBlock or dict)
        block_index: Index of this block in the skill
        skill_dir: Path to skill directory

    Returns:
        LangChain Tool object
    """
    tool_name = _make_tool_name(_get_attr(skill_data, "name", "unnamed"), block_index)
    command = _get_attr(bash_block, "command", "")
    params = _get_attr(bash_block, "params", [])
    description = _get_attr(skill_data, "description", "")

    # Enhance description with parameter info
    if params:
        param_desc = f"\n\nParameters: {', '.join(params)}"
        description = (description or "Execute a skill command") + param_desc
    else:
        description = description or "Execute a skill command (no parameters)"

    # Truncate description if too long (LangChain limit)
    if len(description) > 1024:
        description = description[:1021] + "..."

    executor = _create_tool_executor(command, skill_dir)
    param_schema = _build_param_schema(params)

    return Tool(
        name=tool_name,
        description=description,
        args_schema=param_schema,
        func=executor,
    )


# ── Module Initialization ─────────────────────────────────────────────

def _validate_imports() -> None:
    """Validate that all required imports are available."""
    try:
        from langchain_core.tools import Tool
    except ImportError as e:
        logger.error(f"[SkillManager] LangChain not available: {e}")
        raise


# Optional: Validate on import
_validate_imports()