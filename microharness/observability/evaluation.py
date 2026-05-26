"""
NexusHarness Evaluation Framework
=================================
Benchmark-driven assessment for agent quality measurement.

Features:
- Standardized task definitions (JSON)
- Multiple validation rules (contains, exact, regex, tool_calls, llm_judge, hybrid)
- Multi-provider/model comparison
- Token cost tracking
- Result persistence and reporting

Architecture:
    BenchmarkRunner
    ├── TaskLoader (loads JSON task definitions)
    ├── ValidationEngine (scores agent responses)
    ├── AgentExecutor (runs single task)
    └── ResultReporter (formats and saves results)
"""

import json
import os
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Union

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from microharness.config import get_config, get_llm
from microharness.config.prompts import get_system_prompt
from microharness.observability.token_tracker import token_stats, get_cost
from microharness.agent.tools import TOOLS
from microharness.agent.guard import should_confirm


# ──────────────────────── Constants ────────────────────────

# Maximum output length to store in results
MAX_OUTPUT_LENGTH = 2000

# Default validation threshold for hybrid scoring
DEFAULT_PASS_THRESHOLD = 0.5

# Valid validation rule types
VALID_RULE_TYPES = {"contains", "exact", "regex", "tool_calls", "llm_judge", "hybrid"}

# Default judge model (configurable via JUDGE_MODEL env var)
DEFAULT_JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-haiku-4-20250501")


# ──────────────────────── Data Models ────────────────────────

@dataclass
class BenchmarkTask:
    """A single benchmark task definition loaded from JSON."""
    id: str
    category: str
    task: str
    validation: dict
    metadata: dict = field(default_factory=dict)


@dataclass
class TaskResult:
    """Result of executing a single benchmark task."""
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
    tool_calls: List[str]
    timestamp: str
    error: Optional[str] = None


@dataclass
class BenchmarkResult:
    """Aggregated result of a full benchmark run."""
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
    task_results: List[dict]
    metadata: dict = field(default_factory=dict)


# ──────────────────────── Exceptions ────────────────────────

class ValidationError(Exception):
    """Raised when validation rule configuration is invalid."""
    pass


class TaskExecutionError(Exception):
    """Raised when task execution fails."""
    pass


# ──────────────────────── Validation Engine ────────────────────────

class ValidationEngine:
    """
    Applies validation rules to agent responses.

    Supports multiple rule types:
    - contains: Check if text contains a specific string
    - exact: Check for exact text match
    - regex: Match against a regular expression
    - tool_calls: Validate tool invocation sequence
    - llm_judge: Use an LLM to assess response quality
    - hybrid: Combine multiple rules with weighted scoring
    """

    # Maps rule types to their scoring functions
    SCORER_REGISTRY: Dict[str, callable] = {}

    @classmethod
    def register(cls, rule_type: str):
        """Decorator to register a scoring function for a rule type."""
        def decorator(func):
            cls.SCORER_REGISTRY[rule_type] = func
            return func
        return decorator

    def score(
        self,
        task: BenchmarkTask,
        actual_output: str,
        tool_calls: List[dict],
    ) -> Tuple[bool, float, str]:
        """
        Apply validation rule to agent response.

        Args:
            task: The benchmark task definition
            actual_output: Agent's final text response
            tool_calls: List of tool calls made during execution

        Returns:
            Tuple of (passed, score, details)

        Raises:
            ValidationError: If rule configuration is invalid
        """
        rule = task.validation
        rule_type = rule.get("type", "")

        if not rule_type:
            raise ValidationError(
                f"Task '{task.id}' has no validation rule type specified"
            )

        if rule_type not in self.SCORER_REGISTRY:
            raise ValidationError(
                f"Unknown validation rule type: '{rule_type}'. "
                f"Valid types: {sorted(self.SCORER_REGISTRY.keys())}"
            )

        scorer = self.SCORER_REGISTRY[rule_type]

        try:
            return scorer(task, rule, actual_output, tool_calls)
        except Exception as e:
            return False, 0.0, f"Validation error: {type(e).__name__}: {e}"


# ──────────────────────── Validation Rules ────────────────────────

# Create singleton instance
validation_engine = ValidationEngine()


@ValidationEngine.register("contains")
def _score_contains(task: BenchmarkTask, rule: dict, actual: str, calls: List[dict]) -> Tuple[bool, float, str]:
    """Check if actual output contains expected text."""
    value = rule.get("value", "")
    if not value:
        return True, 1.0, "No expected value specified (always passes)"

    found = value in actual
    detail = f"Contains '{value}'" if found else f"Missing '{value}'"
    return found, 1.0 if found else 0.0, detail


@ValidationEngine.register("exact")
def _score_exact(task: BenchmarkTask, rule: dict, actual: str, calls: List[dict]) -> Tuple[bool, float, str]:
    """Check for exact text match (whitespace-insensitive)."""
    value = rule.get("value", "")
    case_sensitive = rule.get("case_sensitive", False)

    actual_clean = actual.strip()
    expected_clean = value.strip()

    if not case_sensitive:
        actual_clean = actual_clean.lower()
        expected_clean = expected_clean.lower()

    matched = actual_clean == expected_clean
    detail = "Exact match" if matched else f"Expected '{expected_clean}', got '{actual_clean[:100]}'"
    return matched, 1.0 if matched else 0.0, detail


@ValidationEngine.register("regex")
def _score_regex(task: BenchmarkTask, rule: dict, actual: str, calls: List[dict]) -> Tuple[bool, float, str]:
    """Match output against a regular expression pattern."""
    pattern = rule.get("pattern", "")

    if not pattern:
        return True, 1.0, "No pattern specified (always passes)"

    flags = re.IGNORECASE | re.DOTALL
    if rule.get("multiline", True):
        flags |= re.MULTILINE

    try:
        match = re.search(pattern, actual, flags)
        matched = match is not None

        if matched and match.groups():
            detail = f"Regex matched: '{pattern}' (groups: {match.groups()})"
        elif matched:
            detail = f"Regex matched: '{pattern}'"
        else:
            detail = f"No regex match: '{pattern}'"

        return matched, 1.0 if matched else 0.0, detail

    except re.error as e:
        return False, 0.0, f"Invalid regex pattern: {e}"


@ValidationEngine.register("tool_calls")
def _score_tool_calls(task: BenchmarkTask, rule: dict, actual: str, calls: List[dict]) -> Tuple[bool, float, str]:
    """Validate that expected tools were called."""
    expected = rule.get("expected", [])
    allow_extra = rule.get("allow_extra", True)

    if not expected:
        return True, 1.0, "No expected tools specified"

    # Extract tool names from call records
    actual_names = [
        call.get("name") or call.get("function", {}).get("name", str(call))
        for call in calls
    ]

    if allow_extra:
        # Check that all expected tools are present
        matched_count = sum(1 for tool in expected if tool in actual_names)
        score = matched_count / len(expected) if expected else 1.0
        passed = matched_count == len(expected)
    else:
        # Check for exact match (order-independent)
        passed = sorted(expected) == sorted(actual_names)
        score = 1.0 if passed else 0.0

    detail = f"Expected: {expected}, Got: {actual_names}"
    return passed, score, detail


@ValidationEngine.register("llm_judge")
def _score_llm_judge(task: BenchmarkTask, rule: dict, actual: str, calls: List[dict]) -> Tuple[bool, float, str]:
    """
    Use a separate LLM to judge the response quality.

    The judge model evaluates the response against specified criteria
    and returns a structured pass/fail with score.
    """
    judge_model = rule.get("judge_model", DEFAULT_JUDGE_MODEL)
    criteria = rule.get("criteria", "Did the response successfully complete the task?")
    max_score = rule.get("max_score", 1.0)

    # Build evaluation prompt
    judge_prompt = _build_judge_prompt(task.task, actual, calls, criteria)

    try:
        judge_llm = get_llm(judge_model)
        response = judge_llm.invoke([HumanMessage(content=judge_prompt)])

        result_text = response.content if hasattr(response, "content") else str(response)
        result = _parse_judge_response(result_text)

        # Normalize score to 0.0-1.0 range
        normalized_score = result.get("score", 0.0) / max_score
        normalized_score = max(0.0, min(1.0, normalized_score))

        return (
            result.get("passed", False),
            normalized_score,
            result.get("reason", "No reason provided")
        )

    except json.JSONDecodeError as e:
        return False, 0.0, f"Failed to parse judge response: {e}"
    except Exception as e:
        return False, 0.0, f"LLM judge error: {type(e).__name__}: {e}"


@ValidationEngine.register("hybrid")
def _score_hybrid(task: BenchmarkTask, rule: dict, actual: str, calls: List[dict]) -> Tuple[bool, float, str]:
    """
    Combine multiple validation rules with weighted scoring.

    Each sub-rule is scored independently, then averaged.
    Task passes if average score meets the threshold.
    """
    rules = rule.get("rules", [])
    pass_threshold = rule.get("pass_threshold", DEFAULT_PASS_THRESHOLD)
    weights = rule.get("weights", None)  # Optional per-rule weights

    if not rules:
        return True, 1.0, "No sub-rules specified"

    scores = []
    details = []

    for i, subrule in enumerate(rules):
        try:
            # Dispatch to the appropriate scorer based on sub-rule type
            passed, score, detail = _dispatch_subrule(task, subrule, actual, calls)
            scores.append(score)
            details.append(f"[{i+1}] {detail}")
        except Exception as e:
            scores.append(0.0)
            details.append(f"[{i+1}] Error: {e}")

    # Calculate weighted or simple average
    if weights and len(weights) == len(scores):
        weighted_sum = sum(w * s for w, s in zip(weights, scores))
        weight_sum = sum(weights)
        avg_score = weighted_sum / weight_sum if weight_sum > 0 else 0.0
    else:
        avg_score = sum(scores) / len(scores) if scores else 0.0

    passed = avg_score >= pass_threshold

    detail = (
        f"Hybrid: avg={avg_score:.2f} (threshold={pass_threshold}) | "
        f"{'; '.join(details)}"
    )

    return passed, avg_score, detail


# ──────────────────────── Validation Helpers ────────────────────────

def _dispatch_subrule(
    task: BenchmarkTask,
    rule: dict,
    actual: str,
    calls: List[dict],
) -> Tuple[bool, float, str]:
    """
    Dispatch a sub-rule to the appropriate scorer.

    Args:
        task: Benchmark task
        rule: Sub-rule definition
        actual: Agent output text
        calls: Tool calls made

    Returns:
        (passed, score, details) tuple
    """
    rule_type = rule.get("type", "")
    scorer = ValidationEngine.SCORER_REGISTRY.get(rule_type)

    if not scorer:
        return False, 0.0, f"Unknown rule type: {rule_type}"

    return scorer(task, rule, actual, calls)


def _build_judge_prompt(task: str, actual: str, calls: List[dict], criteria: str) -> str:
    """
    Build the evaluation prompt for the LLM judge.

    Args:
        task: Original task description
        actual: Agent's output
        calls: Tool calls made
        criteria: Evaluation criteria

    Returns:
        Formatted prompt string
    """
    calls_summary = _format_tool_calls_for_judge(calls)

    return f"""You are an impartial judge evaluating an AI agent's task performance.

TASK:
{task}

AGENT RESPONSE:
{actual}

TOOL CALLS MADE:
{calls_summary}

EVALUATION CRITERIA:
{criteria}

INSTRUCTIONS:
1. Evaluate the response against the criteria
2. Assign a score from 0.0 (complete failure) to 1.0 (perfect)
3. Determine if the task passed (score >= 0.5)
4. Provide a brief reason for your evaluation

Respond with ONLY a JSON object (no markdown, no extra text):
{{"passed": true/false, "score": 0.0-1.0, "reason": "brief explanation"}}"""


def _format_tool_calls_for_judge(calls: List[dict]) -> str:
    """Format tool calls for display in judge prompt."""
    if not calls:
        return "No tool calls were made."

    lines = []
    for i, call in enumerate(calls, 1):
        name = call.get("name") or call.get("function", {}).get("name", "unknown")
        args = call.get("args", call.get("function", {}).get("arguments", {}))
        lines.append(f"  {i}. {name}({json.dumps(args, ensure_ascii=False)})")

    return "\n".join(lines)


def _parse_judge_response(text: str) -> dict:
    """
    Parse the LLM judge's response, handling common formatting issues.

    Args:
        text: Raw response text

    Returns:
        Parsed dictionary with passed, score, reason

    Raises:
        json.JSONDecodeError: If response cannot be parsed
    """
    # Try to extract JSON from markdown code blocks
    json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if json_match:
        return json.loads(json_match.group(1))

    # Try to find bare JSON object
    json_match = re.search(r'\{[^{}]*"passed"[^{}]*\}', text, re.DOTALL)
    if json_match:
        return json.loads(json_match.group(0))

    # Fallback: try parsing entire response
    return json.loads(text.strip())


# ──────────────────────── Benchmark Runner ────────────────────────

class BenchmarkRunner:
    """
    Executes benchmark suites and collects results.

    Usage:
        runner = BenchmarkRunner()
        result = runner.run_benchmark(category="coding", provider="openai", model="gpt-4")
        runner.compare_providers(["openai", "anthropic"], "gpt-4")
    """

    def __init__(
        self,
        benchmark_dir: str = "benchmarks",
        results_dir: str = "benchmark_results",
        auto_approve: bool = True,
        max_steps: int = 10,
    ):
        """
        Initialize benchmark runner.

        Args:
            benchmark_dir: Directory containing JSON task definitions
            results_dir: Directory for saving results
            auto_approve: Automatically approve all tool calls
            max_steps: Maximum agent steps per task
        """
        self.benchmark_dir = Path(benchmark_dir)
        self.results_dir = Path(results_dir)
        self.auto_approve = auto_approve
        self.max_steps = max_steps or MAX_STEPS
        self.results_dir.mkdir(parents=True, exist_ok=True)

        # Initialize validation engine
        self.validator = ValidationEngine()

    # ──────────────────────── Task Loading ────────────────────────

    def load_benchmarks(self, category: Optional[str] = None) -> List[BenchmarkTask]:
        """
        Load benchmark tasks from JSON files.

        Args:
            category: Optional category filter (subdirectory name)

        Returns:
            List of BenchmarkTask objects
        """
        search_path = (
            self.benchmark_dir / category if category
            else self.benchmark_dir
        )

        if not search_path.exists():
            print(f"  Warning: Benchmark directory not found: {search_path}")
            return []

        tasks = []

        for json_file in search_path.rglob("*.json"):
            task = self._load_single_task(json_file, category or "general")
            if task:
                tasks.append(task)

        return tasks

    def _load_single_task(self, file_path: Path, default_category: str) -> Optional[BenchmarkTask]:
        """
        Load and validate a single task JSON file.

        Args:
            file_path: Path to JSON file
            default_category: Category to use if not specified in file

        Returns:
            BenchmarkTask or None if loading fails
        """
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))

            # Validate required fields
            if "task" not in data:
                print(f"  Warning: Missing 'task' field in {file_path}")
                return None

            return BenchmarkTask(
                id=data.get("id", file_path.stem),
                category=data.get("category", default_category),
                task=data["task"],
                validation=data.get("validation", {"type": "contains", "value": ""}),
                metadata=data.get("metadata", {}),
            )

        except json.JSONDecodeError as e:
            print(f"  Warning: Invalid JSON in {file_path}: {e}")
        except Exception as e:
            print(f"  Warning: Failed to load {file_path}: {e}")

        return None

    # ──────────────────────── Task Execution ────────────────────────

    def run_task(
        self,
        task: BenchmarkTask,
        provider: str,
        model: str,
    ) -> TaskResult:
        """
        Execute a single benchmark task.

        Args:
            task: Task to execute
            provider: LLM provider name
            model: Model identifier

        Returns:
            TaskResult with execution details
        """
        start_time = time.time()
        timestamp = datetime.now().isoformat()

        # Save original config
        import microharness.config as config_module
        original_provider = config_module.PROVIDER
        original_model = config_module.MAIN_MODEL

        try:
            # Override for this run
            config_module.PROVIDER = provider
            config_module.MAIN_MODEL = model

            # Reset token tracking
            token_stats.reset()

            # Execute agent
            actual_output, tool_calls, step_count = self._execute_agent(
                task.task, provider, model
            )

            # Score the result
            passed, score, validation_details = self.validator.score(
                task, actual_output, tool_calls
            )

            # Collect token statistics
            stats = token_stats.get_summary()
            duration_ms = int((time.time() - start_time) * 1000)

            # Build result
            return TaskResult(
                task_id=task.id,
                provider=provider,
                model=model,
                passed=passed,
                score=score,
                actual_output=actual_output[:MAX_OUTPUT_LENGTH],
                expected_output=task.validation.get("value", task.validation.get("pattern", "")),
                validation_details=validation_details,
                tokens_used=stats.get("total_tokens", 0),
                cost_usd=stats.get("total_cost_usd", 0.0),
                duration_ms=duration_ms,
                steps_used=step_count,
                tool_calls=[self._extract_tool_name(tc) for tc in tool_calls],
                timestamp=timestamp,
                error=None,
            )

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
                validation_details=f"Execution error: {type(e).__name__}: {e}",
                tokens_used=0,
                cost_usd=0.0,
                duration_ms=duration_ms,
                steps_used=0,
                tool_calls=[],
                timestamp=timestamp,
                error=str(e),
            )

        finally:
            # Restore original configuration
            config_module.PROVIDER = original_provider
            config_module.MAIN_MODEL = original_model

    def _execute_agent(self, task_description: str, provider: str, model: str) -> Tuple[str, List[dict], int]:
        """
        Execute the agent for a single task.

        Args:
            task_description: The task prompt
            provider: LLM provider name
            model: Model name

        Returns:
            Tuple of (final_output, tool_calls_list, steps_taken)
        """
        llm = get_llm(model).bind_tools(TOOLS)

        messages = [HumanMessage(content=task_description)]
        all_tool_calls = []
        step_count = 0

        for step in range(self.max_steps):
            system_message = SystemMessage(content=get_system_prompt())
            response = llm.invoke([system_message] + messages)

            # Track token usage
            self._record_token_usage(response, provider, model)

            messages.append(response)
            step_count += 1

            # Record tool calls
            if hasattr(response, "tool_calls") and response.tool_calls:
                all_tool_calls.extend(response.tool_calls)

            # Check if agent is done
            if not self._has_tool_calls(response):
                break

            # Execute tool calls
            for call in response.tool_calls:
                result_message = self._execute_tool_call(call)
                messages.append(result_message)

        # Extract final text response
        final_output = self._extract_final_response(messages)

        return final_output, all_tool_calls, step_count

    def _record_token_usage(self, response, provider: str, model: str) -> None:
        """Record token usage from an LLM response."""
        if not hasattr(response, "usage_metadata"):
            return

        usage = response.usage_metadata
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cost = get_cost(provider, model, input_tokens, output_tokens)

        token_stats.record(provider, model, input_tokens, output_tokens, cost)

    def _has_tool_calls(self, response) -> bool:
        """Check if response contains tool calls."""
        return hasattr(response, "tool_calls") and bool(response.tool_calls)

    def _execute_tool_call(self, call: dict) -> ToolMessage:
        """
        Execute a single tool call with Guard integration.

        In benchmark mode (auto_approve=True), ALWAYS_CONFIRM tools are auto-approved
        since there's no human in the loop. However, dangerous keyword detection
        through Guard is still enforced.

        Args:
            call: Tool call dictionary

        Returns:
            ToolMessage with execution result
        """
        tool_name = call.get("name", "")
        tool_args = call.get("args", {})

        # Guard check: determine if this tool needs approval
        needs_guard_check = should_confirm(tool_name, tool_args)

        if needs_guard_check:
            if self.auto_approve:
                # Benchmark mode: auto-approve but still check dangerous keywords
                # If Guard would block due to dangerous keywords, return error
                from microharness.agent.guard import is_dangerous
                if is_dangerous(tool_args):
                    return ToolMessage(
                        content=f"[BLOCKED BY GUARD] Dangerous content detected in {tool_name}: {tool_args}",
                        tool_call_id=call.get("id", ""),
                        name=tool_name,
                    )
            else:
                # Non-benchmark: block until human approves
                # (shouldn't happen in benchmark context, but handle gracefully)
                pass

        # Find matching tool
        tool = next((t for t in TOOLS if t.name == tool_name), None)

        if tool:
            try:
                result = tool.invoke(tool_args)
                content = str(result)
            except Exception as e:
                content = f"Error executing {tool_name}: {e}"
        else:
            content = f"Unknown tool: {tool_name}"

        return ToolMessage(
            content=content,
            tool_call_id=call.get("id", ""),
            name=tool_name,
        )

    @staticmethod
    def _extract_final_response(messages: List) -> str:
        """
        Extract the final text response from message history.

        Gets the last AI message with text content.

        Args:
            messages: List of LangChain messages

        Returns:
            Final response text
        """
        for message in reversed(messages):
            if (
                hasattr(message, "content")
                and isinstance(message.content, str)
                and message.content.strip()
                and not isinstance(message, ToolMessage)
            ):
                return message.content

        return ""

    @staticmethod
    def _extract_tool_name(tool_call: dict) -> str:
        """Extract tool name from various call formats."""
        if isinstance(tool_call, dict):
            return (
                tool_call.get("name")
                or tool_call.get("function", {}).get("name", "unknown")
            )
        return str(tool_call)

    # ──────────────────────── Benchmark Execution ────────────────────────

    def run_benchmark(
        self,
        category: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        benchmark_ids: Optional[List[str]] = None,
    ) -> BenchmarkResult:
        """
        Run a full benchmark suite.

        Args:
            category: Category filter
            provider: LLM provider (uses config default if None)
            model: Model name (uses config default if None)
            benchmark_ids: Specific task IDs to run

        Returns:
            BenchmarkResult with aggregated statistics
        """
        config = get_config()
        provider = provider or config.get("provider")
        model = model or config.get("main_model")

        # Load tasks
        tasks = self.load_benchmarks(category)
        if benchmark_ids:
            tasks = [t for t in tasks if t.id in benchmark_ids]

        if not tasks:
            return self._create_empty_result(category or "all", provider, model)

        # Print header
        self._print_benchmark_header(len(tasks), provider, model)

        # Execute all tasks
        task_results = []
        for i, task in enumerate(tasks, 1):
            result = self._run_and_report_task(i, len(tasks), task, provider, model)
            task_results.append(result)

        # Aggregate results
        return self._aggregate_results(
            category or "all", provider, model, task_results
        )

    def _run_and_report_task(
        self,
        index: int,
        total: int,
        task: BenchmarkTask,
        provider: str,
        model: str,
    ) -> dict:
        """Run a single task and print progress."""
        print(f"[{index}/{total}] {task.id} ({task.category})")
        print(f"       Task: {task.task[:80]}...")

        result = self.run_task(task, provider, model)
        result_dict = asdict(result)

        # Print result
        status = "✅ PASS" if result.passed else "❌ FAIL"
        print(
            f"       {status} | Score: {result.score:.2f} | "
            f"Tokens: {result.tokens_used} | Cost: ${result.cost_usd:.6f}"
        )

        if result.error:
            print(f"       Error: {result.error}")

        print()
        return result_dict

    def _aggregate_results(
        self,
        benchmark_id: str,
        provider: str,
        model: str,
        task_results: List[dict],
    ) -> BenchmarkResult:
        """Aggregate individual task results into benchmark summary."""
        total = len(task_results)
        passed = sum(1 for r in task_results if r["passed"])
        failed = total - passed
        pass_rate = passed / total if total > 0 else 0.0
        avg_score = sum(r["score"] for r in task_results) / total if total > 0 else 0.0

        total_tokens = sum(r["tokens_used"] for r in task_results)
        total_cost = sum(r["cost_usd"] for r in task_results)
        total_duration = sum(r["duration_ms"] for r in task_results)

        result = BenchmarkResult(
            benchmark_id=benchmark_id,
            provider=provider,
            model=model,
            run_id=f"run_{int(time.time())}",
            timestamp=datetime.now().isoformat(),
            tasks_total=total,
            tasks_passed=passed,
            tasks_failed=failed,
            pass_rate=pass_rate,
            avg_score=avg_score,
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            total_duration_ms=total_duration,
            task_results=task_results,
            metadata={"max_steps": self.max_steps},
        )

        # Save to disk
        self._save_result(result)

        return result

    def _save_result(self, result: BenchmarkResult) -> Path:
        """Save benchmark result to JSON file."""
        filename = f"{result.run_id}_{result.provider}_{result.model}.json"
        result_file = self.results_dir / filename

        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(asdict(result), f, ensure_ascii=False, indent=2)

        print(f"Results saved to: {result_file}")
        return result_file

    def _create_empty_result(
        self,
        benchmark_id: str,
        provider: str,
        model: str,
    ) -> BenchmarkResult:
        """Create an empty result when no tasks are found."""
        print(f"No tasks found for benchmark: {benchmark_id}")
        return BenchmarkResult(
            benchmark_id=benchmark_id,
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

    # ──────────────────────── Multi-Provider Comparison ────────────────────────

    def compare_providers(
        self,
        providers: List[str],
        model: str,
        category: Optional[str] = None,
    ) -> List[BenchmarkResult]:
        """
        Run the same benchmark across multiple providers for comparison.

        Args:
            providers: List of provider names to compare
            model: Model name (same model across providers)
            category: Optional category filter

        Returns:
            List of BenchmarkResult objects (one per provider)
        """
        results = []

        for provider in providers:
            print(f"\n{'#' * 60}")
            print(f"# Provider: {provider}")
            print(f"{'#' * 60}")

            result = self.run_benchmark(
                category=category,
                provider=provider,
                model=model,
            )
            results.append(result)

        # Print comparison table
        self._print_comparison_table(results)

        return results

    def _print_benchmark_header(self, task_count: int, provider: str, model: str) -> None:
        """Print benchmark run header."""
        print(f"\n{'=' * 60}")
        print(f"  Running {task_count} benchmark tasks")
        print(f"  Provider: {provider} | Model: {model}")
        print(f"{'=' * 60}\n")

    def _print_comparison_table(self, results: List[BenchmarkResult]) -> None:
        """Print provider comparison table."""
        print(f"\n{'=' * 70}")
        print(f"  COMPARISON RESULTS")
        print(f"{'=' * 70}")
        print(
            f"{'Provider':<15} {'Pass Rate':<12} {'Avg Score':<10} "
            f"{'Total Cost':<14} {'Tokens':<10}"
        )
        print(f"{'-' * 70}")

        for result in results:
            print(
                f"{result.provider:<15} {result.pass_rate * 100:>9.1f}%  "
                f"{result.avg_score:>8.3f}   ${result.total_cost_usd:>11.6f}  "
                f"{result.total_tokens:>8,}"
            )

        print(f"{'=' * 70}\n")


# ───────────────────────── Utility Functions ────────────────────────

def print_benchmark_result(result: BenchmarkResult) -> None:
    """
    Pretty-print a benchmark result.

    Args:
        result: BenchmarkResult to display
    """
    print(f"\n{'=' * 60}")
    print(f"  BENCHMARK RESULT: {result.benchmark_id}")
    print(f"{'=' * 60}")
    print(f"  Provider/Model: {result.provider} / {result.model}")
    print(f"  Run ID: {result.run_id}")
    print(f"  Timestamp: {result.timestamp}")
    print(f"{'-' * 60}")
    print(f"  Tasks: {result.tasks_passed}/{result.tasks_total} passed "
          f"({result.pass_rate * 100:.1f}%)")
    print(f"  Avg Score: {result.avg_score:.3f}")
    print(f"  Total Tokens: {result.total_tokens:,}")
    print(f"  Total Cost: ${result.total_cost_usd:.6f}")
    print(f"  Duration: {result.total_duration_ms / 1000:.1f}s")

    if result.tasks_failed > 0:
        print(f"  Failed Tasks:")
        for task_result in result.task_results:
            if not task_result["passed"]:
                print(f"    - {task_result['task_id']}: "
                      f"{task_result['validation_details'][:100]}")

    print(f"{'=' * 60}")