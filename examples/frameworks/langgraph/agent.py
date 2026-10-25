# /// script
# requires-python = ">=3.10"
# dependencies = ["langchain>=1.4", "langchain-mcp-adapters>=0.3", "langchain-openai>=1.7"]
# ///
"""A LangGraph agent (LangChain create_agent) that reports knowledge gaps to Kebivo over MCP.

Usage:
    KEBIVO_URL=http://127.0.0.1:8000 KEBIVO_TOKEN=<project token> \
    OPENAI_API_KEY=sk-... uv run agent.py "How do I reset my corporate VPN?"

    # Any OpenAI-compatible server, for example Ollama:
    OPENAI_BASE_URL=http://127.0.0.1:11434/v1 OPENAI_API_KEY=ollama MODEL=qwen2.5:3b \
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

from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

INSTRUCTIONS = """You answer questions about company IT and HR policies.
You have no documentation available in this example, so you cannot answer
reliably. When you cannot answer, call report_knowledge_gap once with the
user's question (without personal data) and a short context, then tell the
user that you could not find a reliable answer and that the gap was reported."""


async def main(question: str | None) -> None:
    token = os.environ.get("KEBIVO_TOKEN")
    if not token:
        sys.exit("Set KEBIVO_TOKEN to a Kebivo project token")
    client = MultiServerMCPClient(
        {
            "kebivo": {
                "transport": "streamable_http",
                "url": os.environ.get("KEBIVO_URL", "http://127.0.0.1:8000").rstrip("/") + "/mcp",
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    )
    tools = await client.get_tools()
    if question is None:
        print(json.dumps({"tools": [tool.name for tool in tools]}))
        return
    # ChatOpenAI reads OPENAI_API_KEY and OPENAI_BASE_URL from the environment.
    model = ChatOpenAI(model=os.environ.get("MODEL", "gpt-5-mini"))
    agent = create_agent(model, tools, system_prompt=INSTRUCTIONS)
    result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})
    calls = [
        call["name"]
        for message in result["messages"]
        for call in getattr(message, "tool_calls", [])
    ]
    print(json.dumps({"tool_calls": calls, "answer": result["messages"][-1].content}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question", nargs="?", help="Question for the agent")
    parser.add_argument("--list-tools", action="store_true", help="Only list Kebivo tools")
    args = parser.parse_args()
    if args.question is None and not args.list_tools:
        parser.error("Give a question or --list-tools")
    asyncio.run(main(None if args.list_tools else args.question))
