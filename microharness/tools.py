"""
Tool Orchestration Module
=========================
Harness 的工具调度层 —— 预置 Agent 可用的工具，统一管理调用逻辑。

工具安全分级（与 guard.py 对应）：
  AUTO_APPROVE  : list_files, read_file, get_file_info  ← 纯读
  ALWAYS_CONFIRM: write_file, delete_file               ← 写/删
  KEYWORD_CHECK : run_python                            ← 内容决定风险
"""

import os
import sys
import subprocess
from pathlib import Path
from langchain_core.tools import tool

# Windows compatible sandbox path
if sys.platform == "win32":
    SANDBOX = os.path.join(os.environ.get("TEMP", "C:\\tmp"), "sandbox")
else:
    SANDBOX = "/tmp/sandbox"
os.makedirs(SANDBOX, exist_ok=True)


def _safe_path(filename: str) -> str:
    """防止路径穿越攻击，强制限定在沙箱目录内"""
    # Remove any path components, keep only filename
    basename = os.path.basename(filename)
    return os.path.join(SANDBOX, basename)


# ── AUTO_APPROVE 工具（纯读，无副作用）─────────────────

@tool
def list_files() -> str:
    """List all files currently in the sandbox directory."""
    files = os.listdir(SANDBOX)
    if not files:
        return "📂 Sandbox is empty."
    return "📂 Files in sandbox:\n" + "\n".join(f"  - {f}" for f in sorted(files))


@tool
def read_file(filename: str) -> str:
    """
    Read and return the content of a file in the sandbox.

    Args:
        filename: Name of the file to read
    """
    path = _safe_path(filename)
    if not os.path.exists(path):
        return f"❌ File not found: {filename}"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if len(content) > 3000:
        content = content[:3000] + "\n... (truncated)"
    return content


@tool
def get_file_info(filename: str) -> str:
    """
    Return metadata about a file in the sandbox (size, modified time).

    Args:
        filename: Name of the file to inspect
    """
    path = _safe_path(filename)
    if not os.path.exists(path):
        return f"❌ File not found: {filename}"
    stat = os.stat(path)
    import datetime
    mtime = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"📄 {filename}\n"
        f"   Size    : {stat.st_size} bytes\n"
        f"   Modified: {mtime}"
    )


# ── ALWAYS_CONFIRM 工具（有持久化副作用）───────────────

@tool
def write_file(filename: str, content: str) -> str:
    """
    Write content to a file inside the sandbox directory.

    Args:
        filename: Name of the file (e.g. 'main.py')
        content: Full content to write
    """
    path = _safe_path(filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"✅ File written: {path} ({len(content)} chars)"


@tool
def delete_file(filename: str) -> str:
    """
    Delete a file from the sandbox directory.

    Args:
        filename: Name of the file to delete
    """
    path = _safe_path(filename)
    if not os.path.exists(path):
        return f"❌ File not found: {filename}"
    os.remove(path)
    return f"🗑️  File deleted: {filename}"


# ── KEYWORD_CHECK 工具（内容决定风险）──────────────────

@tool
def run_python(filename: str) -> str:
    """
    Execute a Python file inside the sandbox directory.

    Args:
        filename: Name of the Python file to run
    """
    path = _safe_path(filename)
    if not os.path.exists(path):
        return f"❌ Error: {path} does not exist. Did you write the file first?"

    # Use 'python' on Windows, 'python3' on other platforms
    python_cmd = "python" if sys.platform == "win32" else "python3"

    try:
        result = subprocess.run(
            [python_cmd, path],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=SANDBOX,
            encoding="utf-8",
            errors="replace",
        )
        output = result.stdout or result.stderr or "(no output)"
    except subprocess.TimeoutExpired:
        output = "❌ Timeout: execution exceeded 30 seconds and was terminated."
    except FileNotFoundError:
        return f"❌ Error: '{python_cmd}' not found. Is Python installed and in PATH?"

    if len(output) > 2000:
        output = output[:2000] + "\n... (output truncated)"
    return output


# ── 注册表 ──────────────────────────────────────────────
TOOLS = [list_files, read_file, get_file_info, write_file, delete_file, run_python]

# Safety levels for built-in tools
BUILTIN_SAFETY = {
    "list_files": "AUTO_APPROVE",
    "read_file": "AUTO_APPROVE",
    "get_file_info": "AUTO_APPROVE",
    "write_file": "ALWAYS_CONFIRM",
    "delete_file": "ALWAYS_CONFIRM",
    "run_python": "KEYWORD_CHECK",
}

# ── 加载 Skills ──────────────────────────────────────────
try:
    from .skill_manager import load_skills, get_skills
    load_skills()
    skill_tools = get_skills()
    if skill_tools:
        TOOLS = TOOLS + skill_tools
        print(f"[ToolNode] Loaded {len(skill_tools)} skill tool(s): {[t.name for t in skill_tools]}")
except Exception as e:
    print(f"[ToolNode] Skill loading skipped: {e}")

# ── 导出到 ToolRegistry ──────────────────────────────────
from .tool_registry import get_registry, register_tool, unregister_tool, list_tools, get_tool, enable_tool, disable_tool, is_tool_enabled

# Initialize registry with built-in tools
registry = get_registry()
for t in TOOLS:
    safety = BUILTIN_SAFETY.get(t.name, "KEYWORD_CHECK")
    registry.register(t, safety)
