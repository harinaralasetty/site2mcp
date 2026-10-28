import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from test_pipeline import capture, entry

from site2mcp.inference import infer
from site2mcp.runtime import MAX_RESPONSE, Client, SiteError


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append((time.monotonic(), self.path, dict(self.headers)))
        path = self.path.split("?")[0]
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.server.origin + "/target")
            self.end_headers()
            return
        if path in ("/rate", "/error"):
            self.send_response(429 if path == "/rate" else 500)
            self.end_headers()
            self.wfile.write(b"never-echo-response-secret")
            return
        self.send_response(200)
        self.end_headers()
        if path == "/large":
            payload = b"x" * (MAX_RESPONSE + 1)
        elif path == "/html":
            payload = b"<h1>not json</h1>"
        else:
            payload = b'{"ok": true}'
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def client(self, path, **kwargs):
        return Client(infer(capture([entry(self.server.origin + path)])), **kwargs)

    def test_redirects_never_forward_credentials(self):
        manifest = infer(
            capture(
                [
                    entry(
                        self.server.origin + "/redirect",
                        headers=[{"name": "Cookie", "value": "sid=captured-secret"}],
                    )
                ]
            )
        )
        with patch.dict(os.environ, {"SITE2MCP_COOKIE": "sid=fresh-secret"}):
            client = Client(manifest)
            with self.assertRaisesRegex(SiteError, "Redirect refused"):
                client.call("list_redirect")
        self.assertEqual(len(self.server.requests), 1)
        self.assertNotIn("/target", [r[1] for r in self.server.requests])

    def test_error_messages_caps_and_rate_limit(self):
        for path, message in [
            ("/rate", "rate limit"),
            ("/error", "HTTP 500"),
            ("/large", "size limit"),
            ("/html", "non-JSON"),
        ]:
            with self.assertRaisesRegex(SiteError, message) as error:
                self.client(path).call("list_" + path[1:])
            self.assertNotIn("never-echo-response-secret", str(error.exception))
        client = self.client("/ok", rate=10)
        client.call("list_ok")
        client.call("list_ok")
        self.assertGreaterEqual(self.server.requests[-1][0] - self.server.requests[-2][0], 0.085)

    def test_environment_and_dotenv_credentials_replace_captured_values(self):
        record = entry(
            self.server.origin + "/notes?api_key=captured-query",
            "POST",
            {"title": "new", "nested": {"accessToken": "captured-body"}},
            headers=[{"name": "Authorization", "value": "Bearer captured-auth"}],
        )
        manifest = infer(capture([record]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                'SITE2MCP_AUTHORIZATION="Bearer from-file"\n'
                "SITE2MCP_API_KEY=fresh-query\n"
                "SITE2MCP_NESTED_ACCESSTOKEN=fresh-body\n"
            )
            with patch.dict(os.environ, {"SITE2MCP_AUTHORIZATION": "Bearer from-env"}, clear=True):
                client = Client(manifest, allow_writes=True, env_file=path)
                url, headers, body = client.prepare("create_note", {"body": {"title": "new"}})
            self.assertIn("api_key=fresh-query", url)
            self.assertEqual(headers["Authorization".lower()], "Bearer from-env")
            self.assertEqual(json.loads(body)["nested"]["accessToken"], "fresh-body")
            self.assertNotIn("captured", str((url, headers, body)))

    def test_invalid_rate_timeout_and_literal_dotenv(self):
        for settings in [{"rate": 0}, {"rate": float("nan")}, {"timeout": 0}]:
            with self.assertRaises(SiteError):
                self.client("/ok", **settings)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("SITE2MCP_COOKIE=$(never_execute)\n")
            client = self.client("/ok", env_file=path)
            self.assertEqual(client.env["SITE2MCP_COOKIE"], "$(never_execute)")
