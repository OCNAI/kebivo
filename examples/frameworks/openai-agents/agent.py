# /// script
# requires-python = ">=3.10"
# dependencies = ["openai-agents>=0.24"]
# ///
"""An OpenAI Agents SDK agent that reports knowledge gaps to Kebivo over MCP.

Usage:
    KEBIVO_URL=http://127.0.0.1:8000 KEBIVO_TOKEN=<project token> \
    OPENAI_API_KEY=sk-... uv run agent.py "How do I reset my corporate VPN?"

    # Any OpenAI-compatible server, for example Ollama:
    OPENAI_BASE_URL=http://127.0.0.1:11434/v1 OPENAI_API_KEY=ollama MODEL=qwen2.5:1.5b \
        KEBIVO_TOKEN=<project token> uv run agent.py "How do I reset my corporate VPN?"

    # Check the connection without a model:
    KEBIVO_TOKEN=<project token> uv run agent.py --list-tools
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from agents import (
    Agent,
    AsyncOpenAI,
    OpenAIChatCompletionsModel,
    Runner,
    set_tracing_disabled,
)
from agents.mcp import MCPServerStreamableHttp

INSTRUCTIONS = """You answer questions about company IT and HR policies.
You have no documentation available in this example, so you cannot answer
reliably. When you cannot answer, call report_knowledge_gap once with the
user's question (without personal data) and a short context, then tell the
user that you could not find a reliable answer and that the gap was reported."""


def model() -> str | OpenAIChatCompletionsModel:
    name = os.environ.get("MODEL", "gpt-5-mini")
    base_url = os.environ.get("OPENAI_BASE_URL")
    if not base_url:
        return name
    # Most OpenAI-compatible servers implement Chat Completions, not Responses.
    set_tracing_disabled(True)
    client = AsyncOpenAI(base_url=base_url, api_key=os.environ.get("OPENAI_API_KEY", "unused"))
    return OpenAIChatCompletionsModel(model=name, openai_client=client)


async def main(question: str | None) -> None:
    url = os.environ.get("KEBIVO_URL", "http://127.0.0.1:8000").rstrip("/") + "/mcp"
    token = os.environ.get("KEBIVO_TOKEN")
    if not token:
        sys.exit("Set KEBIVO_TOKEN to a Kebivo project token")
    async with MCPServerStreamableHttp(
        name="kebivo",
        params={"url": url, "headers": {"Authorization": f"Bearer {token}"}, "timeout": 10},
        cache_tools_list=True,
        max_retry_attempts=2,
    ) as kebivo:
        if question is None:
            tools = await kebivo.list_tools()
            print(json.dumps({"tools": [tool.name for tool in tools]}))
            return
        agent = Agent(
            name="Policy assistant",
            instructions=INSTRUCTIONS,
            mcp_servers=[kebivo],
            model=model(),
        )
        result = await Runner.run(agent, question)
        calls = [
            item.raw_item.name
            for item in result.new_items
            if item.type == "tool_call_item" and hasattr(item.raw_item, "name")
        ]
        print(json.dumps({"tool_calls": calls, "answer": result.final_output}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question", nargs="?", help="Question for the agent")
    parser.add_argument("--list-tools", action="store_true", help="Only list Kebivo tools")
    args = parser.parse_args()
    if args.question is None and not args.list_tools:
        parser.error("Give a question or --list-tools")
    asyncio.run(main(None if args.list_tools else args.question))
