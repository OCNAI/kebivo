# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic-ai-slim[mcp,openai]>=1.0"]
# ///
"""A Pydantic AI agent that reports unanswered questions to Kebivo over MCP.

Usage:
    KEBIVO_TOKEN=<project token> uv run agent.py --list-tools
    KEBIVO_TOKEN=<project token> OPENAI_API_KEY=sk-... \
        uv run agent.py "How do I reset my corporate VPN?"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset

INSTRUCTIONS = """You answer questions about company IT and HR policies.
You have no documentation available in this example, so you cannot answer
reliably. When you cannot answer, call report_knowledge_gap once with the
user's question (without personal data) and a short context, then tell the
user that you could not find a reliable answer and that the gap was reported."""


def make_toolset(token: str) -> MCPToolset:
    base_url = os.environ.get("KEBIVO_URL", "http://127.0.0.1:8000").rstrip("/")
    return MCPToolset(
        f"{base_url}/mcp",
        headers={"Authorization": f"Bearer {token}"},
    )


async def main(question: str | None) -> None:
    token = os.environ.get("KEBIVO_TOKEN")
    if not token:
        sys.exit("Set KEBIVO_TOKEN to a Kebivo project token")

    toolset = make_toolset(token)
    tools = await toolset.list_tools()
    if question is None:
        print(json.dumps({"tools": [tool.name for tool in tools]}))
        return

    model = f"openai:{os.environ.get('MODEL', 'gpt-5-mini')}"
    agent = Agent(model, toolsets=[toolset], instructions=INSTRUCTIONS)
    result = await agent.run(question)
    print(result.output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question", nargs="?", help="Question for the agent")
    parser.add_argument("--list-tools", action="store_true", help="Only list Kebivo tools")
    args = parser.parse_args()
    if args.question is None and not args.list_tools:
        parser.error("Give a question or --list-tools")
    asyncio.run(main(None if args.list_tools else args.question))
