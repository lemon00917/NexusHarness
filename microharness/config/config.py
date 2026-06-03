"""
Config Module
=============
统一从 .env 文件加载所有配置，并根据 PROVIDER 构建对应的 LangChain LLM 实例。

支持的 Provider：
  anthropic  → ChatAnthropic
  openai     → ChatOpenAI (api.openai.com)
  deepseek   → ChatOpenAI + base_url (api.deepseek.com)
  kimi       → ChatOpenAI + base_url (api.moonshot.cn/v1)
  minimax    → ChatOpenAI + base_url (api.minimax.chat/v1)
  qwen       → ChatOpenAI + base_url (dashscope.aliyuncs.com/...)
  glm        → ChatOpenAI + base_url (open.bigmodel.cn/...)

用法：
  from config import get_llm, MAIN_MODEL, MEMORY_MODEL, MAX_STEPS
  llm = get_llm(MAIN_MODEL)
"""

import os
import json
from pathlib import Path

# Config file for runtime settings (persisted separately from .env)
_CONFIG_FILE = Path(__file__).parent.parent.parent / "configs" / "config.json"


def load_config() -> dict:
    """Load persisted config from config.json."""
    if not _CONFIG_FILE.exists():
        return {}
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_config(settings: dict) -> None:
    """Persist config to config.json."""
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def get_config() -> dict:
    """Return current runtime config (env vars + config.json merged)."""
    base = {
        "provider": PROVIDER,
        "main_model": MAIN_MODEL,
        "memory_model": MEMORY_MODEL,
        "max_steps": MAX_STEPS,
    }
    base.update(load_config())
    return base


# ── 加载 .env ──────────────────────────────────────────────────────
# Project root is one level up from microharness/ (i.e., the repo root where .env lives)
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    with open(_env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key not in os.environ:
                os.environ[key] = value

# ── 读取配置项 ─────────────────────────────────────────────────────
PROVIDER: str = os.environ.get("PROVIDER", "anthropic").lower()

ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_COMPATIBLE_API_KEY: str = os.environ.get("OPENAI_COMPATIBLE_API_KEY", "")
OPENAI_COMPATIBLE_BASE_URL: str = os.environ.get("OPENAI_COMPATIBLE_BASE_URL", "")

MAIN_MODEL: str = os.environ.get("MAIN_MODEL", "claude-sonnet-4-20250514")
MEMORY_MODEL: str = os.environ.get("MEMORY_MODEL", "claude-haiku-4-5-20251001")
MAX_STEPS: int = int(os.environ.get("MAX_STEPS", "10"))

# ── Runtime Model Switching ─────────────────────────────────────────
# Store runtime overrides for model switching
_runtime_model_override: str = None
_runtime_provider_override: str = None


def get_runtime_model() -> str:
    """Get current model (runtime override or default)."""
    return _runtime_model_override or MAIN_MODEL


def get_runtime_provider() -> str:
    """Get current provider (runtime override or default)."""
    return _runtime_provider_override or PROVIDER


def set_runtime_model(model: str) -> None:
    """Set runtime model override."""
    global _runtime_model_override
    _runtime_model_override = model


def set_runtime_provider(provider: str) -> None:
    """Set runtime provider override."""
    global _runtime_provider_override
    _runtime_provider_override = provider


def switch_model(provider: str, model: str) -> dict:
    """
    Switch model at runtime.

    Args:
        provider: Provider name (ollama, anthropic, minimax, etc.)
        model: Model name

    Returns:
        Dict with new config
    """
    set_runtime_provider(provider)
    set_runtime_model(model)

    return {
        "provider": provider,
        "main_model": model,
        "status": "ok"
    }


def reset_runtime_config() -> None:
    """Reset runtime overrides to .env defaults."""
    global _runtime_model_override, _runtime_provider_override
    _runtime_model_override = None
    _runtime_provider_override = None

# OpenAI 兼容 provider 的默认 base_url
_DEFAULT_BASE_URLS: dict[str, str] = {
    "openai":   "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
    "kimi":     "https://api.moonshot.cn/v1",
    "minimax":  "https://api.minimax.chat/v1",
    "qwen":     "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "glm":      "https://open.bigmodel.cn/api/paas/v4",
    "xiaomi":   "https://api.xiaomimimo.com/v1"
}


# ── LLM 工厂函数 ───────────────────────────────────────────────────
def get_llm(model: str = None):
    """
    根据 PROVIDER 返回对应的 LangChain LLM 实例。

    Args:
        model: 模型名，如果为None则使用runtime配置的模型

    Returns:
        LangChain BaseChatModel 实例
    """
    # Use runtime config if no specific model provided
    if model is None:
        provider = get_runtime_provider()
        model = get_runtime_model()
    else:
        provider = get_runtime_provider()

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=model,
            api_key=ANTHROPIC_API_KEY,
        )

    if provider == "ollama":
        # Ollama uses OpenAI compatible API
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model,
            api_key="ollama-local",  # Ollama doesn't need real API key
            base_url="http://localhost:11434/v1",
        )

    # 其他 provider 统一走 OpenAI 兼容接口
    from langchain_openai import ChatOpenAI

    # base_url 优先用 .env 里的显式配置，没有则用内置默认值
    base_url = (
        OPENAI_COMPATIBLE_BASE_URL
        or _DEFAULT_BASE_URLS.get(provider, "")
    )

    if not base_url:
        raise ValueError(
            f"Unknown provider '{provider}'. "
            f"Please set OPENAI_COMPATIBLE_BASE_URL in .env, "
            f"or use one of: {list(_DEFAULT_BASE_URLS.keys())}"
        )

    return ChatOpenAI(
        model=model,
        api_key=OPENAI_COMPATIBLE_API_KEY,
        base_url=base_url,
    )


# ── 启动校验 ───────────────────────────────────────────────────────
def validate():
    """启动时校验必填配置，缺失则提前报错并给出提示"""
    if PROVIDER == "anthropic":
        if not ANTHROPIC_API_KEY:
            raise EnvironmentError(
                "\n❌ ANTHROPIC_API_KEY is not set.\n"
                "   Edit .env: ANTHROPIC_API_KEY=your_key_here\n"
            )
    else:
        if not OPENAI_COMPATIBLE_API_KEY:
            raise EnvironmentError(
                f"\n❌ OPENAI_COMPATIBLE_API_KEY is not set (provider={PROVIDER}).\n"
                f"   Edit .env: OPENAI_COMPATIBLE_API_KEY=your_key_here\n"
            )
        base_url = OPENAI_COMPATIBLE_BASE_URL or _DEFAULT_BASE_URLS.get(PROVIDER, "")
        if not base_url:
            raise EnvironmentError(
                f"\n❌ Cannot resolve base_url for provider '{PROVIDER}'.\n"
                f"   Edit .env: OPENAI_COMPATIBLE_BASE_URL=https://...\n"
            )

    # 检查 langchain-openai 是否安装（非 anthropic provider 需要）
    if PROVIDER != "anthropic":
        try:
            import langchain_openai  # noqa: F401
        except ImportError:
            raise ImportError(
                "\n❌ langchain-openai is not installed.\n"
                "   Run: pip install langchain-openai\n"
            )
