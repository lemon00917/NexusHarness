"""
Token Tracker Module
====================
Track LLM usage: call count, token counts, cost estimation per provider.

Usage:
    from token_tracker import token_stats, record_usage
    token_stats.record(provider, model, input_tokens, output_tokens, cost)
"""

from typing import Optional
from datetime import datetime
from dataclasses import dataclass, field

# Provider pricing in USD per million tokens (input/output)
# Prices are approximate and based on public pricing as of 2025
_PROVIDER_PRICING: dict[str, tuple[float, float]] = {
    # Anthropic
    "anthropic": {
        "claude-opus-4-6": (15.0, 75.0),
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-haiku-4-5-20251001": (0.8, 4.0),
        "claude-sonnet-4-20250514": (3.0, 15.0),
    },
    # OpenAI
    "openai": {
        "gpt-4o": (2.5, 10.0),
        "gpt-4o-mini": (0.15, 0.6),
        "gpt-4-turbo": (10.0, 30.0),
    },
    # DeepSeek
    "deepseek": {
        "deepseek-chat": (0.1, 0.4),
        "deepseek-coder": (0.14, 0.46),
    },
    # MiniMax
    "minimax": {
        "MiniMax-M2": (0.0, 0.0),  # Pricing not public, use estimate
        "MiniMax-M2.7": (0.0, 0.0),
        "abab6.5s-chat": (0.0, 0.0),
    },
    # Kimi (Moonshot)
    "kimi": {
        "moonshot-v1-8k": (0.03, 0.12),
        "moonshot-v1-32k": (0.06, 0.24),
    },
    # Qwen
    "qwen": {
        "qwen-plus": (0.004, 0.012),
        "qwen-turbo": (0.002, 0.006),
        "qwen-max": (0.02, 0.06),
    },
    # GLM
    "glm": {
        "glm-4": (0.05, 0.15),
        "glm-4-flash": (0.001, 0.005),
        "glm-4-air": (0.003, 0.009),
    },
    # Xiaomi
    "xiaomi": {
        # No public pricing, use estimate
    },
    # Custom
    "custom": {
        # No pricing, use estimate
    },
}

# Default pricing for unknown models (USD per million tokens)
_DEFAULT_INPUT_PRICE = 1.0
_DEFAULT_OUTPUT_PRICE = 3.0


@dataclass
class CallRecord:
    """A single LLM call record."""
    timestamp: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost: float


@dataclass
class TokenStats:
    """Singleton for tracking token usage across all LLM calls."""
    calls: list[CallRecord] = field(default_factory=list)

    def record(self, provider: str, model: str,
               input_tokens: int, output_tokens: int,
               cost: float = 0.0) -> None:
        """Record a single LLM call."""
        record = CallRecord(
            timestamp=datetime.now().isoformat(),
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost=cost,
        )
        self.calls.append(record)

    def get_summary(self) -> dict:
        """Get aggregated statistics."""
        if not self.calls:
            return {
                "total_calls": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "avg_cost_per_call": 0.0,
            }

        total_input = sum(r.input_tokens for r in self.calls)
        total_output = sum(r.output_tokens for r in self.calls)
        total_cost = sum(r.cost for r in self.calls)

        return {
            "total_calls": len(self.calls),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "total_cost_usd": round(total_cost, 6),
            "avg_cost_per_call": round(total_cost / len(self.calls), 6),
        }

    def get_history(self) -> list[dict]:
        """Get per-call history."""
        return [
            {
                "timestamp": r.timestamp,
                "provider": r.provider,
                "model": r.model,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "total_tokens": r.total_tokens,
                "cost": round(r.cost, 6),
            }
            for r in self.calls
        ]

    def reset(self) -> None:
        """Clear all records."""
        self.calls.clear()


def get_cost(provider: str, model: str,
             input_tokens: int, output_tokens: int) -> float:
    """Calculate cost based on provider and model pricing."""
    input_price, output_price = _get_pricing(provider, model)
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def _get_pricing(provider: str, model: str) -> tuple[float, float]:
    """Get pricing tuple (input, output) for a provider/model."""
    if provider in _PROVIDER_PRICING:
        model_prices = _PROVIDER_PRICING[provider]
        if model in model_prices:
            return model_prices[model]
    # Try to find by model prefix
    if provider in _PROVIDER_PRICING:
        model_prices = _PROVIDER_PRICING[provider]
        for known_model, prices in model_prices.items():
            if model.startswith(known_model.split("-")[0]):
                return prices
    return (_DEFAULT_INPUT_PRICE, _DEFAULT_OUTPUT_PRICE)


# Global singleton instance
token_stats = TokenStats()