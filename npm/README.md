# site2mcp

Turn any website into an MCP server your AI agents can call.

This npm package launches the Python tool. It has no npm dependencies and needs
Python 3.11+ with uv or pipx installed. It tries `uvx site2mcp==0.1.0` first,
then `pipx run site2mcp==0.1.0` if uvx is unavailable. Arguments, standard input,
output, and the tool's exit code pass through.

After the npm and PyPI releases are published:

```sh
npx site2mcp --help
```

If neither runner exists, or the Python release cannot be fetched, the launcher
prints an install hint and exits unsuccessfully. Until publication, use the source
checkout instructions in the [main README](https://github.com/harinaralasetty/site2mcp#quick-start).

Record a browser session as HAR, inspect it, learn the API tools, then supply fresh
credentials to serve them. Writes are hidden unless `--allow-writes`.
See the [main README](https://github.com/harinaralasetty/site2mcp#readme) for commands,
credential rules, and limits.

MIT license. By Hari Naralasetty ([GitHub](https://github.com/harinaralasetty) · [LinkedIn](https://www.linkedin.com/in/harinaralasetty)).
