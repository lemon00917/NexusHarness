"""
Tool Registry Module
====================
Harness 的工具注册表 —— 动态管理所有可用工具。

功能：
- 注册/注销工具
- 查询工具列表和 Schema
- 内置工具 + Skill 工具统一管理
- 运行时动态启用/禁用工具
"""

from typing import Dict, List, Optional, Callable
from langchain_core.tools import Tool
import logging

logger = logging.getLogger(__name__)


class ToolRegistry:
    """
    工具注册表 - 单例模式

    用法：
        registry = ToolRegistry()
        registry.register(my_tool)
        registry.unregister("my_tool")
        tools = registry.list()
        tool = registry.get("my_tool")
        schema = registry.get_schema("my_tool")
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._tools: Dict[str, Tool] = {}
        self._schemas: Dict[str, dict] = {}
        self._disabled: set = set()
        self._safety_levels: Dict[str, str] = {}
        self._initialized = True
        logger.info("[ToolRegistry] Initialized")

    def register(self, tool: Tool, safety: str = "KEYWORD_CHECK") -> None:
        """注册一个工具"""
        from langchain_core.tools import StructuredTool
        if not isinstance(tool, (Tool, StructuredTool)):
            raise ValueError(f"Expected Tool, got {type(tool)}")

        self._tools[tool.name] = tool
        self._schemas[tool.name] = self._build_schema(tool)
        self._safety_levels[tool.name] = safety
        logger.info(f"[ToolRegistry] Registered: {tool.name} (safety={safety})")

    def unregister(self, name: str) -> bool:
        """注销一个工具"""
        if name in self._tools:
            del self._tools[name]
            if name in self._schemas:
                del self._schemas[name]
            if name in self._safety_levels:
                del self._safety_levels[name]
            self._disabled.discard(name)
            logger.info(f"[ToolRegistry] Unregistered: {name}")
            return True
        return False

    def get(self, name: str) -> Optional[Tool]:
        """获取工具"""
        return self._tools.get(name)

    def list(self, include_disabled: bool = False) -> List[Tool]:
        """列出所有工具"""
        if include_disabled:
            return list(self._tools.values())
        return [t for t in self._tools.values() if t.name not in self._disabled]

    def get_active(self) -> List[Tool]:
        """获取所有已启用的工具"""
        return self.list(include_disabled=False)

    def enable(self, name: str) -> bool:
        """启用工具"""
        if name in self._disabled:
            self._disabled.discard(name)
            logger.info(f"[ToolRegistry] Enabled: {name}")
            return True
        return False

    def disable(self, name: str) -> bool:
        """禁用工具"""
        if name in self._tools and name not in self._disabled:
            self._disabled.add(name)
            logger.info(f"[ToolRegistry] Disabled: {name}")
            return True
        return False

    def is_enabled(self, name: str) -> bool:
        """检查工具是否启用"""
        return name in self._tools and name not in self._disabled

    def get_schema(self, name: str) -> Optional[dict]:
        """获取工具的 JSON Schema"""
        return self._schemas.get(name)

    def list_schemas(self) -> Dict[str, dict]:
        """获取所有工具的 Schema"""
        return dict(self._schemas)

    def get_safety(self, name: str) -> Optional[str]:
        """获取工具的安全级别"""
        return self._safety_levels.get(name)

    def get_all_safety_levels(self) -> Dict[str, str]:
        """获取所有工具的安全级别"""
        return dict(self._safety_levels)

    def _build_schema(self, tool: Tool) -> dict:
        """从 Tool 构建 JSON Schema"""
        schema = {
            "name": tool.name,
            "description": tool.description or "",
        }

        if hasattr(tool, 'args_schema') and tool.args_schema:
            # Pydantic model or dict with schema
            if hasattr(tool.args_schema, 'model_json_schema'):
                schema["parameters"] = tool.args_schema.model_json_schema()
            elif isinstance(tool.args_schema, dict):
                schema["parameters"] = tool.args_schema
        else:
            # Simple schema with just input
            schema["parameters"] = {
                "type": "object",
                "properties": {
                    "input": {
                        "type": "string",
                        "description": "Input to the tool"
                    }
                }
            }

        return schema

    def clear(self) -> None:
        """清空所有工具"""
        self._tools.clear()
        self._schemas.clear()
        self._disabled.clear()
        self._safety_levels.clear()
        logger.info("[ToolRegistry] Cleared all tools")

    def summary(self) -> dict:
        """获取注册表摘要"""
        return {
            "total": len(self._tools),
            "enabled": len(self.list(include_disabled=False)),
            "disabled": len(self._disabled),
            "tools": [t.name for t in self.list(include_disabled=True)],
        }


# ── 全局注册表实例 ──────────────────────────────────────────
_registry: Optional[ToolRegistry] = None


def get_registry() -> ToolRegistry:
    """获取全局工具注册表实例"""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry


# ── 便捷函数 ──────────────────────────────────────────────

def register_tool(tool: Tool, safety: str = "KEYWORD_CHECK") -> None:
    """注册工具到全局注册表"""
    get_registry().register(tool, safety)


def unregister_tool(name: str) -> bool:
    """从全局注册表注销工具"""
    return get_registry().unregister(name)


def list_tools(include_disabled: bool = False) -> List[Tool]:
    """列出全局注册表的工具"""
    return get_registry().list(include_disabled)


def get_tool(name: str) -> Optional[Tool]:
    """获取全局注册表的工具"""
    return get_registry().get(name)


def enable_tool(name: str) -> bool:
    """启用全局注册表的工具"""
    return get_registry().enable(name)


def disable_tool(name: str) -> bool:
    """禁用全局注册表的工具"""
    return get_registry().disable(name)


def is_tool_enabled(name: str) -> bool:
    """检查工具是否启用"""
    return get_registry().is_enabled(name)