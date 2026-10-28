"""Drive a loopback-only normal app session and save HAR 1.2 without a proxy."""

import argparse
import json
import time
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


class Recorder:
    def __init__(self, origin):
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("The demo recorder only connects to loopback HTTP")
        self.origin = origin.rstrip("/")
        self.cookies, self.entries = {}, []
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path, method="GET", *, body=None, csrf=None, form=False, resource="fetch"):
        url = self.origin + path
        headers = {"Accept": "application/json"}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in self.cookies.items())
        if csrf:
            headers["X-CSRF-Token"] = csrf
        data, mime = None, "application/json"
        if body is not None:
            mime = "application/x-www-form-urlencoded" if form else mime
            headers["Content-Type"] = mime
            data = (urlencode(body) if form else json.dumps(body)).encode()
        started = datetime.now(UTC).isoformat()
        before = time.perf_counter()
        with self.opener.open(
            Request(url, data=data, headers=headers, method=method), timeout=5
        ) as res:
            response_bytes = res.read()
            status, reason, response_headers = res.status, res.reason, list(res.headers.items())
            response_mime = res.headers.get_content_type()
        elapsed = (time.perf_counter() - before) * 1000
        response_cookies = []
        for key, value in response_headers:
            if key.lower() == "set-cookie":
                parsed = SimpleCookie()
                parsed.load(value)
                for cookie in parsed.values():
                    self.cookies[cookie.key] = cookie.value
                    response_cookies.append({"name": cookie.key, "value": cookie.value})
        req = {
            "method": method,
            "url": url,
            "httpVersion": "HTTP/1.1",
            "headers": [{"name": k, "value": v} for k, v in headers.items()],
            "cookies": [
                {"name": k, "value": v}
                for k, v in self.cookies.items()
                if k in headers.get("Cookie", "")
            ],
            "queryString": [
                {"name": k, "value": v}
                for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True)
            ],
            "headersSize": -1,
            "bodySize": len(data) if data else 0,
        }
        if data:
            req["postData"] = {"mimeType": mime, "text": data.decode()}
            if form:
                req["postData"]["params"] = [{"name": k, "value": str(v)} for k, v in body.items()]
        self.entries.append(
            {
                "startedDateTime": started,
                "time": elapsed,
                "request": req,
                "response": {
                    "status": status,
                    "statusText": reason,
                    "httpVersion": "HTTP/1.1",
                    "headers": [{"name": k, "value": v} for k, v in response_headers],
                    "cookies": response_cookies,
                    "content": {
                        "size": len(response_bytes),
                        "mimeType": response_mime,
                        "text": response_bytes.decode(),
                    },
                    "redirectURL": "",
                    "headersSize": -1,
                    "bodySize": len(response_bytes),
                },
                "cache": {},
                "timings": {"send": 0, "wait": elapsed, "receive": 0},
                "_resourceType": resource,
            }
        )
        return json.loads(response_bytes) if response_mime == "application/json" else None

    def save(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(
                {
                    "log": {
                        "version": "1.2",
                        "creator": {"name": "site2mcp-local-recorder", "version": "0.1.0"},
                        "entries": self.entries,
                    }
                },
                indent=2,
            )
            + "\n"
        )


def record(origin, path, password="local-demo-password-only"):
    recorder = Recorder(origin)
    recorder.request("/", resource="document")
    recorder.request("/app.js", resource="script")
    login = recorder.request(
        "/api/login",
        "POST",
        body={"username": "demo.private@example.test", "password": password},
        form=True,
    )
    csrf = login["csrfToken"]
    recorder.request("/api/notes?page=1&limit=2")
    recorder.request("/api/notes?page=2&limit=2")
    recorder.request("/api/notes/1")
    recorder.request("/api/notes/2")
    recorder.request("/api/tags")
    recorder.request("/api/tags/550e8400-e29b-41d4-a716-446655440000")
    recorder.request("/api/tags/550e8400-e29b-41d4-a716-446655440001")
    recorder.request(
        "/api/notes", "POST", body={"title": "Record a session", "tag": "work"}, csrf=csrf
    )
    recorder.request("/api/notes/1", "PATCH", body={"state": "closed"}, csrf=csrf)
    recorder.request(
        "/graphql",
        "POST",
        body={
            "operationName": "ListNotes",
            "variables": {"limit": 2},
            "query": "query ListNotes($limit: Int!) { notes(limit: $limit) { id title } }",
        },
    )
    recorder.request(
        "/graphql",
        "POST",
        body={
            "operationName": "AddNote",
            "variables": {"title": "GraphQL note"},
            "query": "mutation AddNote($title: String!) { addNote(title: $title) { id title } }",
        },
        csrf=csrf,
    )
    recorder.request("/metrics/collect")
    recorder.save(path)
    return recorder


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("-o", "--output", type=Path, default=Path("demo-output/session.har"))
    args = parser.parse_args()
    recorder = record(args.url, args.output)
    print(f"Recorded {len(recorder.entries)} local requests to {args.output}")


if __name__ == "__main__":
    main()
