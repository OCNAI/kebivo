"""Connect to the local MVP MCP server with the official Python MCP SDK.

Run with PROJECT_TOKEN in the environment. The optional question submits one
knowledge gap; without it, the client only lists the tools this token can use.
"""

import argparse
import asyncio
import json
import os

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def connect(url: str, token: str, question: str | None) -> None:
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(
            url.rstrip("/") + "/mcp", http_client=http, terminate_on_close=False
        ) as streams:
            async with ClientSession(*streams[:2]) as session:
                await session.discover()
                tools = await session.list_tools()
                print(json.dumps({"tools": [tool.name for tool in tools.tools]}))
                if question is not None:
                    result = await session.call_tool("report_knowledge_gap", {"question": question})
                    print(json.dumps(result.structured_content))
                    if result.is_error:
                        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="Server origin")
    parser.add_argument("--question", help="Submit a knowledge gap after listing tools")
    args = parser.parse_args()
    token = os.environ.get("PROJECT_TOKEN")
    if not token:
        parser.error("Set PROJECT_TOKEN to a project bearer token")
    asyncio.run(connect(args.url, token, args.question))


if __name__ == "__main__":
    main()
