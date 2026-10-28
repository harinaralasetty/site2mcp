import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openapi_spec_validator import validate
from test_pipeline import capture, entry

from site2mcp.describe import describe
from site2mcp.generate import emit
from site2mcp.har import load_har
from site2mcp.inference import infer


class GenerationTests(unittest.TestCase):
    def test_artifacts_validate_compile_and_contain_no_fixture_secrets(self):
        fixtures = Path(__file__).parent / "fixtures"
        secrets = [
            "fixture-cookie-DO-NOT-EXPORT",
            "fixture-api-key-DO-NOT-EXPORT",
            "fixture-bearer-DO-NOT-EXPORT",
            "fixture.private@example.test",
            "Fixture Private Name",
            "fixture-nested-DO-NOT-EXPORT",
            "fixture-body-key-DO-NOT-EXPORT",
            "fixture-csrf-DO-NOT-EXPORT",
        ]
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            for fixture in sorted(fixtures.glob("*.har")):
                emit(infer(load_har(fixture)), out)
                validate(json.loads((out / "openapi.yaml").read_text()))
                for artifact in out.rglob("*"):
                    if artifact.is_file():
                        text = artifact.read_text()
                        for secret in secrets:
                            self.assertNotIn(secret, text, str(artifact))
                        if artifact.suffix == ".py":
                            compile(text, str(artifact), "exec")
            self.assertFalse((out / ".env").exists())
            self.assertIn("TypedDict", (out / "client.py").read_text())
            self.assertIn("def update_note", (out / "client.py").read_text())

    def test_graphql_openapi_merges_operations_without_losing_tools(self):
        records = [
            entry(
                "http://localhost:8765/graphql",
                "POST",
                {"operationName": name, "query": f"{kind} {name} {{ notes {{ id }} }}"},
            )
            for name, kind in [("Notes", "query"), ("Update", "mutation")]
        ]
        with tempfile.TemporaryDirectory() as directory:
            emit(infer(capture(records)), Path(directory))
            spec = json.loads((Path(directory) / "openapi.yaml").read_text())
            validate(spec)
            self.assertEqual(len(spec["paths"]["/graphql"]["post"]["x-graphql-operations"]), 2)

    def test_cli_errors_are_clear_and_do_not_copy_capture_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.har"
            path.write_text('{"secret": "never-echo-this"')
            result = subprocess.run(
                [sys.executable, "-m", "site2mcp", "learn", str(path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("error:", result.stderr)
            self.assertNotIn("never-echo-this", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_describer_fallback_and_untrusted_output(self):
        manifest = infer(capture([entry()]))
        for provider in ["claude", "codex"]:
            with patch("site2mcp.describe.shutil.which", return_value=None):
                self.assertEqual(describe(manifest, provider), manifest)
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "codex"
            fake.write_text(
                '#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nprint("not json")\n'
            )
            fake.chmod(0o755)
            with patch("site2mcp.describe.shutil.which", return_value=str(fake)):
                self.assertEqual(describe(manifest, "codex"), manifest)

    def test_describer_receives_only_structural_metadata_in_empty_temp_dir(self):
        manifest = infer(
            capture([entry(response={"name": "Private Person", "token": "secret"})]),
            keep_examples=True,
        )
        observed = {}

        def run(args, **kwargs):
            observed.update(kwargs)
            observed["args"] = args
            self.assertEqual(list(Path(kwargs["cwd"]).iterdir()), [])
            self.assertNotIn("Private Person", kwargs["input"])
            self.assertNotIn("secret", kwargs["input"])
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {
                            "id": manifest["operations"][0]["name"],
                            "name": "read_notes",
                            "description": "Read notes.",
                        }
                    ]
                ),
                "",
            )

        with (
            patch("site2mcp.describe.shutil.which", return_value="/bin/fake"),
            patch("site2mcp.describe.subprocess.run", side_effect=run),
        ):
            result = describe(manifest, "claude")
        self.assertEqual(result["operations"][0]["name"], "read_notes")
        self.assertIn("--tools", observed["args"])
        self.assertLessEqual(observed["timeout"], 60)

    def test_runtime_write_gate_and_missing_credentials(self):
        from site2mcp.runtime import Client, SiteError

        manifest = infer(
            capture(
                [
                    entry(headers=[{"name": "Cookie", "value": "sid=private"}]),
                    entry(method="POST", body={"title": "new"}),
                ]
            )
        )
        with patch.dict(os.environ, {}, clear=True):
            client = Client(manifest)
            with self.assertRaisesRegex(SiteError, "SITE2MCP_COOKIE"):
                client.call("list_notes")
            with self.assertRaisesRegex(SiteError, "allow-writes"):
                client.call("create_note", body={"title": "new"})

    def test_runtime_schema_validation_and_path_encoding(self):
        from site2mcp.runtime import Client, SiteError

        manifest = infer(capture([entry("http://localhost:8765/api/notes/42")]))
        client = Client(manifest)
        for arguments in [{}, {"path": {"noteId": {"bad": True}}}, {"other": 1}]:
            with self.assertRaises(SiteError):
                client.call("get_note", **arguments)
        self.assertEqual(
            client.prepare("get_note", {"path": {"noteId": "a/b?c"}})[0],
            "http://localhost:8765/api/notes/a%2Fb%3Fc",
        )

    def test_generated_client_has_typed_input_and_response_models(self):
        manifest = infer(
            capture(
                [entry("http://localhost:8765/api/notes/42", response={"id": 42, "title": "note"})]
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            emit(manifest, Path(directory))
            spec = importlib.util.spec_from_file_location(
                "generated_client", Path(directory) / "client.py"
            )
            module = importlib.util.module_from_spec(spec)
            sys.path.insert(0, directory)
            try:
                spec.loader.exec_module(module)
                self.assertTrue(hasattr(module.Client, "get_note"))
                self.assertIn("path", module.Client.get_note.__annotations__)
                self.assertIn("return", module.Client.get_note.__annotations__)
            finally:
                sys.path.pop(0)
                sys.modules.pop("runtime", None)


class DescriberSafetyTests(unittest.TestCase):
    def test_codex_without_explicit_no_tools_capability_falls_back(self):
        manifest = infer(capture([entry()]))
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            self.assertNotIn("SITE2MCP_COOKIE", kwargs["env"])
            return subprocess.CompletedProcess(args, 0, "Usage: codex exec [OPTIONS]", "")

        with (
            patch("site2mcp.describe.shutil.which", return_value="/bin/fake"),
            patch("site2mcp.describe.subprocess.run", side_effect=run),
            patch.dict(os.environ, {"SITE2MCP_COOKIE": "never-send-site-cookie"}),
        ):
            self.assertEqual(describe(manifest, "codex"), manifest)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], "--help")

    def test_duplicate_or_invalid_model_labels_and_timeout_fall_back(self):
        manifest = infer(capture([entry()]))
        for output in [
            '[{"id":"list_notes","name":"bad-name","description":"x"}]',
            '[{"id":"wrong","name":"okay","description":"x"}]',
            "{}",
            "null",
        ]:
            with (
                patch("site2mcp.describe.shutil.which", return_value="/bin/fake"),
                patch(
                    "site2mcp.describe.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, output, ""),
                ),
            ):
                self.assertEqual(describe(manifest, "claude"), manifest)
        with (
            patch("site2mcp.describe.shutil.which", return_value="/bin/fake"),
            patch(
                "site2mcp.describe.subprocess.run",
                side_effect=subprocess.TimeoutExpired("fake", 45),
            ),
        ):
            self.assertEqual(describe(manifest, "claude"), manifest)


class OptionalBodyTests(unittest.TestCase):
    def test_optional_body_presence_is_preserved_in_spec_and_runtime(self):
        from site2mcp.generate import openapi
        from site2mcp.runtime import Client

        manifest = infer(capture([entry(method="POST", body={"title": "x"}), entry(method="POST")]))
        self.assertFalse(
            openapi(manifest)["paths"]["/api/notes"]["post"]["requestBody"]["required"]
        )
        with patch.dict(os.environ, {}, clear=True):
            client = Client(manifest, allow_writes=True)
            self.assertIsNone(client.prepare("create_note", {})[2])
            self.assertEqual(
                json.loads(client.prepare("create_note", {"body": {"title": "x"}})[2]),
                {"title": "x"},
            )
