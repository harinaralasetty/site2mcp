# Changelog

## 0.1.0 - 2026-10-28

- Inspect browser HAR recordings and learn observed JSON REST and named GraphQL
  operations without contacting the recorded site.
- Generate OpenAPI 3.1, a typed Python client, and a standalone MCP stdio server.
- Redact captured credentials and require fresh runtime credentials; hide write
  tools unless explicitly enabled.
- Include an offline loopback demo covering recording, generation, OpenAPI
  validation, MCP calls, and the typed client.
- Handle real browser captures in commit `ea5e86b`:
  - Skip entries with captured bodies over 1 MiB and report `body over 1 MiB`,
    instead of aborting the whole capture.
  - Treat non-UTF-8 base64 binary content as having no body for schema inference.
    Invalid base64 still produces an error.
  - Accept status `-1` for aborted requests and drop them as `incomplete response`.
- Add package metadata, MIT licensing, launch documentation, a version-matched
  npm launcher, and CI and tag-triggered release workflows.
