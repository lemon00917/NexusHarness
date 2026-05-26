"""
Token Tracker Module
====================
Track LLM usage: call count, token counts, and cost estimation per provider.

Features:
- Per-call recording with timestamps
- Multi-provider pricing support
- Cost estimation based on provider/model
- Usage history and aggregated statistics
- Extensible pricing registry

Usage:
    from token_tracker import TokenTracker

    tracker = TokenTracker()
    tracker.record("openai", "gpt-4o", 1000, 500)

    summary = tracker.get_summary()
    # {'total_calls': 1, 'total_tokens': 1500, 'total_cost_usd': 0.0075}
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple


# ──────────────────────── Constants ────────────────────────

# Default pricing for unknown models (USD per million tokens)
DEFAULT_INPUT_PRICE = 1.0  # $1.0 per million input tokens
DEFAULT_OUTPUT_PRICE = 3.0  # $3.0 per million output tokens


# ──────────────────────── Pricing Registry ────────────────────────

class PricingRegistry:
    """
    Registry for LLM provider pricing.

    Stores prices as USD per million tokens.
    Supports exact model matches and prefix-based fallback.

    Usage:
        registry = PricingRegistry()

        # Register pricing
        registry.register("openai", "gpt-4o", input_price=2.5, output_price=10.0)

        # Look up pricing (with fallback to defaults)
        input_price, output_price = registry.get_pricing("openai", "gpt-4o")
    """

    def __init__(self):
        """Initialize with built-in pricing data."""
        # Structure: {provider: {model: (input_price, output_price)}}
        self._pricing: Dict[str, Dict[str, Tuple[float, float]]] = {}
        self._initialize_defaults()

    def _initialize_defaults(self) -> None:
        """
        Initialize built-in provider pricing.

        Prices are in USD per million tokens (input/output).
        Based on public pricing as of 2025.
        """
        default_pricing = {
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
            # Moonshot (Kimi)
            "kimi": {
                "moonshot-v1-8k": (0.03, 0.12),
                "moonshot-v1-32k": (0.06, 0.24),
            },
            # Qwen (Alibaba)
            "qwen": {
                "qwen-plus": (0.004, 0.012),
                "qwen-turbo": (0.002, 0.006),
                "qwen-max": (0.02, 0.06),
            },
            # GLM (Zhipu)
            "glm": {
                "glm-4": (0.05, 0.15),
                "glm-4-flash": (0.001, 0.005),
                "glm-4-air": (0.003, 0.009),
            },
            # MiniMax - pricing not publicly available, use zero as placeholder
            "minimax": {
                "MiniMax-M2": (0.0, 0.0),
                "MiniMax-M2.7": (0.0, 0.0),
                "abab6.5s-chat": (0.0, 0.0),
            },
            # Xiaomi (no public pricing)
            "xiaomi": {},
            # Custom providers
            "custom": {},
        }

        for provider, models in default_pricing.items():
            for model, (input_price, output_price) in models.items():
                self.register(provider, model, input_price, output_price)

    def register(
        self,
        provider: str,
        model: str,
        input_price: float,
        output_price: float,
    ) -> None:
        """
        Register pricing for a provider/model combination.

        Args:
            provider: Provider name (e.g., "openai")
            model: Model name (e.g., "gpt-4o")
            input_price: USD per million input tokens
            output_price: USD per million output tokens

        Raises:
            ValueError: If prices are negative
        """
        if input_price < 0 or output_price < 0:
            raise ValueError(
                f"Prices must be non-negative, got input={input_price}, "
                f"output={output_price}"
            )

        if provider not in self._pricing:
            self._pricing[provider] = {}

        self._pricing[provider][model] = (input_price, output_price)

    def get_pricing(
        self,
        provider: str,
        model: str,
    ) -> Tuple[float, float]:
        """
        Get pricing for a provider/model, with fallback logic.

        Lookup order:
        1. Exact provider + model match
        2. Provider match + model prefix match
        3. Provider match (use cheapest known model)
        4. Default pricing

        Args:
            provider: Provider name
            model: Model name

        Returns:
            Tuple of (input_price, output_price) in USD per million tokens
        """
        # 1. Exact match
        if provider in self._pricing:
            provider_models = self._pricing[provider]
            if model in provider_models:
                return provider_models[model]

            # 2. Prefix match (e.g., "gpt-4o-2024-08-06" matches "gpt-4o")
            for known_model, prices in provider_models.items():
                if model.startswith(known_model):
                    return prices

            # 3. Provider exists but model unknown: use average of known models
            if provider_models:
                avg_input = sum(p[0] for p in provider_models.values()) / len(provider_models)
                avg_output = sum(p[1] for p in provider_models.values()) / len(provider_models)
                return (avg_input, avg_output)

        # 4. Unknown provider: use defaults
        return (DEFAULT_INPUT_PRICE, DEFAULT_OUTPUT_PRICE)

    def list_providers(self) -> List[str]:
        """Get list of registered providers."""
        return sorted(self._pricing.keys())

    def list_models(self, provider: str) -> List[str]:
        """
        Get list of models for a provider.

        Args:
            provider: Provider name

        Returns:
            List of model names
        """
        if provider in self._pricing:
            return sorted(self._pricing[provider].keys())
        return []

    def get_all_pricing(self) -> Dict[str, Dict[str, Dict[str, float]]]:
        """
        Get all registered pricing information.

        Returns:
            Nested dict of pricing data
        """
        result = {}
        for provider, models in self._pricing.items():
            result[provider] = {
                model: {
                    "input_price_per_million": input_price,
                    "output_price_per_million": output_price,
                }
                for model, (input_price, output_price) in models.items()
            }
        return result


# ──────────────────────── Data Models ────────────────────────

@dataclass
class CallRecord:
    """Record of a single LLM API call."""
    timestamp: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)


# ──────────────────────── Token Tracker ────────────────────────

class TokenTracker:
    """
    Tracks LLM token usage and costs across multiple providers.

    Features:
    - Per-call recording with metadata
    - Automatic cost calculation using pricing registry
    - Aggregated usage statistics
    - Usage history export
    - Provider/model breakdown

    Usage:
        tracker = TokenTracker()

        # Record a call (cost calculated automatically)
        tracker.record("openai", "gpt-4o", input_tokens=1000, output_tokens=500)

        # Get summary
        summary = tracker.get_summary()

        # Get per-provider breakdown
        breakdown = tracker.get_provider_breakdown()
    """

    def __init__(self, pricing: Optional[PricingRegistry] = None):
        """
        Initialize token tracker.

        Args:
            pricing: Pricing registry (uses default if None)
        """
        self.calls: List[CallRecord] = []
        self._pricing = pricing or PricingRegistry()

    def record(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost: Optional[float] = None,
    ) -> CallRecord:
        """
        Record a single LLM API call.

        If cost is not provided, it will be calculated automatically
        based on the pricing registry.

        Args:
            provider: Provider name (e.g., "openai")
            model: Model name (e.g., "gpt-4o")
            input_tokens: Number of input/prompt tokens
            output_tokens: Number of output/completion tokens
            cost: Cost in USD (auto-calculated if None)

        Returns:
            The recorded CallRecord

        Raises:
            ValueError: If token counts are negative
        """
        # Validate inputs
        if input_tokens < 0:
            raise ValueError(f"input_tokens must be non-negative, got {input_tokens}")
        if output_tokens < 0:
            raise ValueError(f"output_tokens must be non-negative, got {output_tokens}")

        # Calculate cost if not provided
        if cost is None:
            cost = self._calculate_cost(provider, model, input_tokens, output_tokens)

        record = CallRecord(
            timestamp=datetime.now().isoformat(),
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_usd=cost,
        )

        self.calls.append(record)
        return record

    def _calculate_cost(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """
        Calculate cost using the pricing registry.

        Args:
            provider: Provider name
            model: Model name
            input_tokens: Input tokens
            output_tokens: Output tokens

        Returns:
            Cost in USD
        """
        input_price, output_price = self._pricing.get_pricing(provider, model)

        input_cost = (input_tokens / 1_000_000) * input_price
        output_cost = (output_tokens / 1_000_000) * output_price

        return round(input_cost + output_cost, 8)

    def get_summary(self) -> dict:
        """
        Get aggregated usage statistics.

        Returns:
            Dictionary with summary statistics:
            - total_calls: Total number of API calls
            - total_input_tokens: Total input tokens
            - total_output_tokens: Total output tokens
            - total_tokens: Total tokens (input + output)
            - total_cost_usd: Total cost in USD
            - avg_cost_per_call: Average cost per call
            - first_call: Timestamp of first call
            - last_call: Timestamp of last call
        """
        if not self.calls:
            return {
                "total_calls": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "avg_cost_per_call": 0.0,
                "first_call": None,
                "last_call": None,
            }

        total_input = sum(r.input_tokens for r in self.calls)
        total_output = sum(r.output_tokens for r in self.calls)
        total_cost = sum(r.cost_usd for r in self.calls)
        call_count = len(self.calls)

        return {
            "total_calls": call_count,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "total_cost_usd": round(total_cost, 6),
            "avg_cost_per_call": round(total_cost / call_count, 6) if call_count > 0 else 0.0,
            "first_call": self.calls[0].timestamp,
            "last_call": self.calls[-1].timestamp,
        }

    def get_provider_breakdown(self) -> Dict[str, dict]:
        """
        Get usage breakdown by provider.

        Returns:
            Dictionary mapping provider -> usage statistics
        """
        breakdown = {}

        for record in self.calls:
            if record.provider not in breakdown:
                breakdown[record.provider] = {
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                    "models": set(),
                }

            stats = breakdown[record.provider]
            stats["calls"] += 1
            stats["input_tokens"] += record.input_tokens
            stats["output_tokens"] += record.output_tokens
            stats["total_tokens"] += record.total_tokens
            stats["cost_usd"] += record.cost_usd
            stats["models"].add(record.model)

        # Convert sets to lists and round costs
        for provider in breakdown:
            breakdown[provider]["models"] = sorted(breakdown[provider]["models"])
            breakdown[provider]["cost_usd"] = round(breakdown[provider]["cost_usd"], 6)

        return breakdown

    def get_model_breakdown(self) -> Dict[str, dict]:
        """
        Get usage breakdown by model.

        Returns:
            Dictionary mapping "provider/model" -> usage statistics
        """
        breakdown = {}

        for record in self.calls:
            key = f"{record.provider}/{record.model}"
            if key not in breakdown:
                breakdown[key] = {
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                }

            stats = breakdown[key]
            stats["calls"] += 1
            stats["input_tokens"] += record.input_tokens
            stats["output_tokens"] += record.output_tokens
            stats["total_tokens"] += record.total_tokens
            stats["cost_usd"] += record.cost_usd

        for key in breakdown:
            breakdown[key]["cost_usd"] = round(breakdown[key]["cost_usd"], 6)

        return breakdown

    def get_history(self, limit: Optional[int] = None) -> List[dict]:
        """
        Get per-call history, most recent last.

        Args:
            limit: Maximum number of records (None = all)

        Returns:
            List of call record dictionaries
        """
        records = self.calls[-limit:] if limit else self.calls
        return [r.to_dict() for r in records]

    def get_recent_calls(self, n: int = 10) -> List[dict]:
        """
        Get the n most recent calls.

        Args:
            n: Number of recent calls

        Returns:
            List of call record dictionaries
        """
        return self.get_history(limit=n)

    def reset(self) -> None:
        """Clear all recorded calls."""
        self.calls.clear()

    def merge(self, other: "TokenTracker") -> None:
        """
        Merge records from another tracker.

        Args:
            other: Another TokenTracker instance
        """
        self.calls.extend(other.calls)
        # Sort by timestamp, then by provider/model for stable order
        self.calls.sort(key=lambda r: (r.timestamp, r.provider, r.model))

    @property
    def call_count(self) -> int:
        """Total number of recorded calls."""
        return len(self.calls)

    @property
    def total_tokens(self) -> int:
        """Total tokens across all calls."""
        return sum(r.total_tokens for r in self.calls)

    @property
    def total_cost(self) -> float:
        """Total cost in USD."""
        return round(sum(r.cost_usd for r in self.calls), 6)

    def estimate_cost(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """
        Estimate cost for a hypothetical call without recording it.

        Args:
            provider: Provider name
            model: Model name
            input_tokens: Estimated input tokens
            output_tokens: Estimated output tokens

        Returns:
            Estimated cost in USD
        """
        return self._calculate_cost(provider, model, input_tokens, output_tokens)

    def register_pricing(
        self,
        provider: str,
        model: str,
        input_price: float,
        output_price: float,
    ) -> None:
        """
        Register or update pricing for a provider/model.

        Args:
            provider: Provider name
            model: Model name
            input_price: USD per million input tokens
            output_price: USD per million output tokens
        """
        self._pricing.register(provider, model, input_price, output_price)


# ──────────────────────── Module-Level Convenience ────────────────────────

# Global singleton instance for backward compatibility
token_stats = TokenTracker()


def get_cost(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """
    Calculate cost for a single call (convenience function).

    Args:
        provider: Provider name
        model: Model name
        input_tokens: Input token count
        output_tokens: Output token count

    Returns:
        Cost in USD
    """
    return token_stats.estimate_cost(provider, model, input_tokens, output_tokens)


def record_usage(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost: Optional[float] = None,
) -> CallRecord:
    """
    Record a single LLM call (convenience function).

    Args:
        provider: Provider name
        model: Model name
        input_tokens: Input token count
        output_tokens: Output token count
        cost: Cost in USD (auto-calculated if None)

    Returns:
        Recorded CallRecord
    """
    return token_stats.record(provider, model, input_tokens, output_tokens, cost)


def reset_tracking() -> None:
    """Reset all tracking data (convenience function)."""
    token_stats.reset()