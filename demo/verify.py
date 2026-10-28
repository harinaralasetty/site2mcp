"""Offline end-to-end acceptance check using the real MCP SDK stdio client."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openapi_spec_validator import validate

from demo.app import NotesServer
from demo.record import record

ROOT = Path(__file__).resolve().parents[1]


def cli(*args):
    result = subprocess.run(
        [sys.executable, "-m", "site2mcp", *map(str, args)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout.rstrip()


def decoded(result):
    assert not result.isError, result.content
    return json.loads(result.content[0].text)


async def verify(directory, *, verbose=True):
    def log(message):
        if verbose:
            print(message, flush=True)

    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "out"
    with NotesServer() as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            recorder = record(server.origin, directory / "session.har")
            log(f"Recorded {len(recorder.entries)} requests against loopback demo")
            log(cli("learn", directory / "session.har", "-o", output, "--name", "Notes demo"))
            manifest = json.loads((output / "manifest.json").read_text())
            validate(json.loads((output / "openapi.yaml").read_text()))
            log("OpenAPI 3.1 validation: PASS")
            secrets = [
                server.sid,
                server.csrf,
                server.password,
                "demo.private@example.test",
                "Local Demo User",
            ]
            for path in output.rglob("*"):
                if path.is_file():
                    data = path.read_bytes()
                    for secret in secrets:
                        assert secret.encode() not in data, f"Credential leak in {path.name}"
            log("All generated artifacts: no recorded credentials or personal values")
            assert not (output / ".env").exists()
            environment = dict(
                {k: v for k, v in os.environ.items() if not k.startswith("SITE2MCP_")},
                SITE2MCP_COOKIE=f"sid={server.sid}; csrf={server.csrf}",
                SITE2MCP_X_CSRF_TOKEN=server.csrf,
                PYTHONPATH="",
            )
            args = ["-m", "site2mcp", "serve", str(output)]
            params = StdioServerParameters(command=sys.executable, args=args, env=environment)
            async with stdio_client(params) as (read, write):
                async with ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=15)
                ) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    actual = {tool.name for tool in tools}
                    expected = {op["name"] for op in manifest["operations"] if op["readOnly"]}
                    assert actual == expected
                    log("Default MCP tools: " + ", ".join(sorted(actual)))
                    notes = decoded(
                        await session.call_tool("list_notes", {"query": {"page": 1, "limit": 2}})
                    )
                    assert len(notes["notes"]) == 2
                    note = decoded(await session.call_tool("get_note", {"path": {"noteId": "1"}}))
                    assert note["id"] == 1
                    tags = decoded(await session.call_tool("list_tags", {}))
                    assert len(tags["tags"]) == 2
                    tag = decoded(
                        await session.call_tool(
                            "get_tag", {"path": {"tagId": "550e8400-e29b-41d4-a716-446655440000"}}
                        )
                    )
                    assert tag["label"] == "home"
                    gql = decoded(
                        await session.call_tool("graphql_list_notes", {"variables": {"limit": 2}})
                    )
                    assert len(gql["data"]["notes"]) == 2
                    hidden = await session.call_tool(
                        "create_note", {"body": {"title": "should not write"}}
                    )
                    assert hidden.isError
                    log("Real MCP read calls: 5 passed; hidden write call rejected")
            # Launch the generated script directly, with no site2mcp imports.
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(output / "server.py"), "--allow-writes"],
                env=environment,
                cwd=directory,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=15)
                ) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    actual = {tool.name for tool in tools}
                    assert actual == {op["name"] for op in manifest["operations"]}
                    write_tools = [tool for tool in tools if not tool.annotations.readOnlyHint]
                    assert all("Changes data." in tool.description for tool in write_tools)
                    created = decoded(
                        await session.call_tool(
                            "create_note", {"body": {"title": "Written through MCP", "tag": "work"}}
                        )
                    )
                    assert server.notes[created["id"]]["title"] == "Written through MCP"
                    gql = decoded(
                        await session.call_tool(
                            "graphql_add_note", {"variables": {"title": "MCP GraphQL write"}}
                        )
                    )
                    assert (
                        server.notes[gql["data"]["addNote"]["id"]]["title"] == "MCP GraphQL write"
                    )
                    log(
                        f"--allow-writes: {len(write_tools)} write tools exposed; "
                        "REST and GraphQL writes passed"
                    )
            # Test the generated typed client as a separate consumer of the runtime.
            script = (
                "import sys; sys.path.insert(0, sys.argv[1]); from client import Client; "
                'c=Client(); note=c.get_note(path={"noteId":"1"}); '
                'assert note["id"]==1; print("Generated typed client: PASS")'
            )
            result = subprocess.run(
                [sys.executable, "-c", script, str(output)],
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            log(result.stdout.strip())
            environment["SITE2MCP_COOKIE"] = "sid=expired-local-session"
            params = StdioServerParameters(
                command=sys.executable, args=[str(output / "server.py")], env=environment
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=15)
                ) as session:
                    await session.initialize()
                    expired = await session.call_tool("list_tags", {})
                    assert expired.isError
                    assert "re-record or refresh your cookie" in expired.content[0].text
                    log("Expired-session MCP error: re-record or refresh your cookie")
            log("Offline end-to-end verification: PASS")
        finally:
            server.shutdown()
            thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("demo-output"))
    args = parser.parse_args()
    asyncio.run(verify(args.output))


if __name__ == "__main__":
    main()
