"""
Skill Manager Module
====================
Auto-discovers, loads, and registers skill tools from skills/ directory.

Compatible with OpenClaw SKILL.md format:
  - Parses SKILL.md frontmatter (name, description, metadata)
  - Extracts ```bash code blocks as executable commands
  - Generates @tool functions with parameter substitution
  - Reads safety level from metadata.clawdbot.safety

Usage:
    from skill_manager import load_skills, get_skills, get_skill_safety_map
    load_skills()
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import Dict, List

from langchain_core.tools import Tool

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

logger = logging.getLogger(__name__)

# Safety level constants
SAFETY_AUTO_APPROVE = "AUTO_APPROVE"
SAFETY_ALWAYS_CONFIRM = "ALWAYS_CONFIRM"
SAFETY_KEYWORD_CHECK = "KEYWORD_CHECK"

# Global registry
_registered_tools: List = []
_skill_safety_levels: Dict[str, str] = {}
_loaded = False


def get_skills() -> List:
    """Return all registered skill tools."""
    return _registered_tools


def get_skill_safety_map() -> Dict[str, str]:
    """Return the complete safety level map for all skill tools."""
    return dict(_skill_safety_levels)


def load_skills(skills_dir: str = None) -> None:
    """
    Discover and load all skills from the skills directory.

    Args:
        skills_dir: Path to skills directory. Defaults to <project>/skills/
    """
    global _registered_tools, _skill_safety_levels, _loaded

    if _loaded:
        return

    if skills_dir is None:
        # Skills directory is at project root (parent of microharness package)
        skills_dir = Path(__file__).parent.parent / "skills"
    else:
        skills_dir = Path(skills_dir)

    if not skills_dir.exists():
        logger.warning(f"[SkillManager] Skills directory not found: {skills_dir}")
        return

    # Discover all skill directories (exclude __pycache__ and hidden dirs)
    skill_dirs = [d for d in skills_dir.iterdir() if d.is_dir() and not d.name.startswith('_')]
    logger.info(f"[SkillManager] Found {len(skill_dirs)} skill directory(ies)")

    for skill_dir in skill_dirs:
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            logger.warning(f"[SkillManager] SKILL.md not found in {skill_dir.name}, skipping")
            continue

        try:
            skill_data = _parse_skill_md(skill_md)
            _load_skill(skill_data, skill_dir)
        except Exception as e:
            logger.error(f"[SkillManager] Failed to load skill '{skill_dir.name}': {e}")


def _parse_skill_md(path: Path) -> dict:
    """Parse SKILL.md: extract frontmatter and bash code blocks."""
    content = path.read_text(encoding="utf-8")

    # Split frontmatter from markdown body
    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"Invalid SKILL.md format in {path}")

    frontmatter = parts[1].strip()
    body = parts[2].strip()

    fm = {}
    if HAS_YAML:
        try:
            fm = yaml.safe_load(frontmatter) or {}
        except Exception:
            logger.warning(f"[SkillManager] Failed to parse YAML frontmatter in {path.name}")

    # Extract all bash code blocks
    bash_blocks = []
    for match in re.finditer(r'```bash\s+(.*?)\s```', body, re.DOTALL):
        raw_cmd = match.group(1).strip()
        # Remove comment lines (# Output: ...)
        cmd_lines = [l for l in raw_cmd.splitlines() if not l.strip().startswith("# Output:")]
        cmd = "\n".join(cmd_lines)
        # Extract {param} placeholders
        params = list(set(re.findall(r'\{(\w+)\}', cmd)))
        if cmd.strip():
            bash_blocks.append({"command": cmd, "params": params})

    return {
        "name": fm.get("name", path.parent.name),
        "description": fm.get("description", ""),
        "metadata": fm.get("metadata", {}),
        "bash_blocks": bash_blocks,
        "slug": path.parent.name,
    }


def _get_safety_level(skill_data: dict) -> str:
    """Extract safety level from skill metadata."""
    meta = skill_data.get("metadata", {})
    clawdbot = meta.get("clawdbot", {}) if isinstance(meta, dict) else {}
    explicit = clawdbot.get("safety", "") if isinstance(clawdbot, dict) else ""
    if explicit in (SAFETY_AUTO_APPROVE, SAFETY_ALWAYS_CONFIRM, SAFETY_KEYWORD_CHECK):
        return explicit
    return SAFETY_KEYWORD_CHECK


def _make_tool_name(skill_name: str, block_index: int = 0) -> str:
    """Convert skill name to valid tool name."""
    base = skill_name.lower().replace("-", "_").replace(" ", "_")
    if block_index == 0:
        return base
    return f"{base}_{block_index + 1}"


def _generate_tool(skill_data: dict, bash_block: dict, block_index: int, skill_dir: Path):
    """Generate a Tool from a bash code block."""
    tool_name = _make_tool_name(skill_data["name"], block_index)
    command = bash_block["command"]
    params = bash_block["params"]
    description = skill_data["description"]

    def _execute(input_dict: dict) -> str:
        # Normalize to dict (LangChain may pass pydantic model or raw dict)
        if hasattr(input_dict, 'model_dump'):
            input_dict = input_dict.model_dump()
        elif not isinstance(input_dict, dict):
            input_dict = {"input": str(input_dict)}

        # Substitute {param} placeholders
        cmd = command
        for param, value in input_dict.items():
            if value is None or value == "":
                return f"Missing required parameter: {param}"
            cmd = cmd.replace(f"{{{param}}}", str(value))

        # Auto-fix /path/to/skills placeholder to actual skill dir
        cmd = cmd.replace("/path/to/skills/", str(skill_dir))

        # Execute via bash
        try:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/tmp",
            )
            stdout, stderr = proc.communicate(timeout=30)
            output = stdout.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()
                return f"Command failed (exit {proc.returncode}):\n{err or output}"
            return output if output else "(no output)"
        except FileNotFoundError:
            return "Error: 'bash' not found. Is bash installed?"
        except subprocess.TimeoutExpired:
            return "Timeout: execution exceeded 30 seconds."
        except Exception as e:
            return f"Unexpected error: {e}"

    # Build param schema
    if params:
        param_schema = {
            "type": "object",
            "properties": {p: {"type": "string", "description": f"Parameter {p}"} for p in params},
            "required": params,
        }
    else:
        param_schema = None

    tool = Tool(
        name=tool_name,
        description=description,
        args_schema=param_schema,
        func=_execute,
    )
    return tool


def _load_skill(skill_data: dict, skill_dir: Path) -> None:
    """Load a single skill: generate tools from bash blocks."""
    global _registered_tools, _skill_safety_levels

    if not skill_data["bash_blocks"]:
        logger.warning(f"[SkillManager] No bash blocks in skill '{skill_data['name']}', skipping")
        return

    safety = _get_safety_level(skill_data)

    for i, bash_block in enumerate(skill_data["bash_blocks"]):
        tool = _generate_tool(skill_data, bash_block, i, skill_dir)
        # Skip if tool already registered
        if any(t.name == tool.name for t in _registered_tools):
            logger.info(f"[SkillManager] Skipped duplicate: {tool.name}")
            continue
        _registered_tools.append(tool)
        _skill_safety_levels[tool.name] = safety
        logger.info(f"[SkillManager] Registered: {tool.name} (safety={safety})")


def clear_skills() -> None:
    """Clear all registered skills. Useful for testing."""
    global _registered_tools, _skill_safety_levels, _loaded
    _registered_tools = []
    _skill_safety_levels = {}
    _loaded = False


def remove_skill(name: str) -> bool:
    """Remove a specific skill tool by name. Returns True if found and removed."""
    global _registered_tools, _skill_safety_levels
    original_len = len(_registered_tools)
    _registered_tools = [t for t in _registered_tools if t.name != name]
    _skill_safety_levels.pop(name, None)
    return len(_registered_tools) < original_len