"""
Retry Mechanism
===============
Tool execution with automatic retry and exponential backoff.

Supports:
- Configurable max retries per tool
- Exponential backoff with jitter
- Different retry policies for different error types
- Retry logging for observability
"""

import asyncio
import random
import time
from typing import Any, Callable, Optional, TypeVar
from functools import wraps

from .tools import TOOLS, BUILTIN_SAFETY

T = TypeVar("T")


class RetryPolicy:
    """Retry policy configuration."""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        exponential_base: float = 2.0,
        jitter: bool = True,
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter

    def get_delay(self, attempt: int) -> float:
        """Calculate delay for a given attempt number."""
        delay = self.base_delay * (self.exponential_base ** attempt)
        delay = min(delay, self.max_delay)
        if self.jitter:
            delay = delay * (0.5 + random.random() * 0.5)
        return delay


# Retry policies by error category
RETRY_POLICIES = {
    "timeout": RetryPolicy(max_retries=3, base_delay=1.0, max_delay=15.0),
    "network": RetryPolicy(max_retries=3, base_delay=1.5, max_delay=30.0),
    "rate_limit": RetryPolicy(max_retries=5, base_delay=5.0, max_delay=60.0),
    "default": RetryPolicy(max_retries=2, base_delay=1.0, max_delay=10.0),
}


def classify_error(error: Exception) -> str:
    """Classify an error into a retry category."""
    error_msg = str(error).lower()
    error_type = type(error).__name__.lower()

    # Timeout errors
    if "timeout" in error_msg or "timeout" in error_type or "timed out" in error_msg:
        return "timeout"

    # Network errors
    if any(kw in error_msg for kw in ["connection", "network", "ECONNREFUSED", "ENETUNREACH", "dns", "socket"]):
        return "network"
    if any(kw in error_type for kw in ["connectionerror", "httperror", "requesterror"]):
        return "network"

    # Rate limit errors
    if any(kw in error_msg for kw in ["rate limit", "429", "too many requests", "quota"]):
        return "rate_limit"

    return "default"


def with_retry(func: Callable[..., T]) -> Callable[..., T]:
    """Decorator that adds retry logic to a function."""

    @wraps(func)
    def sync_wrapper(*args, **kwargs) -> T:
        last_error: Optional[Exception] = None

        for attempt in range(RETRY_POLICIES["default"].max_retries + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_error = e
                category = classify_error(e)
                policy = RETRY_POLICIES.get(category, RETRY_POLICIES["default"])

                if attempt >= policy.max_retries:
                    print(f"  [RETRY] {category} error: max retries ({policy.max_retries}) reached. Giving up.")
                    raise

                delay = policy.get_delay(attempt)
                print(f"  [RETRY] {category} error (attempt {attempt + 1}/{policy.max_retries + 1}): {e}")
                print(f"  [RETRY] Retrying in {delay:.1f}s...")
                time.sleep(delay)

        raise last_error

    @wraps(func)
    async def async_wrapper(*args, **kwargs) -> T:
        last_error: Optional[Exception] = None

        for attempt in range(RETRY_POLICIES["default"].max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_error = e
                category = classify_error(e)
                policy = RETRY_POLICIES.get(category, RETRY_POLICIES["default"])

                if attempt >= policy.max_retries:
                    print(f"  [RETRY] {category} error: max retries ({policy.max_retries}) reached. Giving up.")
                    raise

                delay = policy.get_delay(attempt)
                print(f"  [RETRY] {category} error (attempt {attempt + 1}/{policy.max_retries + 1}): {e}")
                print(f"  [RETRY] Retrying in {delay:.1f}s...")
                await asyncio.sleep(delay)

        raise last_error

    # Return appropriate wrapper based on function type
    if asyncio.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper


class RetryToolExecutor:
    """
    Tool executor with built-in retry logic.

    Wraps ToolNode-style tool execution with:
    - Per-tool retry configuration
    - Exponential backoff
    - Detailed retry logging
    """

    def __init__(self, tools, max_retries: int = 3, default_policy: str = "default"):
        self.tools = tools
        self.max_retries = max_retries
        self.default_policy = default_policy
        self._retry_counts = {}  # track retries per tool

    def _get_policy(self, tool_name: str) -> RetryPolicy:
        """Get retry policy for a specific tool."""
        # Tools with ALWAYS_CONFIRM or KEYWORD_CHECK should retry more carefully
        safety = BUILTIN_SAFETY.get(tool_name, "KEYWORD_CHECK")
        if safety == "ALWAYS_CONFIRM":
            # Write/delete operations - fewer retries, shorter delay
            return RetryPolicy(max_retries=2, base_delay=0.5, max_delay=5.0)
        return RETRY_POLICIES[self.default_policy]

    def _call_tool(self, tool_func: Callable, tool_args: dict) -> Any:
        """Call a single tool with retry logic."""
        @with_retry
        def bounded_call():
            return tool_func.invoke(tool_args)

        return bounded_call()

    def invoke(self, tool_calls: list) -> list:
        """
        Execute tool calls with retry logic.

        Args:
            tool_calls: List of {name, args} dicts from LLM response

        Returns:
            List of tool result dicts with tool_call_id, name, result
        """
        results = []
        for call in tool_calls:
            tool_name = call.get("name") or call.get("function", {}).get("name", "unknown")
            tool_args = call.get("args") or call.get("function", {}).get("arguments", {})

            # Find the tool
            tool = next((t for t in self.tools if t.name == tool_name), None)
            if not tool:
                results.append({
                    "tool_call_id": call.get("id", "unknown"),
                    "name": tool_name,
                    "result": f"Error: Tool '{tool_name}' not found",
                    "status": "error",
                })
                continue

            try:
                policy = self._get_policy(tool_name)
                result = self._call_tool_with_retry(tool, tool_args, policy)
                results.append({
                    "tool_call_id": call.get("id", "unknown"),
                    "name": tool_name,
                    "result": result,
                    "status": "success",
                })
            except Exception as e:
                results.append({
                    "tool_call_id": call.get("id", "unknown"),
                    "name": tool_name,
                    "result": f"Error after {policy.max_retries + 1} attempts: {str(e)}",
                    "status": "error",
                })

        return results

    def _call_tool_with_retry(self, tool, tool_args: dict, policy: RetryPolicy, on_retry=None) -> Any:
        """Call a tool with retry, handling different error types.

        Args:
            tool: The tool to invoke
            tool_args: Arguments to pass to the tool
            policy: Retry policy to use
            on_retry: Optional callback on_retry(attempt, max_retries, delay, error) called on each retry
        """
        last_error: Optional[Exception] = None

        for attempt in range(policy.max_retries + 1):
            try:
                return tool.invoke(tool_args)
            except Exception as e:
                last_error = e
                category = classify_error(e)

                if attempt >= policy.max_retries:
                    print(f"  [RETRY] [{tool.name}] {category} error: max retries ({policy.max_retries}) reached. Last error: {e}")
                    if on_retry:
                        on_retry(attempt, policy.max_retries, 0, str(e), True)
                    raise

                delay = policy.get_delay(attempt)
                print(f"  [RETRY] [{tool.name}] {category} error (attempt {attempt + 1}/{policy.max_retries + 1}): {e}")
                print(f"  [RETRY] [{tool.name}] Retrying in {delay:.1f}s...")
                if on_retry:
                    on_retry(attempt + 1, policy.max_retries, delay, str(e), False)
                time.sleep(delay)

        raise last_error


# ── Global executor instance ──────────────────────────
_executor: Optional[RetryToolExecutor] = None


def get_retry_executor() -> RetryToolExecutor:
    """Get or create the global retry executor."""
    global _executor
    if _executor is None:
        _executor = RetryToolExecutor(TOOLS)
    return _executor