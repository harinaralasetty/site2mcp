"""Official SDK adapter; also copied beside the standalone generated runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

try:
    from .runtime import Client, SiteError
except ImportError:
    from runtime import Client, SiteError


async def run(manifest, *, allow_writes=False, rate=2.0, timeout=15.0):
    client = Client(manifest, allow_writes=allow_writes, rate=rate, timeout=timeout)
    server = Server(client.manifest["name"], version="0.1.0")

    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name=op["name"],
                description=op["description"],
                inputSchema=op["inputSchema"],
                annotations=ToolAnnotations(
                    readOnlyHint=op["readOnly"],
                    destructiveHint=not op["readOnly"],
                    openWorldHint=True,
                ),
            )
            for op in client.tools()
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            result = await asyncio.to_thread(client.call, name, **(arguments or {}))
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        except SiteError as exc:
            # The SDK sets isError for raised exceptions. Its wrapper only receives
            # our sanitized message, never the raw HTTP error or credential URL.
            raise ValueError(str(exc)) from None

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser(description="Run generated site2mcp tools over stdio")
    parser.add_argument("--allow-writes", action="store_true", help="Expose tools that change data")
    parser.add_argument("--rate", type=float, default=2.0, help="Maximum requests per second")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds")
    args = parser.parse_args()
    asyncio.run(run(Path(__file__).with_name("manifest.json"), **vars(args)))


if __name__ == "__main__":
    main()
