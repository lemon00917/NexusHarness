"""
Agent Scheduler
===============
LLM query planner + execution engine for complex medical queries.

Simple queries → existing pipeline (unchanged)
Complex queries (temporal, cross-source) → LLM-generated execution plan

Usage:
    from microharness.agent.scheduler import QueryPlanner, ExecutionEngine

    planner = QueryPlanner(model="deepseek-r1:1.5b")
    judgment = planner.judge_complexity("手术后24小时内开了阿司匹林")
    # → {"complexity": "COMPLEX", ...}
"""

from microharness.agent.scheduler.planner import QueryPlanner
from microharness.agent.scheduler.executor import ExecutionEngine

__all__ = ["QueryPlanner", "ExecutionEngine"]
