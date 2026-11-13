"""Agent turn benchmark: 3 scenarios over a fake LLM (no network).

Run:  python benchmarks/agent_bench.py
Reports LLM steps, tool calls, reply size, estimated tokens and wall time per scenario.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from rbit.agent import AgentEngine  # noqa: E402
from rbit.models import ChatMessage, Session, ToolCallRecord, short_id  # noqa: E402
from rbit.tools import ToolExecutor  # noqa: E402


def _msg(content: str, **extra) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content, **extra}}]})


SCENARIOS = {
    "single_qa": {
        "prompt": "What does this repo do? Answer in one sentence.",
        "steps": [_msg("This repo is rbit, a terminal AI coding agent.")],
    },
    "read_then_answer": {
        "prompt": "Read README.md and summarize it.",
        "steps": [
            _msg(
                "",
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "read", "arguments": '{"filePath": "README.md"}'},
                    }
                ],
            ),
            _msg("README describes rbit: install, /connect, then ask tasks in the TUI."),
        ],
    },
    "edit_round": {
        "prompt": "Fix the typo in docs file.",
        "steps": [
            _msg(
                "",
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "grep", "arguments": '{"pattern": "teh "}'},
                    }
                ],
            ),
            _msg(
                "",
                tool_calls=[
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {
                            "name": "edit",
                            "arguments": json.dumps(
                                {"filePath": "README.md", "oldString": "teh ", "newString": "the "}
                            ),
                        },
                    }
                ],
            ),
            _msg("Typo fixed."),
        ],
    },
}


async def run_scenario(name: str, spec: dict) -> dict:
    ws = Path(tempfile.mkdtemp(prefix=f"rbit-bench-{name}-"))
    (ws / "README.md").write_text("rbit readme teh typo\n", encoding="utf-8")
    executor = ToolExecutor(workspace=ws, session=Session())
    cfg: dict = {"api_key": "bench", "max_steps": 50}
    engine = AgentEngine(workspace=ws, executor=executor, get_config=lambda: cfg)
    step_state = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        i = step_state["n"]
        step_state["n"] += 1
        return spec["steps"][min(i, len(spec["steps"]) - 1)]

    engine.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = Session()
    user = ChatMessage(id=short_id(), role="user", content=spec["prompt"])
    session.messages.append(user)

    async def _noop(_rec: ToolCallRecord) -> None:
        return None

    async def _noop_s(_s: str) -> None:
        return None

    t0 = time.perf_counter()
    reply = await engine.run_turn(
        session=session,
        user_msg=user,
        agent_mode="build",
        model_id="openai/gpt-4.1",
        on_tool_start=_noop,
        on_tool_end=_noop,
        on_status=_noop_s,
    )
    dt = time.perf_counter() - t0
    await engine.http_client.aclose()
    return {
        "scenario": name,
        "llm_steps": step_state["n"],
        "tool_calls": len(reply.tool_calls),
        "reply_chars": len(reply.content),
        "tokens_est": reply.tokens,
        "seconds": round(dt, 3),
    }


async def main() -> None:
    print(f"{'scenario':<18} {'steps':>5} {'tools':>5} {'chars':>6} {'tokens':>7} {'sec':>6}")
    for name, spec in SCENARIOS.items():
        row = await run_scenario(name, spec)
        print(
            f"{row['scenario']:<18} {row['llm_steps']:>5} {row['tool_calls']:>5} "
            f"{row['reply_chars']:>6} {row['tokens_est']:>7} {row['seconds']:>6}"
        )


if __name__ == "__main__":
    asyncio.run(main())
