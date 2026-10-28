import base64
import json
import tempfile
import unittest
from pathlib import Path

from site2mcp.har import HarError, load_har
from site2mcp.inference import infer, template_path
from site2mcp.schema import infer_schema


def entry(
    url="http://localhost:8765/api/notes",
    method="GET",
    body=None,
    response=None,
    headers=None,
    resource="fetch",
):
    request = {
        "method": method,
        "url": url,
        "httpVersion": "HTTP/1.1",
        "headers": headers or [],
        "cookies": [],
        "queryString": [],
        "headersSize": -1,
        "bodySize": 0,
    }
    if body is not None:
        request["postData"] = {"mimeType": "application/json", "text": json.dumps(body)}
    return {
        "startedDateTime": "2026-10-21T00:00:00Z",
        "time": 1,
        "request": request,
        "response": {
            "status": 200,
            "statusText": "OK",
            "httpVersion": "HTTP/1.1",
            "headers": [],
            "cookies": [],
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": 0,
            "content": {
                "size": 0,
                "mimeType": "application/json",
                "text": json.dumps(response or {"ok": True}),
            },
        },
        "cache": {},
        "timings": {"send": 0, "wait": 1, "receive": 0},
        "_resourceType": resource,
    }


def har(entries):
    return {
        "log": {"version": "1.2", "creator": {"name": "unit", "version": "1"}, "entries": entries}
    }


def capture(entries, **kwargs):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "capture.har"
        path.write_text(json.dumps(har(entries)))
        return load_har(path, **kwargs)


class PipelineTests(unittest.TestCase):
    def test_path_templates_numeric_uuid_hex_and_repeated_resources(self):
        path, notes = template_path("/users/123/orders/9f0123456789abcd")
        self.assertEqual(path, "/users/{userId}/orders/{orderId}")
        self.assertTrue(all("confidence" in note for note in notes))
        path, _ = template_path("/notes/550e8400-e29b-41d4-a716-446655440000")
        self.assertEqual(path, "/notes/{noteId}")
        path, _ = template_path("/users/1/users/2")
        self.assertEqual(path, "/users/{userId}/users/{userId2}")

    def test_schema_merge_optional_nested_arrays_formats_and_enums(self):
        samples = [
            {"state": "open", "owner": {"email": "a@example.test"}, "tags": [1]},
            {"state": "closed", "owner": {"email": "b@example.test"}, "tags": []},
            {"state": "open", "tags": [2], "extra": None},
        ]
        schema = infer_schema(samples)
        self.assertEqual(schema["required"], ["state", "tags"])
        self.assertEqual(schema["properties"]["state"]["enum"], ["closed", "open"])
        self.assertEqual(schema["properties"]["owner"]["properties"]["email"]["format"], "email")
        self.assertNotIn("enum", schema["properties"]["owner"]["properties"]["email"])
        self.assertEqual(schema["properties"]["tags"]["items"]["type"], "integer")
        self.assertEqual(infer_schema([1, 1.5])["type"], "number")
        self.assertEqual(infer_schema(["2026-10-21T10:00:00Z"])["format"], "date-time")
        self.assertEqual(infer_schema([None, "x"])["anyOf"][0]["type"], "null")

    def test_filtering_reports_all_drops_and_host_override(self):
        records = [
            entry(),
            entry("http://localhost:8765/app.js"),
            entry("http://analytics.test/collect"),
            entry("http://other.test/api/things"),
            entry("http://localhost:8765/metrics/collect"),
        ]
        cap = capture(records)
        self.assertEqual(len(cap.entries), 1)
        self.assertEqual(sum(d["count"] for d in cap.dropped), 4)
        cap = capture(records, host="other.test")
        self.assertEqual(len(cap.entries), 1)
        self.assertEqual(cap.host, "other.test")

    def test_auth_and_csrf_roundtrip(self):
        first = entry("http://localhost:8765/api/login", "POST", {"password": "pw-secret"})
        first["response"]["cookies"] = [{"name": "csrf", "value": "csrf-secret"}]
        second = entry(
            headers=[
                {"name": "Cookie", "value": "sid=cookie-secret; csrf=csrf-secret"},
                {"name": "Authorization", "value": "Bearer bearer-secret"},
                {"name": "X-API-Key", "value": "key-secret"},
                {"name": "X-CSRF-Token", "value": "csrf-secret"},
            ]
        )
        manifest = infer(capture([first, second]))
        bindings = [c for op in manifest["operations"] for c in op["credentials"]]
        self.assertTrue({"cookie", "bearer", "apiKey", "csrf"} <= {c["kind"] for c in bindings})
        self.assertTrue(any(c.get("roundTrip") for c in bindings if c["kind"] == "csrf"))
        encoded = json.dumps(manifest)
        for secret in ["pw-secret", "cookie-secret", "csrf-secret", "bearer-secret", "key-secret"]:
            self.assertNotIn(secret, encoded)

    def test_redaction_nested_queries_personal_and_keep_examples(self):
        record = entry(
            "http://localhost:8765/api/notes?access_token=query-secret&email=me%40example.test",
            "POST",
            {
                "nested": {"refreshToken": "nested-secret", "name": "Private Person"},
                "phone": "+1 555 123 9876",
                "title": "ordinary",
            },
            {"email": "me@example.test", "password": "response-secret"},
        )
        for keep in (False, True):
            manifest = infer(capture([record]), keep_examples=keep)
            serialized = json.dumps(manifest)
            for secret in ["query-secret", "nested-secret", "response-secret"]:
                self.assertNotIn(secret, serialized)
            if not keep:
                for personal in ["me@example.test", "Private Person", "+1 555 123 9876"]:
                    self.assertNotIn(personal, serialized)
            else:
                self.assertIn("Private Person", serialized)

    def test_pagination_and_repeated_query_parameters(self):
        manifest = infer(
            capture(
                [
                    entry("http://localhost:8765/api/notes?page=1&tag=a&tag=b"),
                    entry("http://localhost:8765/api/notes?page=2&limit=2"),
                ]
            )
        )
        op = manifest["operations"][0]
        self.assertIn("page", op["pagination"])
        self.assertEqual(op["querySchema"]["properties"]["tag"]["type"], "array")
        self.assertEqual(op["querySchema"]["required"], ["page"])

    def test_graphql_one_operation_each_and_mutations_gated(self):
        docs = [
            ("ListNotes", "query ListNotes($limit: Int!) { notes(limit: $limit) { id } }"),
            ("AddNote", "mutation AddNote($title: String!) { addNote(title: $title) { id } }"),
        ]
        records = [
            entry(
                "http://localhost:8765/graphql",
                "POST",
                {"query": doc, "operationName": name, "variables": {"limit": 2}},
            )
            for name, doc in docs
        ]
        ops = infer(capture(records))["operations"]
        self.assertEqual(len(ops), 2)
        self.assertEqual(
            {o["graphql"]["operationName"]: o["readOnly"] for o in ops},
            {"ListNotes": True, "AddNote": False},
        )
        self.assertTrue(all("query" not in o["inputSchema"]["properties"] for o in ops))

    def test_graphql_ambiguous_or_literal_documents_are_not_replayed(self):
        for doc in [
            "query A { notes { id } } mutation B { erase }",
            'query A { user(email: "private@example.test") { name } }',
        ]:
            cap = capture(
                [
                    entry(
                        "http://localhost:8765/graphql",
                        "POST",
                        {"query": doc, "operationName": "A"},
                    )
                ]
            )
            result = infer(cap)
            self.assertEqual(result["operations"], [])
            self.assertTrue(result["dropped"])
            self.assertNotIn("private@example.test", json.dumps(result))

    def test_malformed_and_size_caps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.har"
            for data in [
                "{",
                "[]",
                '{"log": {"entries": [null]}}',
                json.dumps(har([{"request": {"url": "not-a-url"}}])),
            ]:
                path.write_text(data)
                with self.assertRaises(HarError):
                    load_har(path)
            path.write_text(json.dumps(har([entry()])))
            with self.assertRaisesRegex(HarError, "size limit"):
                load_har(path, max_bytes=10)
            with self.assertRaisesRegex(HarError, "entry limit"):
                load_har(path, max_entries=0)
            path.write_text(json.dumps(har([entry(response={"deep": [[[1]]]})])))
            with self.assertRaisesRegex(HarError, "depth"):
                load_har(path, max_depth=2)

    def test_browser_fixtures_and_base64_response(self):
        for name in ["chrome.har", "firefox.har"]:
            cap = load_har(Path(__file__).parent / "fixtures" / name)
            self.assertTrue(cap.entries)
            self.assertTrue(infer(cap)["operations"])
        record = entry()
        record["response"]["content"].update(
            encoding="base64", text=base64.b64encode(b'{"ok":true}').decode()
        )
        self.assertEqual(capture([record]).entries[0]["responseBody"], {"ok": True})
        image = entry()
        image["response"]["content"].update(
            mimeType="image/png",
            encoding="base64",
            text=base64.b64encode(b"\x89PNG\xff\x00").decode(),
        )
        self.assertEqual(len(capture([image, entry()]).entries), 2)
        aborted = entry()
        aborted["response"]["status"] = -1
        result = capture([aborted, entry()])
        self.assertEqual(len(result.entries), 1)
        self.assertIn({"reason": "incomplete response", "count": 1}, result.dropped)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bom.har"
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps(har([record])).encode())
            self.assertEqual(load_har(path).entries[0]["responseBody"], {"ok": True})


class PrivacyEdgeTests(unittest.TestCase):
    def test_display_names_serialized_json_and_url_secrets_are_sanitized(self):
        record = entry(
            response={
                "displayName": "Hidden Display Person",
                "payload": json.dumps({"token": "hidden-serialized-secret"}),
                "link": "https://example.test/reset?api_key=hidden-url-secret",
            }
        )
        result = infer(capture([record]), keep_examples=True)
        encoded = json.dumps(result)
        self.assertNotIn("hidden-serialized-secret", encoded)
        self.assertNotIn("hidden-url-secret", encoded)
        result = infer(capture([record]))
        self.assertNotIn("Hidden Display Person", json.dumps(result))

    def test_nonfinite_json_rejected_and_real_default_caps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.har"
            path.write_bytes(b" " * (32 * 1024 * 1024 + 1))
            with self.assertRaisesRegex(HarError, "size limit"):
                load_har(path)
            record = entry()
            record["response"]["content"]["text"] = '{"value": NaN}'
            path.write_text(json.dumps(har([record])))
            with self.assertRaises(HarError):
                load_har(path)

    def test_body_size_cap_and_secret_path_segment(self):
        record = entry(response={"value": "x" * (1024 * 1024)})
        result = capture([record, entry()])
        self.assertEqual(len(result.entries), 1)
        self.assertIn({"reason": "body over 1 MiB", "count": 1}, result.dropped)
        manifest = infer(capture([entry("http://localhost:8765/reset-token/path-secret-value")]))
        self.assertNotIn("path-secret-value", json.dumps(manifest))


class ReviewRegressionTests(unittest.TestCase):
    def test_graphql_rejects_numeric_enum_and_default_literals(self):
        documents = [
            "query Lookup @cache(token: SECRET_ENUM) { account { id } }",
            "query Lookup { account(token: 123456789) { id } }",
            "query Lookup { account(token: SECRET_ENUM) { id } }",
            "query Lookup($token: Int = 123456789) { account(token: $token) { id } }",
        ]
        for document in documents:
            result = infer(
                capture(
                    [
                        entry(
                            "http://localhost:8765/graphql",
                            "POST",
                            {"query": document, "operationName": "Lookup"},
                        )
                    ]
                )
            )
            self.assertFalse(result["operations"])
            self.assertNotIn("123456789", json.dumps(result))
            self.assertNotIn("SECRET_ENUM", json.dumps(result))

    def test_rest_search_query_parameter_is_not_graphql(self):
        result = infer(capture([entry("http://localhost:8765/api/search?query=hello")]))
        self.assertEqual(len(result["operations"]), 1)
        self.assertNotIn("graphql", result["operations"][0])
        self.assertIn("query", result["operations"][0]["querySchema"]["properties"])

    def test_dynamic_private_keys_use_additional_properties(self):
        result = infer(capture([entry(response={"alice.private@example.test": {"id": 1}})]))
        self.assertNotIn("alice.private@example.test", json.dumps(result))
        schema = result["operations"][0]["responses"]["200"]
        self.assertEqual(schema["additionalProperties"]["type"], "object")

    def test_copied_numeric_and_short_secrets_are_always_redacted(self):
        record = entry(
            response={"token": 987654321, "echo": 987654321, "password": "xy", "copy": "xy"}
        )
        result = infer(capture([record]), keep_examples=True)
        response = result["operations"][0]["examples"]["response"]
        self.assertEqual(response["echo"], "[REDACTED]")
        self.assertEqual(response["copy"], "[REDACTED]")
        self.assertNotIn("987654321", json.dumps(result))
