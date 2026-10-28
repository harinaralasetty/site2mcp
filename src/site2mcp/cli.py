"""Command-line entry point. Learning and inspection never contact the recorded site."""

import argparse
import asyncio
import sys
from pathlib import Path

from .describe import describe
from .generate import emit
from .har import HarError, load_har
from .inference import infer
from .runtime import SiteError


def main(argv=None):
    parser = argparse.ArgumentParser(prog="site2mcp", description="Turn a HAR into API tools")
    commands = parser.add_subparsers(dest="command", required=True)
    learn = commands.add_parser("learn", help="Infer OpenAPI, typed Python client and MCP server")
    learn.add_argument("har", type=Path)
    learn.add_argument("-o", "--output", type=Path, default=Path("out"))
    learn.add_argument("--host", help="First-party hostname, optionally with port")
    learn.add_argument("--name", default="Recorded site")
    learn.add_argument("--describe", choices=["claude", "codex", "none"], default="none")
    learn.add_argument(
        "--keep-examples",
        action="store_true",
        help="Include personal examples; credentials are still redacted",
    )
    inspect = commands.add_parser(
        "inspect", help="Summarize captured API traffic without raw values"
    )
    inspect.add_argument("har", type=Path)
    inspect.add_argument("--host")
    serve = commands.add_parser("serve", help="Run generated tools over stdio")
    serve.add_argument("output", type=Path)
    serve.add_argument("--allow-writes", action="store_true")
    serve.add_argument("--rate", type=float, default=2.0)
    serve.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .server import run

            asyncio.run(
                run(
                    args.output / "manifest.json",
                    allow_writes=args.allow_writes,
                    rate=args.rate,
                    timeout=args.timeout,
                )
            )
            return 0
        cap = load_har(args.har, host=args.host)
        manifest = infer(
            cap,
            name=getattr(args, "name", "Recorded site"),
            keep_examples=getattr(args, "keep_examples", False),
        )
        if args.command == "learn":
            if not manifest["operations"]:
                raise HarError(
                    "No supported first-party API operations; inspect capture or use --host"
                )
            manifest = describe(manifest, args.describe)
            emit(manifest, args.output)
            print(f"Learned {len(manifest['operations'])} tools from {cap.total} entries")
            print(f"Wrote openapi.yaml, client.py and server.py to {args.output}")
        else:
            print(
                "METHOD  PATH                                      "
                "TOOL                      SAMPLES  ACCESS"
            )
            for op in manifest["operations"]:
                print(
                    f"{op['method']:<7} {op['path']:<41} {op['name']:<25} "
                    f"{op['samples']:<8} {'read' if op['readOnly'] else 'write'}"
                )
            print(f"Total: {cap.total} entries; {len(manifest['operations'])} inferred tools")
        for drop in manifest["dropped"]:
            print(f"Dropped {drop['count']}: {drop['reason']}")
        return 0
    except (HarError, SiteError, OSError, ValueError, KeyError, TypeError) as exc:
        # Parser/runtime errors are already sanitized; filesystem/manifest errors
        # must not include file contents or unexpected credential values.
        message = (
            str(exc)
            if isinstance(exc, (HarError, SiteError))
            else ("Cannot read or write generated artifacts; check paths and manifest")
        )
        print("site2mcp: error: " + message, file=sys.stderr)
        return 2
