# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "llama-index-core>=0.14",
#   "llama-index-tools-mcp>=0.3",
#   "llama-index-llms-openai>=0.6",
# ]
# ///
"""A LlamaIndex agent that reports unanswered questions to Kebivo over MCP.

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

from llama_index.core.agent.workflow import AgentWorkflow
from llama_index.llms.openai import OpenAI
from llama_index.tools.mcp import BasicMCPClient, McpToolSpec

INSTRUCTIONS = """You answer questions about company IT and HR policies.
You have no documentation available in this example, so you cannot answer
reliably. When you cannot answer, call report_knowledge_gap once with the
user's question (without personal data) and a short context, then tell the
user that you could not find a reliable answer and that the gap was reported."""


def make_client(token: str) -> BasicMCPClient:
    base_url = os.environ.get("KEBIVO_URL", "http://127.0.0.1:8000").rstrip("/")
    return BasicMCPClient(
        f"{base_url}/mcp",
        headers={"Authorization": f"Bearer {token}"},
    )


async def main(question: str | None) -> None:
    token = os.environ.get("KEBIVO_TOKEN")
    if not token:
        sys.exit("Set KEBIVO_TOKEN to a Kebivo project token")

    client = make_client(token)
    tool_spec = McpToolSpec(client=client)
    tools = await tool_spec.to_tool_list_async()
    if question is None:
        print(json.dumps({"tools": [tool.metadata.name for tool in tools]}))
        return

    model = OpenAI(
        model=os.environ.get("MODEL", "gpt-5-mini"),
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        api_base=os.environ.get("OPENAI_BASE_URL"),
    )
    agent = AgentWorkflow.from_tools_or_functions(
        tools,
        llm=model,
        system_prompt=INSTRUCTIONS,
    )
    result = await agent.run(user_msg=question)
    print(str(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question", nargs="?", help="Question for the agent")
    parser.add_argument("--list-tools", action="store_true", help="Only list Kebivo tools")
    args = parser.parse_args()
    if args.question is None and not args.list_tools:
        parser.error("Give a question or --list-tools")
    asyncio.run(main(None if args.list_tools else args.question))
