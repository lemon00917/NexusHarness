"""
NexusHarness — Main Entry Point
================================
基于 LangGraph 的最小可运行 Agent Harness demo。

配置：编辑 .env 文件即可，无需改动代码。
支持多 Provider：anthropic / openai / deepseek / kimi / minimax / qwen / glm

架构层级：
  [User Input]
       ↓
  [agent_node]   ← 提示管理（含长期记忆注入）+ 模型推理
       ↓
  [guard_node]   ← 安全守卫（写操作/高危操作拦截）
       ↓
  [tool_node]    ← 工具执行
       ↓
  [agent_node]   ← 继续推理（循环，直到任务完成或达到上限）
       ↓
  [memory]       ← 长期记忆提炼 + 持久化写入 memory.json

用法：
  python harness.py
"""

from typing import Annotated

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from typing_extensions import TypedDict

from microharness.config.config import get_llm, MAIN_MODEL, MAX_STEPS, PROVIDER, MEMORY_MODEL, validate as config_validate
from microharness.agent.guard import should_confirm, request_human_approval
from microharness.memory.memory import extract_and_save_memory, load_memories
from microharness.config.prompts import get_system_prompt
from microharness.observability.token_tracker import token_stats, get_cost
from microharness.agent.tools import TOOLS

llm = get_llm(MAIN_MODEL).bind_tools(TOOLS)


# ──────────────────────────────────────────────────
# State 定义
# ──────────────────────────────────────────────────

class HarnessState(TypedDict):
    messages: Annotated[list, add_messages]
    step_count: int
    approved: bool


# ──────────────────────────────────────────────────
# Nodes
# ──────────────────────────────────────────────────

def agent_node(state: HarnessState) -> dict:
    """模型推理节点：注入系统提示（含长期记忆）→ 推理 → step_count +1"""
    system = SystemMessage(content=get_system_prompt())
    messages = [system] + state["messages"]

    print(f"\n[HARNESS] Step {state['step_count'] + 1}/{MAX_STEPS} — Agent thinking...")
    response = llm.invoke(messages)

    # Record token usage
    if hasattr(response, "usage_metadata"):
        usage = response.usage_metadata
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cost = get_cost(PROVIDER, MAIN_MODEL, input_tokens, output_tokens)
        token_stats.record(PROVIDER, MAIN_MODEL, input_tokens, output_tokens, cost)
        print(f"  [TOKENS] input={input_tokens} output={output_tokens} cost=${cost:.6f}")

    # Print tool calls if any
    if hasattr(response, "tool_calls") and response.tool_calls:
        for call in response.tool_calls:
            try:
                args_str = str(call['args']).encode('utf-8', errors='replace').decode('utf-8', errors='replace')
                print(f"  [TOOL CALL] {call['name']}: {args_str}")
            except Exception:
                print(f"  [TOOL CALL] {call['name']}: (args not printable)")

    return {
        "messages": [response],
        "step_count": state["step_count"] + 1,
    }


def guard_node(state: HarnessState) -> dict:
    """安全守卫节点：检查工具调用，写/删操作请求人工确认"""
    last = state["messages"][-1]
    approved = True

    if hasattr(last, "tool_calls") and last.tool_calls:
        for call in last.tool_calls:
            tool_name = call["name"]
            tool_args = call["args"]
            print(f"  [GUARD] Checking tool: {tool_name}")
            if should_confirm(tool_name, tool_args):
                print(f"  [GUARD] Requesting approval for: {tool_name}")
                approved = request_human_approval(tool_name, tool_args)
                if not approved:
                    print(f"  [GUARD] Rejected: {tool_name}")
                    break

    return {"approved": approved}


tool_node = ToolNode(TOOLS)


# ──────────────────────────────────────────────────
# 路由函数
# ──────────────────────────────────────────────────

def route_after_agent(state: HarnessState) -> str:
    if state["step_count"] >= MAX_STEPS:
        print(f"\n[HARNESS] ⚠️  Max steps ({MAX_STEPS}) reached. Stopping.")
        return END
    last = state["messages"][-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "guard"
    return END


def route_after_guard(state: HarnessState) -> str:
    return "tools" if state["approved"] else END


# ──────────────────────────────────────────────────
# 构建图
# ──────────────────────────────────────────────────

def build_harness():
    graph = StateGraph(HarnessState)
    graph.add_node("agent", agent_node)
    graph.add_node("guard", guard_node)
    graph.add_node("tools", tool_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", route_after_agent)
    graph.add_conditional_edges("guard", route_after_guard)
    graph.add_edge("tools", "agent")
    return graph.compile()


# ──────────────────────────────────────────────────
# 主程序
# ──────────────────────────────────────────────────

def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

    # Check for 'web' subcommand
    if len(sys.argv) > 1 and sys.argv[1] == "web":
        import uvicorn
        from web.app import app
        print("Starting NexusHarness web server on http://localhost:8000")
        uvicorn.run(app, host="0.0.0.0", port=8000)
        return

    # Check for 'skill' subcommand
    if len(sys.argv) > 1 and sys.argv[1] == "skill":
        from .skill_cli import main as skill_main
        skill_main()
        return

    # Check for 'benchmark' subcommand
    if len(sys.argv) > 1 and sys.argv[1] == "benchmark":
        import argparse
        from .evaluation import BenchmarkRunner, print_benchmark_result

        parser = argparse.ArgumentParser(description="Run NexusHarness benchmarks")
        parser.add_argument("--category", help="Filter by category (code/tool_use/reasoning/regression)")
        parser.add_argument("--providers", nargs="+", help="Providers to compare")
        parser.add_argument("--model", default=None, help="Model to test (default: current MAIN_MODEL)")
        parser.add_argument("--tasks", nargs="+", help="Specific task IDs to run")
        parser.add_argument("--output", default="benchmark_results", help="Output directory")
        args = parser.parse_args(sys.argv[2:])

        runner = BenchmarkRunner(results_dir=args.output)

        if args.providers:
            model = args.model or MAIN_MODEL
            results = runner.compare_providers(args.providers, model, args.category)
        else:
            result = runner.run_benchmark(
                category=args.category,
                provider=PROVIDER,
                model=args.model or MAIN_MODEL,
                benchmark_ids=args.tasks,
            )
            print_benchmark_result(result)
        return

    # Check for 'rag' subcommand
    if len(sys.argv) > 1 and sys.argv[1] == "rag":
        import argparse
        from .rag import rag

        parser = argparse.ArgumentParser(description="NexusHarness RAG knowledge base")
        subparsers = parser.add_subparsers(dest="action", help="RAG actions")

        sp = subparsers.add_parser("index", help="Index documents from a directory")
        sp.add_argument("--dir", default="documents", help="Directory to index")

        sp = subparsers.add_parser("search", help="Search the knowledge base")
        sp.add_argument("query", help="Search query")
        sp.add_argument("--top-k", type=int, default=3, help="Number of results")

        sp = subparsers.add_parser("list", help="List indexed documents")

        args = parser.parse_args(sys.argv[2:])

        rag.load_index()

        if args.action == "index":
            rag.load_documents_from_dir(args.dir)
            rag.save_index()
            docs = rag.list_documents()
            print(f"Indexed {len(docs)} document(s) from {args.dir}")
        elif args.action == "search":
            results = rag.similarity_search(args.query, args.top_k)
            if not results:
                print("No results found.")
            else:
                print(f"=== Search results for: {args.query} ===\n")
                for i, doc in enumerate(results, 1):
                    preview = doc.content[:200] + "..." if len(doc.content) > 200 else doc.content
                    print(f"【{i}】 {doc.filename}")
                    print(f"    {preview}\n")
        elif args.action == "list":
            docs = rag.list_documents()
            if not docs:
                print("Knowledge base is empty.")
            else:
                print(f"=== Indexed documents ({len(docs)}) ===\n")
                for doc in docs:
                    print(f"• {doc['filename']} (ID: {doc['doc_id']})")
        else:
            parser.print_help()
        return

    config_validate()

    # Load skills and register their safety levels
    from microharness.skills.skill_manager import load_skills, get_skill_safety_map
    from microharness.agent.guard import register_skill_safety_levels

    load_skills()
    register_skill_safety_levels(get_skill_safety_map())

    print("=" * 55)
    print("  NexusHarness  —  LangGraph + Claude")
    print(f"  Provider    : {PROVIDER}")
    print(f"  Main Model  : {MAIN_MODEL}")
    print(f"  Memory Model: {MEMORY_MODEL}")
    print(f"  Max Steps   : {MAX_STEPS}")
    print(f"  Sandbox     : /tmp/sandbox/")
    print("=" * 55)

    existing = load_memories()
    if existing:
        print(f"\n[HARNESS] Found {len(existing)} long-term memory record(s).")
        print(f"          Last: {existing[-1]['date']} — {existing[-1]['summary'][:60]}...")
    else:
        print("\n[HARNESS] No long-term memory found. Starting fresh.")

    harness = build_harness()

    print("\nType your task below. Examples:")
    print("  - Write a Python script that prints Fibonacci numbers up to 100, then run it")
    print("  - Improve the script from last time")
    print()

    user_input = input("Task: ").strip()
    if not user_input:
        print("No task provided. Exiting.")
        return

    init_state: HarnessState = {
        "messages": [HumanMessage(content=user_input)],
        "step_count": 0,
        "approved": True,
    }

    print("\n[HARNESS] Starting...\n")
    final_state = harness.invoke(init_state)

    final_messages = final_state["messages"]
    final_response = next(
        (m for m in reversed(final_messages)
         if hasattr(m, "content") and isinstance(m.content, str) and m.content.strip()),
        None
    )

    print("\n" + "=" * 55)
    print("  FINAL RESPONSE")
    print("=" * 55)
    print(final_response.content if final_response else "(Task completed — see tool outputs above)")
    print("=" * 55)
    print(f"  Total steps used: {final_state['step_count']}/{MAX_STEPS}")

    # Print token stats summary
    stats = token_stats.get_summary()
    if stats["total_calls"] > 0:
        print("=" * 55)
        print("  TOKEN STATS")
        print("=" * 55)
        print(f"  LLM calls     : {stats['total_calls']}")
        print(f"  Input tokens  : {stats['total_input_tokens']:,}")
        print(f"  Output tokens : {stats['total_output_tokens']:,}")
        print(f"  Total tokens  : {stats['total_tokens']:,}")
        print(f"  Total cost    : ${stats['total_cost_usd']:.6f}")
    print("=" * 55)

    print("\n[HARNESS] Extracting long-term memory...")
    summary = extract_and_save_memory(final_state["messages"], user_input)
    print(f"[HARNESS] Memory saved: {summary}\n")


if __name__ == "__main__":
    main()
