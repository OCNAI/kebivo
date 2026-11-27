"""Connect to the local MVP MCP server with the official Python MCP SDK.

Run with PROJECT_TOKEN in the environment. The optional question submits one
knowledge gap; without it, the client only lists the tools this token can use.
"""

import argparse
import asyncio
import json
import os
import sys

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def connect(
    url: str, token: str, question: str | None, failed_status: list[tuple[int, str | None]]
) -> None:
    async def record_status(response: httpx2.Response) -> None:
        if response.status_code >= 400:
            await response.aread()
            try:
                detail = response.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                detail = None
            failed_status.append(
                (response.status_code, detail if isinstance(detail, str) else None)
            )

    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, event_hooks={"response": [record_status]}
    ) as http:
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
    endpoint = args.url.rstrip("/") + "/mcp"
    failed_status: list[tuple[int, str | None]] = []
    try:
        asyncio.run(connect(args.url, token, args.question, failed_status))
    except (ExceptionGroup, httpx2.HTTPError) as exc:
        pending = [exc]
        errors = []
        while pending:
            current = pending.pop()
            if isinstance(current, ExceptionGroup):
                pending.extend(current.exceptions)
            else:
                errors.append(current)
        status_error = next((e for e in errors if isinstance(e, httpx2.HTTPStatusError)), None)
        if status_error is not None or failed_status:
            status = (
                status_error.response.status_code
                if status_error is not None
                else failed_status[-1][0]
            )
            message = f"{status} from {endpoint}"
            if status == 401 and failed_status and failed_status[-1][1]:
                detail = failed_status[-1][1].replace(token, "[redacted]")
                message += f": {''.join(char for char in detail if char.isprintable())}"
        elif any(isinstance(e, httpx2.ConnectError | httpx2.ConnectTimeout) for e in errors):
            message = f"Could not connect to {endpoint}: is Kebivo running?"
        else:
            raise
        print(message, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
