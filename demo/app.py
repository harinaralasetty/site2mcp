"""Small, deliberately undocumented web app. Bind to loopback only."""

import argparse
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class NotesServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address=("127.0.0.1", 0)):
        super().__init__(address, Handler)
        self.notes = {
            1: {"id": 1, "title": "Plan garden", "tag": "home", "state": "open"},
            2: {
                "id": 2,
                "title": "Read protocol docs",
                "tag": "work",
                "state": "closed",
                "detail": "Optional detail",
            },
            3: {"id": 3, "title": "Buy seeds", "tag": "home", "state": "open"},
        }
        self.tags = [
            {"id": "550e8400-e29b-41d4-a716-446655440000", "label": "home"},
            {"id": "550e8400-e29b-41d4-a716-446655440001", "label": "work"},
        ]
        self.sid, self.csrf = secrets.token_hex(24), secrets.token_hex(24)
        self.password = "local-demo-password-only"
        self.request_times = []
        self.received = []

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, value, *, headers=None, mime="application/json"):
        data = (json.dumps(value) if mime == "application/json" else value).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        for key, item in headers or []:
            self.send_header(key, item)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def dispatch(self):
        parsed = urlsplit(self.path)
        path, query = parsed.path, parse_qs(parsed.query, keep_blank_values=True)
        body = {}
        if self.command in ("POST", "PATCH", "PUT"):
            data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if "x-www-form-urlencoded" in self.headers.get("Content-Type", ""):
                body = {k: v[0] for k, v in parse_qs(data.decode()).items()}
            else:
                try:
                    body = json.loads(data or b"{}")
                except ValueError:
                    return self.reply(400, {"error": "invalid JSON"})
        self.server.received.append(
            {
                "method": self.command,
                "path": path,
                "query": query,
                "body": body,
                "headers": dict(self.headers),
            }
        )
        if path == "/":
            return self.reply(
                200,
                "<!doctype html><title>Notes and tags</title>"
                "<h1>Notes and tags</h1><p>Use the local recorder to explore this API.</p>",
                mime="text/html",
            )
        if path == "/app.js":
            return self.reply(200, "// local demo asset", mime="application/javascript")
        if path == "/metrics/collect":
            return self.reply(200, {"recorded": True})
        if path == "/api/login" and self.command == "POST":
            if body.get("password") != self.server.password:
                return self.reply(401, {"error": "bad password"})
            return self.reply(
                200,
                {
                    "authenticated": True,
                    "csrfToken": self.server.csrf,
                    "name": "Local Demo User",
                    "email": "demo.private@example.test",
                },
                headers=[
                    ("Set-Cookie", f"sid={self.server.sid}; HttpOnly; Path=/"),
                    ("Set-Cookie", f"csrf={self.server.csrf}; Path=/"),
                ],
            )
        if f"sid={self.server.sid}" not in self.headers.get("Cookie", ""):
            return self.reply(401, {"error": "login required"})
        readonly_gql = path == "/graphql" and body.get("operationName") == "ListNotes"
        if self.command not in ("GET", "HEAD") and not readonly_gql:
            if self.headers.get("X-CSRF-Token") != self.server.csrf:
                return self.reply(403, {"error": "invalid CSRF"})
        if path == "/api/notes":
            if self.command in ("GET", "HEAD"):
                page, limit = int(query.get("page", ["1"])[0]), int(query.get("limit", ["2"])[0])
                notes = list(self.server.notes.values())
                start = (page - 1) * limit
                return self.reply(
                    200,
                    {
                        "notes": notes[start : start + limit],
                        "page": page,
                        "next": page + 1 if start + limit < len(notes) else None,
                    },
                )
            if self.command == "POST":
                identifier = max(self.server.notes) + 1
                note = {
                    "id": identifier,
                    "title": body["title"],
                    "tag": body.get("tag", "home"),
                    "state": "open",
                }
                self.server.notes[identifier] = note
                return self.reply(201, note)
        if path.startswith("/api/notes/"):
            try:
                identifier = int(path.rsplit("/", 1)[1])
                note = self.server.notes[identifier]
            except (ValueError, KeyError):
                return self.reply(404, {"error": "note not found"})
            if self.command == "PATCH":
                note.update(body)
            if self.command in ("GET", "PATCH", "HEAD"):
                return self.reply(200, note)
        if path == "/api/tags":
            return self.reply(200, {"tags": self.server.tags})
        if path.startswith("/api/tags/"):
            matches = [tag for tag in self.server.tags if tag["id"] == path.rsplit("/", 1)[1]]
            return self.reply(200, matches[0]) if matches else self.reply(404, {"error": "tag"})
        if path == "/graphql" and self.command == "POST":
            variables = body.get("variables", {})
            if body.get("operationName") == "ListNotes":
                return self.reply(
                    200,
                    {
                        "data": {
                            "notes": list(self.server.notes.values())[: variables.get("limit", 2)]
                        }
                    },
                )
            if body.get("operationName") == "AddNote":
                identifier = max(self.server.notes) + 1
                note = {
                    "id": identifier,
                    "title": variables["title"],
                    "tag": "work",
                    "state": "open",
                }
                self.server.notes[identifier] = note
                return self.reply(200, {"data": {"addNote": note}})
        return self.reply(404, {"error": "not found"})

    do_GET = dispatch
    do_POST = dispatch
    do_PATCH = dispatch
    do_HEAD = dispatch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with NotesServer(("127.0.0.1", args.port)) as server:
        print(f"Notes demo listening on {server.origin}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
