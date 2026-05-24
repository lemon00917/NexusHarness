"""
Safety Guard Module
===================
Harness 的安全守卫层 —— 这是 Harness 和普通 Agent 最关键的区别。

设计原则：
- AUTO_APPROVE_TOOLS（只读，无副作用）：自动放行
- ALWAYS_CONFIRM_TOOLS（写/删除操作）：无论内容，强制人工确认
- 其他工具：检测危险关键词，触发时需人工确认
- 人工拒绝后：Harness 中止当前操作，不继续执行

工具分级说明：
  AUTO_APPROVE  : list_files, read_file, get_file_info   ← 纯读，零副作用
  ALWAYS_CONFIRM: write_file, delete_file                ← 有持久化副作用
  KEYWORD_CHECK : run_python 及其他未分类工具             ← 内容决定风险
"""

# 高危操作关键词 —— 出现在任意工具参数中时触发守卫拦截
DANGEROUS_KEYWORDS = [
    "rm -rf", "rm -r", "rm -f",  # 递归删除
    "shutil.rmtree",
    "os.remove",
    "os.unlink",
    "DROP TABLE",
    "DELETE FROM",
    "format(",          # 磁盘格式化
    "subprocess.call",  # 原始 shell 调用
    "> /dev/",           # 写入系统设备
    "&& ",              # 命令链
    "| ",              # 管道
    "; ",              # 命令分隔
    "curl ",           # 可能的网络调用
    "wget ",
    "python -c",       # 直接执行代码
    "eval(",
    "exec(",
    "__import__",
    "base64 -d",
    "/etc/passwd",
    "/etc/shadow",
]

# 强制人工确认工具 —— 有持久化副作用，无论参数内容
ALWAYS_CONFIRM_TOOLS = {
    "write_file",    # 写入文件
    "delete_file",   # 删除文件
}

# 自动放行工具 —— 纯读，无任何副作用
AUTO_APPROVE_TOOLS = {
    "list_files",    # 列出文件
    "read_file",     # 读取文件内容
    "get_file_info", # 获取文件元信息
}

# Skill tool safety registry (populated at runtime via register_skill_safety_levels)
SKILL_SAFETY_LEVELS: dict[str, str] = {}


def register_skill_safety_levels(safety_map: dict[str, str]) -> None:
    """Register skill tool safety levels from skill_manager."""
    global SKILL_SAFETY_LEVELS
    SKILL_SAFETY_LEVELS.update(safety_map)


def is_dangerous(tool_input: dict) -> bool:
    """
    检查工具调用参数是否包含高危关键词。

    注意：此函数只检查参数内容，不依赖 tool_name。
    tool_name 级别的判断由 should_confirm() 的分级逻辑处理。

    Args:
        tool_input: 工具调用参数字典

    Returns:
        True 表示参数中含有高危内容
    """
    content = str(tool_input).lower()
    return any(kw.lower() in content for kw in DANGEROUS_KEYWORDS)


def should_confirm(tool_name: str, tool_input: dict) -> bool:
    """
    决定是否需要人工确认。

    优先级（从高到低）：
    1. AUTO_APPROVE_TOOLS → 直接放行，不检查内容
    2. ALWAYS_CONFIRM_TOOLS → 直接拦截，不检查内容
    3. 其他工具 → 检查参数是否含危险关键词

    Args:
        tool_name: 工具名称
        tool_input: 工具调用参数字典

    Returns:
        True = 需要人工确认
        False = 自动放行
    """
    if tool_name in AUTO_APPROVE_TOOLS:
        return False
    if tool_name in ALWAYS_CONFIRM_TOOLS:
        return True
    # Check skill-specific safety level
    if tool_name in SKILL_SAFETY_LEVELS:
        safety = SKILL_SAFETY_LEVELS[tool_name]
        if safety == "AUTO_APPROVE":
            return False
        if safety == "ALWAYS_CONFIRM":
            return True
        # KEYWORD_CHECK falls through to default
    return is_dangerous(tool_input)


def request_human_approval(tool_name: str, tool_input: dict) -> bool:
    """
    暂停执行，向操作员展示待执行操作，等待明确批准。

    Args:
        tool_name: 工具名称
        tool_input: 工具调用参数

    Returns:
        True = 批准继续执行
        False = 拒绝，Harness 将中止此操作
    """
    # 根据工具类型和内容决定风险标签
    if tool_name in ALWAYS_CONFIRM_TOOLS and tool_name == "delete_file":
        flag = "🗑️  DELETE OP"
    elif is_dangerous(tool_input):
        flag = "⚠️  HIGH RISK"
    else:
        flag = "📝 WRITE OP"

    print(f"\n{'='*55}")
    print(f"  [HARNESS GUARD] {flag}")
    print(f"  Tool   : {tool_name}")

    for k, v in tool_input.items():
        display_val = str(v)
        if len(display_val) > 200:
            display_val = display_val[:200] + "... (truncated)"
        print(f"  {k:8}: {display_val}")

    print(f"{'='*55}")

    while True:
        answer = input("  Approve? (yes / no): ").strip().lower()
        if answer in ("yes", "y"):
            print("  ✅ Approved.\n")
            return True
        elif answer in ("no", "n"):
            print("  ❌ Rejected. Operation cancelled.\n")
            return False
        else:
            print("  Please type 'yes' or 'no'.")