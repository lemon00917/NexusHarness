"""
NexusHarness Logging Configuration
===================================
统一日志系统，支持多级别、多输出、格式化。

日志级别：DEBUG < INFO < WARNING < ERROR < CRITICAL
输出目标：控制台 + 文件日志

文件轮转：
- 单文件最大 10MB
- 保留最近 5 个备份
- 自动清理过期日志
"""

import os
import sys
import logging
import logging.handlers
from pathlib import Path
from datetime import datetime
from typing import Optional


# ──────────────────────── 常量 ────────────────────────

LOG_DIR = Path(__file__).parent.parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 日志级别
DEBUG = logging.DEBUG
INFO = logging.INFO
WARNING = logging.WARNING
ERROR = logging.ERROR
CRITICAL = logging.CRITICAL

# 日志文件名
WEB_LOG = "web.log"
APP_LOG = "app.log"
RAG_LOG = "rag.log"
OLLAMA_LOG = "ollama.log"
SESSION_LOG = "session.log"
FILTER_LOG = "filter.log"
AUDIT_LOG = "audit.log"  # 保留原有的audit.log

# 日志格式
CONSOLE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
FILE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 日志文件大小和备份数
MAX_BYTES = 10 * 1024 * 1024  # 10MB
BACKUP_COUNT = 5


# ──────────────────────── 日志创建函数 ────────────────────────

def _create_file_handler(filename: str, level: int = INFO) -> logging.handlers.RotatingFileHandler:
    """
    创建带轮转的文件处理器

    Args:
        filename: 日志文件名
        level: 日志级别

    Returns:
        配置好的RotatingFileHandler
    """
    filepath = LOG_DIR / filename
    handler = logging.handlers.RotatingFileHandler(
        str(filepath),
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8"
    )
    handler.setLevel(level)
    formatter = logging.Formatter(FILE_FORMAT, datefmt=DATE_FORMAT)
    handler.setFormatter(formatter)
    return handler


def _create_console_handler(level: int = INFO) -> logging.StreamHandler:
    """
    创建控制台处理器

    Args:
        level: 日志级别

    Returns:
        配置好的StreamHandler
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    formatter = logging.Formatter(CONSOLE_FORMAT, datefmt=DATE_FORMAT)
    handler.setFormatter(formatter)
    return handler


# ──────────────────────── 日志模块缓存 ────────────────────────

_loggers = {}


def get_logger(
    name: str,
    level: int = INFO,
    console: bool = True,
    file: Optional[str] = None
) -> logging.Logger:
    """
    获取或创建命名的logger

    Args:
        name: logger名称 (如 "web", "rag", "ollama")
        level: 日志级别
        console: 是否输出到控制台
        file: 是否输出到文件，如果为str则是文件名

    Returns:
        配置好的Logger
    """
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False  # 不向上传播，避免重复

    # 清除已有的handler
    logger.handlers.clear()

    # 添加控制台handler
    if console:
        logger.addHandler(_create_console_handler(level))

    # 添加文件handler
    if file:
        logger.addHandler(_create_file_handler(file, level))

    _loggers[name] = logger
    return logger


# ──────────────────────── 预配置日志模块 ────────────────────────

# Web请求日志
web_logger = get_logger("web", INFO, file="web.log")

# 应用日志（通用）
app_logger = get_logger("app", INFO, file="app.log")

# RAG检索日志
rag_logger = get_logger("rag", INFO, file="rag.log")

# Ollama调用日志
ollama_logger = get_logger("ollama", INFO, file="ollama.log")

# Session会话日志
session_logger = get_logger("session", INFO, file="session.log")

# Filter筛选日志
filter_logger = get_logger("filter", INFO, file="filter.log")

# 审计日志（保留原有功能）
audit_logger = get_logger("audit", INFO, file="audit.log")

# Agent日志
agent_logger = get_logger("agent", INFO, file="agent.log")

# Tool日志
tool_logger = get_logger("tool", INFO, file="tool.log")


# ──────────────────────── 便捷函数 ────────────────────────

def log_request(method: str, path: str, status: int = None, duration_ms: float = None, **kwargs):
    """
    记录HTTP请求日志

    Args:
        method: HTTP方法 (GET/POST等)
        path: 请求路径
        status: 响应状态码
        duration_ms: 请求耗时(毫秒)
        **kwargs: 其他字段 (user_id, session_id等)
    """
    parts = [f"{method} {path}"]
    if status:
        parts.append(f"status={status}")
    if duration_ms is not None:
        parts.append(f"耗时={duration_ms:.2f}ms")
    for k, v in kwargs.items():
        parts.append(f"{k}={v}")
    web_logger.info(" | ".join(parts))


def log_rag_search(query: str, top_k: int, results_count: int, duration_ms: float = None, **kwargs):
    """
    记录RAG检索日志

    Args:
        query: 检索查询
        top_k: 请求数量
        results_count: 返回结果数量
        duration_ms: 检索耗时
        **kwargs: 其他字段
    """
    parts = [
        f"查询: {query[:50]}..." if len(query) > 50 else f"查询: {query}",
        f"top_k={top_k}",
        f"结果={results_count}"
    ]
    if duration_ms is not None:
        parts.append(f"耗时={duration_ms:.2f}ms")
    for k, v in kwargs.items():
        parts.append(f"{k}={v}")
    rag_logger.info(" | ".join(parts))


def log_ollama_call(model: str, messages_count: int, response_length: int = None, duration_ms: float = None, error: str = None, **kwargs):
    """
    记录Ollama调用日志

    Args:
        model: 模型名称
        messages_count: 输入消息数量
        response_length: 输出长度
        duration_ms: 调用耗时
        error: 错误信息
        **kwargs: 其他字段
    """
    parts = [f"模型: {model}", f"消息数={messages_count}"]
    if response_length is not None:
        parts.append(f"输出长度={response_length}")
    if duration_ms is not None:
        parts.append(f"耗时={duration_ms:.2f}ms")
    if error:
        parts.append(f"错误: {error}")
    for k, v in kwargs.items():
        parts.append(f"{k}={v}")

    if error:
        ollama_logger.error(" | ".join(parts))
    else:
        ollama_logger.info(" | ".join(parts))


def log_session_event(session_id: str, event: str, **kwargs):
    """
    记录Session事件日志

    Args:
        session_id: 会话ID
        event: 事件类型 (created/updated/interrupted/completed)
        **kwargs: 其他字段
    """
    parts = [f"会话: {session_id}", f"事件: {event}"]
    for k, v in kwargs.items():
        parts.append(f"{k}={v}")
    session_logger.info(" | ".join(parts))


def log_filter(condition: str, model: str, candidates: int, matched: int, duration_ms: float = None, **kwargs):
    """
    记录Filter筛选日志

    Args:
        condition: 筛选条件
        model: 使用的模型
        candidates: 候选数量
        matched: 匹配数量
        duration_ms: 筛选耗时
        **kwargs: 其他字段
    """
    parts = [
        f"条件: {condition[:50]}..." if len(condition) > 50 else f"条件: {condition}",
        f"模型: {model}",
        f"候选={candidates}",
        f"匹配={matched}"
    ]
    if duration_ms is not None:
        parts.append(f"耗时={duration_ms:.2f}ms")
    for k, v in kwargs.items():
        parts.append(f"{k}={v}")
    filter_logger.info(" | ".join(parts))


def log_agent_step(session_id: str, step: int, action: str, **kwargs):
    """
    记录Agent步骤日志

    Args:
        session_id: 会话ID
        step: 步骤号
        action: 操作类型 (thinking/tool_call/complete等)
        **kwargs: 其他字段
    """
    parts = [f"会话: {session_id}", f"步骤: {step}", f"动作: {action}"]
    for k, v in kwargs.items():
        parts.append(f"{k}={v}")
    agent_logger.info(" | ".join(parts))


def log_tool_call(session_id: str, step: int, tool_name: str, approved: bool = True, duration_ms: float = None, **kwargs):
    """
    记录Tool调用日志

    Args:
        session_id: 会话ID
        step: 步骤号
        tool_name: 工具名称
        approved: 是否批准
        duration_ms: 调用耗时
        **kwargs: 其他字段
    """
    parts = [
        f"会话: {session_id}",
        f"步骤: {step}",
        f"工具: {tool_name}",
        f"批准: {'是' if approved else '否'}"
    ]
    if duration_ms is not None:
        parts.append(f"耗时={duration_ms:.2f}ms")
    for k, v in kwargs.items():
        parts.append(f"{k}={v}")

    if not approved:
        tool_logger.warning(" | ".join(parts))
    else:
        tool_logger.info(" | ".join(parts))


# ──────────────────────── 初始化函数 ────────────────────────

def setup_logging():
    """
    初始化全局日志配置
    在应用启动时调用
    """
    # 设置根logger级别
    root_logger = logging.getLogger()
    root_logger.setLevel(INFO)

    # 确保logs目录存在
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # 添加一个全局文件handler记录所有日志
    # 如果还没有全局handler的话
    if not root_logger.handlers:
        root_handler = _create_file_handler("app.log", INFO)
        root_logger.addHandler(root_handler)

    app_logger.info(f"=" * 50)
    app_logger.info(f"NexusHarness 日志系统已初始化")
    app_logger.info(f"日志目录: {LOG_DIR}")
    app_logger.info(f"=" * 50)


# ──────────────────────── 导出 ────────────────────────

__all__ = [
    "get_logger",
    "setup_logging",
    "log_request",
    "log_rag_search",
    "log_ollama_call",
    "log_session_event",
    "log_filter",
    "log_agent_step",
    "log_tool_call",
    # Logger实例
    "web_logger",
    "app_logger",
    "rag_logger",
    "ollama_logger",
    "session_logger",
    "filter_logger",
    "audit_logger",
    "agent_logger",
    "tool_logger",
    # 级别常量
    "DEBUG",
    "INFO",
    "WARNING",
    "ERROR",
    "CRITICAL",
]


if __name__ == "__main__":
    # 测试日志
    setup_logging()
    app_logger.info("测试 info 日志")
    app_logger.warning("测试 warning 日志")
    app_logger.error("测试 error 日志")

    log_request("GET", "/api/rag/search", 200, 15.5)
    log_rag_search("糖尿病患者", 20, 5, 123.4)
    log_ollama_call("qwen2:7b", 3, 150, 500.0)
    log_session_event("session_123", "created")
    log_filter("血糖>7", "qwen2:7b", 20, 5, 2000.0)
    log_agent_step("session_123", 1, "thinking")
    log_tool_call("session_123", 1, "read_file", True, 50.0)

    print(f"\n日志已写入: {LOG_DIR}")