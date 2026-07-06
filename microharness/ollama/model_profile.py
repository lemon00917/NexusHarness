"""
Model Profile System
=====================
Maps model capabilities so prompts and API calls auto-adapt.

Different models need different strategies:
- deepseek-r1 → native thinking, format:json safe
- qwen3.5:4b → prompt CoT, parse JSON, relaxed prompts
- qwen2.5:3b → prompt CoT, parse JSON, strict prompts

Usage:
    profile = get_profile("deepseek-r1:1.5b")
    client = OllamaClient(format_json=profile.format_json, ...)
    prompt = build_prompt(profile, ...)
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ModelProfile:
    """Capabilities and recommended strategies for a model."""

    # ── Thinking strategy ──
    # "native": model internally thinks (e.g. deepseek-r1)
    #   → Ollama returns thinking + content separately
    #   → prompts should be DIRECT (no CoT steps), model handles reasoning
    # "prompt_cot": we write step-by-step in the prompt
    #   → prompts include "第1步…第2步…" instructions
    thinking: str = "prompt_cot"

    # ── JSON output strategy ──
    # "format_json": use Ollama's `format: json` (grammar-constrained)
    #   → guaranteed valid JSON, but CoT must be inside JSON field
    # "parse": free-text output + regex extraction
    #   → allows CoT preamble, but needs fallback parsing
    json_mode: str = "parse"

    # ── Prompt style ──
    # "strict": explicit steps, negative examples, pre-injected keywords
    # "relaxed": simpler prompts, trust model to figure it out
    prompt_style: str = "strict"

    # ── API parameters ──
    # temperature override (None = use default 0.1)
    temperature: Optional[float] = None
    # num_predict override for judge-like calls
    num_predict: int = 256

    # ── Native thinking config ──
    # For "native" thinking models, we can still use format:json on the
    # content output. The model sees the format constraint during generation.
    native_thinking_format_json: bool = True


# ═══════════════════════════════════════════════════════════════
# Profile Registry
# ─────────────────────────────────────────────────
# Matched by prefix — first matching key wins.
# Order matters: put more specific prefixes first.
# ═══════════════════════════════════════════════════════════════

PROFILES: dict[str, ModelProfile] = {
    # DeepSeek-R1 series: native thinking, can use format:json
    "deepseek-r1": ModelProfile(
        thinking="native",
        json_mode="format_json",
        prompt_style="relaxed",
        temperature=0.0,
        num_predict=512,
        native_thinking_format_json=True,
    ),
    # Qwen 3.5 series: smarter than 2.5, relaxed prompts OK
    "qwen3.5": ModelProfile(
        thinking="prompt_cot",
        json_mode="parse",
        prompt_style="relaxed",
        temperature=0.1,
        num_predict=1024,
    ),
    # Qwen 2.5 series: needs strict prompting, heavy CoT guidance
    "qwen2.5": ModelProfile(
        thinking="prompt_cot",
        json_mode="parse",
        prompt_style="strict",
        temperature=0.1,
        num_predict=1024,
    ),
    # SmallThinker: similar to qwen2.5
    "SmallThinker": ModelProfile(
        thinking="prompt_cot",
        json_mode="parse",
        prompt_style="strict",
        temperature=0.1,
        num_predict=1024,
    ),
}

# Default profile for unknown models (conservative: like qwen2.5:3b)
DEFAULT_PROFILE = ModelProfile()


def get_profile(model_name: str) -> ModelProfile:
    """Get profile for a model. Matches by prefix (case-insensitive)."""
    if not model_name:
        return DEFAULT_PROFILE
    name_lower = model_name.lower()
    for prefix, profile in PROFILES.items():
        if name_lower.startswith(prefix.lower()):
            return profile
    return DEFAULT_PROFILE


def get_profile_summary(model_name: str) -> str:
    """One-line summary of model capabilities."""
    p = get_profile(model_name)
    return (
        f"[{model_name}] thinking={p.thinking} json={p.json_mode} "
        f"style={p.prompt_style} temp={p.temperature} npredict={p.num_predict}"
    )


def pick_fallback_for_judge(model_name: str) -> str:
    """Pick a fallback model for when the primary judge model returns empty.

    Strategy: find a model with native thinking capability that's different
    from the current one. Native thinking models are more reliable at
    producing structured JSON output.

    Returns fallback model name, or empty string if none available.
    """
    p = get_profile(model_name)
    # If current model is already native thinking, no fallback needed
    if p.thinking == "native":
        return ""

    # Find any native-thinking model from the profile registry
    for prefix, profile in PROFILES.items():
        if profile.thinking == "native" and not model_name.lower().startswith(prefix.lower()):
            # Use the prefix as the model name — caller can try this model
            return f"{prefix}:latest"

    return ""


def is_model_available(model_name: str) -> bool:
    """Check if a model is available in Ollama (lightweight)."""
    try:
        import requests
        resp = requests.get("http://localhost:11434/api/tags", timeout=3)
        if resp.status_code == 200:
            models = [m.get("name", "") for m in resp.json().get("models", [])]
            # Match by prefix
            name_lower = model_name.lower()
            for m in models:
                if m.lower().startswith(name_lower.rstrip(":latest").lower()):
                    return True, m  # Return actual model name
    except Exception:
        pass
    return False, model_name


def find_native_thinking_model(available_only: bool = True) -> str:
    """Find a model with native thinking capability.

    Args:
        available_only: if True, only return models actually installed in Ollama

    Returns model name, or empty string.
    """
    for prefix, profile in PROFILES.items():
        if profile.thinking == "native":
            candidate = f"{prefix}:latest"
            if available_only:
                ok, actual = is_model_available(candidate)
                if ok:
                    return actual
            else:
                return candidate
    return ""
