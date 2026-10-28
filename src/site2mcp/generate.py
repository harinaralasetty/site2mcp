"""Emit portable sanitized artifacts. JSON is valid YAML 1.2, avoiding a YAML dependency."""

import copy
import json
import keyword
from pathlib import Path

from . import runtime, server


def openapi(manifest):
    spec = {
        "openapi": "3.1.0",
        "info": {"title": manifest["name"], "version": "0.1.0"},
        "servers": [
            {"url": origin} for origin in sorted({op["origin"] for op in manifest["operations"]})
        ],
        "paths": {},
        "components": {"securitySchemes": {}},
        "x-inference-notes": manifest["notes"],
        "x-dropped-traffic": manifest["dropped"],
    }
    for op in manifest["operations"]:
        parameters = []
        for location, schema in [
            ("path", op["inputSchema"]["properties"].get("path", {})),
            ("query", op["querySchema"]),
        ]:
            for key, child in schema.get("properties", {}).items():
                parameters.append(
                    {
                        "name": key,
                        "in": location,
                        "schema": child,
                        "required": key in schema.get("required", []),
                    }
                )
        responses = {
            status: {
                "description": f"Observed HTTP {status}",
                **(
                    {"content": {"application/json": {"schema": schema}}}
                    if schema is not None
                    else {}
                ),
            }
            for status, schema in op["responses"].items()
        }
        operation = {
            "operationId": op["name"],
            "description": op["description"],
            "parameters": parameters,
            "responses": responses,
            "x-read-only": op["readOnly"],
            "x-path-confidence": op["pathNotes"],
            "x-pagination": op["pagination"],
        }
        if len(spec["servers"]) > 1:
            operation["servers"] = [{"url": op["origin"]}]
        if op.get("graphql"):
            gql = op["graphql"]
            body = {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "operationName": {"type": "string"},
                    "variables": op["bodySchema"] or {"type": "object"},
                },
                "required": ["query", "operationName"],
            }
            operation["x-graphql-operations"] = [
                {"name": gql["operationName"], "tool": op["name"], "type": gql["type"]}
            ]
            if op["method"] == "GET":
                operation["parameters"].extend(
                    [
                        {
                            "name": "query",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {"name": "operationName", "in": "query", "schema": {"type": "string"}},
                        {"name": "variables", "in": "query", "schema": {"type": "string"}},
                    ]
                )
        else:
            body = op["bodySchema"]
        if body is not None and op["method"] not in ("GET", "HEAD"):
            mime = "application/json" if op.get("graphql") else op["mimeType"]
            operation["requestBody"] = {
                "required": op["bodyRequired"],
                "content": {mime: {"schema": body}},
            }
        security = {}
        for c in op["credentials"]:
            if c["location"] == "body":
                operation.setdefault("x-body-credentials", []).append(c)
                continue
            scheme_name = c["env"]
            scheme = (
                {"type": "http", "scheme": "bearer"}
                if c["kind"] == "bearer"
                else {"type": "apiKey", "in": c["location"], "name": c["name"]}
            )
            scheme["description"] = "Supply at runtime using " + c["env"]
            if c.get("roundTrip"):
                scheme["x-csrf-round-trip"] = True
            spec["components"]["securitySchemes"][scheme_name] = scheme
            security[scheme_name] = []
        if security:
            operation["security"] = [security]
        operations = spec["paths"].setdefault(op["path"], {})
        previous = operations.get(op["method"].lower())
        if previous and op.get("graphql"):
            previous["x-graphql-operations"].extend(operation["x-graphql-operations"])
            previous["x-read-only"] = previous["x-read-only"] and op["readOnly"]
            if not previous["x-read-only"] and "Changes data." not in previous["description"]:
                previous["description"] += " Some recorded operations change data."
            previous["responses"].update(operation["responses"])
            if "requestBody" in previous:
                old = previous["requestBody"]["content"]["application/json"]["schema"]
                new = operation["requestBody"]["content"]["application/json"]["schema"]
                previous["requestBody"]["content"]["application/json"]["schema"] = {
                    "anyOf": [old, new]
                }
            if security:
                previous.setdefault("security", [{}])[0].update(security)
        elif previous:
            # Multiple origins at the same path/method cannot be expressed as
            # separate OpenAPI operations. Runtime tools retain separate origins.
            previous.setdefault("x-alternate-tools", []).append(op["name"])
            previous["servers"] = [
                {"url": origin}
                for origin in sorted(
                    {
                        item["origin"]
                        for item in manifest["operations"]
                        if item["path"] == op["path"] and item["method"] == op["method"]
                    }
                )
            ]
        else:
            operations[op["method"].lower()] = operation
    return spec


class Models:
    def __init__(self):
        self.lines = []
        self.count = 0

    def type(self, schema, prefix):
        if schema is None:
            return "None"
        if "anyOf" in schema:
            return " | ".join(dict.fromkeys(self.type(child, prefix) for child in schema["anyOf"]))
        typ = schema.get("type")
        if typ == "object":
            if isinstance(schema.get("additionalProperties"), dict) and not schema.get(
                "properties"
            ):
                return "dict[str, " + self.type(schema["additionalProperties"], prefix) + "]"
            self.count += 1
            name = prefix + "Model" + str(self.count)
            fields = {}
            for key, value in schema.get("properties", {}).items():
                child = self.type(value, prefix)
                fields[key] = (
                    child if key in schema.get("required", []) else f"NotRequired[{child}]"
                )
            items = ", ".join(f"{key!r}: {value}" for key, value in fields.items())
            self.lines.append(f"{name} = TypedDict({name!r}, {{{items}}})")
            return name
        if typ == "array":
            return "list[" + self.type(schema.get("items", {}), prefix) + "]"
        return {
            "string": "str",
            "integer": "int",
            "number": "float",
            "boolean": "bool",
            "null": "None",
        }.get(typ, "Any")


def client_source(manifest):
    models = Models()
    methods = []
    used = {"call", "prepare", "tools", "__init__"}
    for op in manifest["operations"]:
        name = op["name"]
        if keyword.iskeyword(name) or name in used:
            name = "tool_" + name
        used.add(name)
        prefix = "".join(part.title() for part in name.split("_"))
        parameters, values = [], []
        schema = op["inputSchema"]
        for key, child in schema["properties"].items():
            typ = models.type(child, prefix + key.title())
            default = "" if key in schema.get("required", []) else " = _MISSING"
            parameters.append(f"{key}: {typ}{default}")
            values.append(f"        if {key} is not _MISSING:\n            args[{key!r}] = {key}")
        response_schemas = [s for status, s in op["responses"].items() if status.startswith("2")]
        response = {"anyOf": response_schemas} if response_schemas else {}
        returns = models.type(response, prefix + "Response")
        signature = ", *, " + ", ".join(parameters) if parameters else ""
        description = repr(op["description"])
        methods.append(
            f"    def {name}(self{signature}) -> {returns}:\n"
            f"        {description}\n"
            "        args: dict[str, Any] = {}\n"
            + "\n".join(values)
            + ("\n" if values else "")
            + f"        return cast({returns!r}, self.call({op['name']!r}, **args))\n"
        )
    return (
        "from __future__ import annotations\n\n"
        "from pathlib import Path\nfrom typing import Any, NotRequired, TypedDict, cast\n"
        "from runtime import Client as RuntimeClient\n\n_MISSING: Any = object()\n\n"
        + "\n\n".join(models.lines)
        + "\n\n\nclass Client(RuntimeClient):\n"
        "    def __init__(self, *, allow_writes=False, rate=2.0, timeout=15.0):\n"
        '        super().__init__(Path(__file__).with_name("manifest.json"),\n'
        "                         allow_writes=allow_writes, rate=rate, timeout=timeout)\n\n"
        + "\n".join(methods)
        + "\n"
    )


def emit(manifest, output):
    output = Path(output)
    # Render and compile before writing anything, avoiding partial Python artifacts.
    sources = {
        "client.py": client_source(manifest),
        "runtime.py": Path(runtime.__file__).read_text(),
        "server.py": Path(server.__file__).read_text(),
    }
    for filename, source in sources.items():
        compile(source, filename, "exec")
    artifacts = {
        **sources,
        "manifest.json": json.dumps(manifest, indent=2) + "\n",
        "openapi.yaml": json.dumps(openapi(manifest), indent=2) + "\n",
        "requirements.txt": "mcp==1.26.0\n",
    }
    credentials = sorted({c["env"] for op in manifest["operations"] for c in op["credentials"]})
    instructions = (
        "# Generated site2mcp tools\n\n"
        "Run `python server.py` for read tools, or "
        "`python server.py --allow-writes` for writes.\n"
        "Install `pip install -r requirements.txt` first. "
        "HTTP rate defaults to 2 requests/second and timeout to 15 seconds.\n\n"
        "Create a local `.env` here with fresh credentials, or set environment variables. "
        "Values are never generated. Environment variables override `.env`.\n\n"
        + "\n".join("- `" + name + "`" for name in credentials)
        + "\n\nFor Cookie, provide the entire Cookie header. For Authorization, "
        "include the Bearer prefix. Refresh CSRF and cookies together. "
        "Body/query credential variables contain the literal credential string.\n"
        "`SITE2MCP_BASE_URL` optionally overrides the origin for local testing.\n"
        "Inspect manifest.json for parameter shapes and heuristic confidence notes. "
        "Schemas only describe the captured samples.\n"
    )
    artifacts["README.md"] = instructions
    output.mkdir(parents=True, exist_ok=True)
    for filename, text in artifacts.items():
        (output / filename).write_text(text)
    return copy.deepcopy(manifest)
