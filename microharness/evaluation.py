"""
NexusHarness Evaluation Framework
=================================
Benchmark-driven assessment for agent quality measurement.

Supports:
- Standardized task definitions (JSON)
- Multiple validation rules (contains, exact, regex, tool_calls, llm_judge, hybrid)
- Multi-provider/model comparison
- Token cost tracking
- Result persistence to JSON
"""

import json
import re
import time
import os
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, Literal, Union, Any
from datetime import datetime

from . import config
from .config import get_llm
from .token_tracker import token_stats, get_cost
from .tools import TOOLS
from .prompts import get_system_prompt
from .memory import extract_and_save_memory

# Validation rule types
ValidationRule = Union[
    dict  # contains, exact, regex, tool_calls, llm_judge, hybrid
]


@dataclass
class BenchmarkTask:
    """A single benchmark task definition."""
    id: str
    category: str
    task: str
    validation: dict
    metadata: dict


@dataclass
class TaskResult:
    """Result of running a single benchmark task."""
    task_id: str
    provider: str
    model: str
    passed: bool
    score: float
    actual_output: str
    expected_output: str
    validation_details: str
    tokens_used: int
    cost_usd: float
    duration_ms: int
    steps_used: int
    tool_calls: list
    timestamp: str
    error: Optional[str] = None


@dataclass
class BenchmarkResult:
    """Result of running a full benchmark suite."""
    benchmark_id: str
    provider: str
    model: str
    run_id: str
    timestamp: str
    tasks_total: int
    tasks_passed: int
    tasks_failed: int
    pass_rate: float
    avg_score: float
    total_tokens: int
    total_cost_usd: float
    total_duration_ms: int
    task_results: list
    metadata: dict


# ──────────────────────────────────────────────────
# Validation Scorers
# ──────────────────────────────────────────────────

def score_contains(rule: dict, actual: str) -> tuple[bool, float, str]:
    value = rule.get("value", "")
    found = value in actual
    return found, 1.0 if found else 0.0, f"Found: '{value}'" if found else f"Not found: '{value}'"


def score_exact(rule: dict, actual: str) -> tuple[bool, float, str]:
    value = rule.get("value", "")
    matched = actual.strip() == value.strip()
    return matched, 1.0 if matched else 0.0, "Exact match" if matched else "No exact match"


def score_regex(rule: dict, actual: str) -> tuple[bool, float, str]:
    pattern = rule.get("pattern", "")
    try:
        match = re.search(pattern, actual, re.IGNORECASE | re.DOTALL)
        matched = match is not None
        return matched, 1.0 if matched else 0.0, f"Regex matched: '{pattern}'" if matched else f"No match: '{pattern}'"
    except re.error as e:
        return False, 0.0, f"Regex error: {e}"


def score_tool_calls(rule: dict, actual_calls: list) -> tuple[bool, float, str]:
    expected = rule.get("expected", [])
    allow_extra = rule.get("allow_extra", True)

    if not expected:
        return True, 1.0, "No expected tools specified"

    actual_names = [c.get("name") or (c.get("function", {}).get("name") if isinstance(c, dict) else str(c)) for c in actual_calls]

    if allow_extra:
        matched = sum(1 for t in expected if t in actual_names)
        score = matched / len(expected)
        passed = matched == len(expected)
    else:
        passed = expected == actual_names
        score = 1.0 if passed else 0.0

    return passed, score, f"Expected: {expected}, Got: {actual_names}"


def score_llm_judge(rule: dict, task: BenchmarkTask, actual: str, actual_calls: list) -> tuple[bool, float, str]:
    """Use a separate LLM to judge the response quality."""
    judge_model = rule.get("judge_model", "claude-haiku-4-5-20250501")
    criteria = rule.get("criteria", "Did the response successfully complete the task? Rate 0-1.")

    judge_prompt = f"""Task: {task.task}
Response: {actual}
Tool calls made: {actual_calls}
{criteria}
Respond with ONLY a JSON object: {{"passed": true/false, "score": 0.0-1.0, "reason": "..."}}"""

    try:
        judge_llm = get_llm(judge_model)
        from langchain_core.messages import HumanMessage
        response = judge_llm.invoke([HumanMessage(content=judge_prompt)])

        result_text = response.content if hasattr(response, "content") else str(response)
        # Try to parse JSON
        import json as json_lib
        result = json_lib.loads(result_text)
        return result.get("passed", False), result.get("score", 0.0), result.get("reason", "")
    except Exception as e:
        return False, 0.0, f"LLM judge error: {e}"


def score_hybrid(rule: dict, task: BenchmarkTask, actual: str, actual_calls: list) -> tuple[bool, float, str]:
    """Combine multiple validation rules."""
    rules = rule.get("rules", [])
    pass_threshold = rule.get("pass_threshold", 0.5)

    if not rules:
        return True, 1.0, "No rules specified"

    scores = []
    details_parts = []
    for subrule in rules:
        rule_type = subrule.get("type", "")
        if rule_type in ("contains", "exact", "regex"):
            passed, score, detail = _apply_text_rule(subrule, actual)
        elif rule_type == "tool_calls":
            passed, score, detail = score_tool_calls(subrule, actual_calls)
        else:
            score, detail = 0.0, f"Unknown rule type: {rule_type}"
        scores.append(score)
        details_parts.append(detail)

    avg_score = sum(scores) / len(scores)
    passed = avg_score >= pass_threshold
    return passed, avg_score, f"Hybrid: avg={avg_score:.2f} ({', '.join(details_parts)})"


def _apply_text_rule(rule: dict, actual: str) -> tuple[bool, float, str]:
    rule_type = rule.get("type", "")
    if rule_type == "contains":
        return score_contains(rule, actual)
    elif rule_type == "exact":
        return score_exact(rule, actual)
    elif rule_type == "regex":
        return score_regex(rule, actual)
    return False, 0.0, f"Unknown text rule: {rule_type}"


def _apply_rule(rule: dict, task: BenchmarkTask, actual: str, actual_calls: list) -> tuple[bool, float, str]:
    rule_type = rule.get("type", "")
    if rule_type in ("contains", "exact", "regex"):
        return _apply_text_rule(rule, actual)
    elif rule_type == "tool_calls":
        return score_tool_calls(rule, actual_calls)
    elif rule_type == "llm_judge":
        return score_llm_judge(rule, task, actual, actual_calls)
    elif rule_type == "hybrid":
        return score_hybrid(rule, task, actual, actual_calls)
    return False, 0.0, f"Unknown rule type: {rule_type}"


# ──────────────────────────────────────────────────
# Benchmark Runner
# ──────────────────────────────────────────────────

class BenchmarkRunner:
    """
    Runs benchmark tasks and collects results.
    """

    def __init__(
        self,
        benchmark_dir: str = "benchmarks",
        results_dir: str = "benchmark_results",
        auto_approve: bool = True,
        max_steps: int = 10,
    ):
        self.benchmark_dir = Path(benchmark_dir)
        self.results_dir = Path(results_dir)
        self.auto_approve = auto_approve
        self.max_steps = max_steps
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def load_benchmarks(self, category: Optional[str] = None) -> list[BenchmarkTask]:
        """Load all benchmark tasks, optionally filtered by category."""
        tasks = []
        search_path = self.benchmark_dir / category if category else self.benchmark_dir

        for json_file in search_path.rglob("*.json"):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                tasks.append(BenchmarkTask(
                    id=data.get("id", json_file.stem),
                    category=data.get("category", category or "general"),
                    task=data.get("task", ""),
                    validation=data.get("validation", {}),
                    metadata=data.get("metadata", {}),
                ))
            except Exception as e:
                print(f"  Warning: Failed to load {json_file}: {e}")

        return tasks

    def score_task(self, task: BenchmarkTask, actual_output: str, actual_calls: list) -> tuple[bool, float, str]:
        """Apply validation rule to task response."""
        return _apply_rule(task.validation, task, actual_output, actual_calls)

    def run_task(self, task: BenchmarkTask, provider: str, model: str, session_id: Optional[str] = None) -> TaskResult:
        """Run a single task and return result."""
        from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

        start_time = time.time()
        timestamp = datetime.now().isoformat()

        # Save original config
        original_provider = config.PROVIDER
        original_model = config.MAIN_MODEL

        try:
            # Override config for this run
            config.PROVIDER = provider
            config.MAIN_MODEL = model

            # Reset token stats for clean measurement
            token_stats.reset()

            # Build fresh LLM instance
            llm = get_llm(model).bind_tools(TOOLS)

            # Execute simplified agent loop
            messages = [HumanMessage(content=task.task)]
            step_count = 0
            all_tool_calls = []

            for step in range(self.max_steps):
                system = SystemMessage(content=get_system_prompt())
                response = llm.invoke([system] + messages)

                # Record tokens
                if hasattr(response, "usage_metadata"):
                    u = response.usage_metadata
                    input_tokens = u.get("input_tokens", 0)
                    output_tokens = u.get("output_tokens", 0)
                    cost = get_cost(provider, model, input_tokens, output_tokens)
                    token_stats.record(provider, model, input_tokens, output_tokens, cost)

                messages.append(response)
                step_count += 1

                # Record tool calls
                if hasattr(response, "tool_calls") and response.tool_calls:
                    all_tool_calls.extend(response.tool_calls)

                # No tool calls = task complete
                if not (hasattr(response, "tool_calls") and response.tool_calls):
                    break

                # Execute tools (auto-approved for benchmarking)
                for call in response.tool_calls:
                    tool_name = call.get("name")
                    tool_args = call.get("args", {})
                    tool = next((t for t in TOOLS if t.name == tool_name), None)
                    if tool:
                        result = tool.invoke(tool_args)
                        messages.append(ToolMessage(
                            content=str(result),
                            tool_call_id=call.get("id", ""),
                            name=tool_name
                        ))

            # Extract final response
            final_response = next(
                (m for m in reversed(messages)
                 if hasattr(m, "content") and isinstance(m.content, str) and m.content.strip()),
                None
            )
            actual_output = final_response.content if final_response else ""

            # Score
            passed, score, validation_details = self.score_task(task, actual_output, all_tool_calls)

            # Get token stats
            stats = token_stats.get_summary()

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            return TaskResult(
                task_id=task.id,
                provider=provider,
                model=model,
                passed=False,
                score=0.0,
                actual_output="",
                expected_output=task.validation.get("value", ""),
                validation_details=f"Error: {str(e)}",
                tokens_used=0,
                cost_usd=0.0,
                duration_ms=duration_ms,
                steps_used=0,
                tool_calls=[],
                timestamp=timestamp,
                error=str(e),
            )

        finally:
            # Restore original config
            config.PROVIDER = original_provider
            config.MAIN_MODEL = original_model

        duration_ms = int((time.time() - start_time) * 1000)
        stats = token_stats.get_summary()

        return TaskResult(
            task_id=task.id,
            provider=provider,
            model=model,
            passed=passed,
            score=score,
            actual_output=actual_output[:2000],  # Truncate long outputs
            expected_output=task.validation.get("value", ""),
            validation_details=validation_details,
            tokens_used=stats.get("total_tokens", 0),
            cost_usd=stats.get("total_cost_usd", 0.0),
            duration_ms=duration_ms,
            steps_used=step_count,
            tool_calls=[{"name": tc.get("name") or tc.get("function", {}).get("name") if isinstance(tc, dict) else str(tc)} for tc in all_tool_calls],
            timestamp=timestamp,
            error=None,
        )

    def run_benchmark(
        self,
        category: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        benchmark_ids: Optional[list[str]] = None,
    ) -> BenchmarkResult:
        """Run full benchmark suite."""
        provider = provider or config.PROVIDER
        model = model or config.MAIN_MODEL

        tasks = self.load_benchmarks(category)
        if benchmark_ids:
            tasks = [t for t in tasks if t.id in benchmark_ids]

        if not tasks:
            print(f"No tasks found for category={category}, ids={benchmark_ids}")
            return BenchmarkResult(
                benchmark_id=category or "all",
                provider=provider,
                model=model,
                run_id=f"run_{int(time.time())}",
                timestamp=datetime.now().isoformat(),
                tasks_total=0,
                tasks_passed=0,
                tasks_failed=0,
                pass_rate=0.0,
                avg_score=0.0,
                total_tokens=0,
                total_cost_usd=0.0,
                total_duration_ms=0,
                task_results=[],
                metadata={},
            )

        print(f"\n{'='*60}")
        print(f"  Running {len(tasks)} benchmark tasks")
        print(f"  Provider: {provider} | Model: {model}")
        print(f"{'='*60}\n")

        task_results = []
        tasks_passed = 0
        tasks_failed = 0
        total_tokens = 0
        total_cost = 0.0
        total_duration = 0

        for i, task in enumerate(tasks, 1):
            print(f"[{i}/{len(tasks)}] Task: {task.id} ({task.category})")
            print(f"       {task.task[:60]}...")

            result = self.run_task(task, provider, model)
            task_results.append(asdict(result))

            total_tokens += result.tokens_used
            total_cost += result.cost_usd
            total_duration += result.duration_ms
            passed_flag = result.passed
            score_val = result.score

            if passed_flag:
                tasks_passed += 1
                status = "✅ PASS"
            else:
                tasks_failed += 1
                status = "❌ FAIL"

            print(f"       {status} | Score: {score_val:.2f} | Tokens: {result.tokens_used} | Cost: ${result.cost_usd:.6f}")
            if result.error:
                print(f"       Error: {result.error}")
            print()

        pass_rate = tasks_passed / len(tasks) if tasks else 0
        avg_score = sum(r["score"] for r in task_results) / len(task_results) if task_results else 0

        benchmark_result = BenchmarkResult(
            benchmark_id=category or "all",
            provider=provider,
            model=model,
            run_id=f"run_{int(time.time())}",
            timestamp=datetime.now().isoformat(),
            tasks_total=len(tasks),
            tasks_passed=tasks_passed,
            tasks_failed=tasks_failed,
            pass_rate=pass_rate,
            avg_score=avg_score,
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            total_duration_ms=total_duration,
            task_results=task_results,
            metadata={"max_steps": self.max_steps},
        )

        # Save to disk
        result_file = self.results_dir / f"{benchmark_result.run_id}_{provider}_{model}.json"
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(asdict(benchmark_result), f, ensure_ascii=False, indent=2)
        print(f"Results saved to: {result_file}")

        return benchmark_result

    def compare_providers(
        self,
        providers: list[str],
        model: str,
        category: Optional[str] = None,
    ) -> list[BenchmarkResult]:
        """Run same benchmark across multiple providers."""
        results = []

        for provider in providers:
            print(f"\n{'#'*60}")
            print(f"# Provider: {provider}")
            print(f"{'#'*60}")

            result = self.run_benchmark(
                category=category,
                provider=provider,
                model=model,
            )
            results.append(result)

        # Print comparison table
        print(f"\n{'='*60}")
        print(f"  COMPARISON RESULTS")
        print(f"{'='*60}")
        print(f"{'Provider':<15} {'Pass Rate':<10} {'Avg Score':<10} {'Total Cost':<12} {'Tokens':<10}")
        print(f"{'-'*60}")
        for r in results:
            print(f"{r.provider:<15} {r.pass_rate*100:>8.1f}%  {r.avg_score:>8.3f}   ${r.total_cost_usd:>10.6f}  {r.total_tokens:>8,}")

        return results


def print_benchmark_result(result: BenchmarkResult):
    """Pretty print a single benchmark result."""
    print(f"\n{'='*60}")
    print(f"  BENCHMARK RESULT: {result.benchmark_id}")
    print(f"{'='*60}")
    print(f"  Provider/Model: {result.provider} / {result.model}")
    print(f"  Run ID: {result.run_id}")
    print(f"  Timestamp: {result.timestamp}")
    print(f"{'-'*60}")
    print(f"  Tasks: {result.tasks_passed}/{result.tasks_total} passed ({result.pass_rate*100:.1f}%)")
    print(f"  Avg Score: {result.avg_score:.3f}")
    print(f"  Total Tokens: {result.total_tokens:,}")
    print(f"  Total Cost: ${result.total_cost_usd:.6f}")
    print(f"  Duration: {result.total_duration_ms/1000:.1f}s")
    print(f"{'='*60}")