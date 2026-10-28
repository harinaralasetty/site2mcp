"""Standalone stdlib request runtime, copied into each generated directory."""

from __future__ import annotations

import copy
import json
import math
import os
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_RESPONSE = 4 * 1024 * 1024


class SiteError(RuntimeError):
    """An actionable error that does not echo credentials or response data."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_env(path):
    """Read literal KEY=VALUE lines. No shell execution or interpolation."""
    values = {}
    source = Path(path)
    if source.is_file():
        for line in source.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep or not key.strip().replace("_", "").isalnum():
                raise SiteError("Invalid .env line; use KEY=VALUE")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            values[key.strip()] = value
    values.update(os.environ)
    return values


def validate_value(value, schema, location="arguments"):
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                validate_value(value, option, location)
                return
            except SiteError:
                pass
        raise SiteError(f"Invalid type for {location}")
    typ = schema.get("type")
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (int, float) and math.isfinite(value),
        "boolean": type(value) is bool,
        "null": value is None,
    }
    if typ and not valid.get(typ, True):
        raise SiteError(f"Invalid type for {location}; expected {typ}")
    if "enum" in schema and value not in schema["enum"]:
        raise SiteError(f"Invalid value for {location}; outside inferred enum")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        raise SiteError(f"Invalid empty value for {location}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in value:
                raise SiteError(f"Missing required {location}.{required}")
        for key, item in value.items():
            if key not in properties and schema.get("additionalProperties") is False:
                raise SiteError(f"Unknown field in {location}")
            if key in properties:
                validate_value(item, properties[key], location + "." + key)
    if isinstance(value, list):
        for item in value:
            validate_value(item, schema.get("items", {}), location + "[]")


def _put(root, path, value):
    cursor = root
    for index, part in enumerate(path):
        last = index == len(path) - 1
        if isinstance(cursor, list):
            position = int(part)
            while len(cursor) <= position:
                cursor.append(None)
            if last:
                cursor[position] = value
                return
            if cursor[position] is None:
                cursor[position] = [] if path[index + 1].isdigit() else {}
            cursor = cursor[position]
        else:
            if last:
                cursor[part] = value
                return
            cursor = cursor.setdefault(part, [] if path[index + 1].isdigit() else {})


class Client:
    def __init__(self, manifest, *, allow_writes=False, rate=2.0, timeout=15.0, env_file=None):
        if isinstance(manifest, (str, Path)):
            path = Path(manifest)
            self.manifest = json.loads(path.read_text())
            env_file = env_file or path.parent / ".env"
        else:
            self.manifest = manifest
        if not 0 < rate <= 100 or not 0 < timeout <= 300:
            raise SiteError("Rate must be 0 < rate <= 100 and timeout 0 < timeout <= 300")
        self.allow_writes = allow_writes
        self.rate, self.timeout = rate, timeout
        self.env = load_env(env_file) if env_file else dict(os.environ)
        self.operations = {op["name"]: op for op in self.manifest["operations"]}
        self._opener = build_opener(ProxyHandler({}), NoRedirect())
        self._lock, self._last = threading.Lock(), None

    def tools(self):
        return [op for op in self.operations.values() if op["readOnly"] or self.allow_writes]

    def prepare(self, operation, arguments):
        if operation not in self.operations:
            raise SiteError("Unknown tool")
        op = self.operations[operation]
        if not op["readOnly"] and not self.allow_writes:
            raise SiteError("This tool changes data; enable --allow-writes to use it")
        validate_value(arguments, op["inputSchema"])
        path = op["path"]
        for key, value in arguments.get("path", {}).items():
            encoded = quote(str(value), safe="")
            if encoded in (".", ".."):
                raise SiteError("Path segments may not be dot segments")
            path = path.replace("{" + key + "}", encoded)
        origin = self.env.get("SITE2MCP_BASE_URL", op["origin"]).rstrip("/")
        parsed = urlsplit(origin)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            raise SiteError("Base URL must be HTTP(S) without userinfo")
        if parsed.query or parsed.fragment:
            raise SiteError("Base URL may not have a query or fragment")
        query = copy.deepcopy(arguments.get("query", {}))
        graphql = op.get("graphql")
        body_key = "variables" if graphql else "body"
        has_body = body_key in arguments or op.get("bodyRequired", False)
        body = copy.deepcopy(arguments.get(body_key, {}))
        headers = {"Accept": "application/json", "User-Agent": "site2mcp/0.1"}
        for credential in op["credentials"]:
            value = self.env.get(credential["env"])
            if not value:
                raise SiteError(
                    f"Missing credential {credential['env']}; set it in environment or .env"
                )
            if "\r" in value or "\n" in value:
                raise SiteError(f"Invalid newline in credential {credential['env']}")
            location = credential["location"]
            if location == "header":
                headers[credential["name"]] = value
            elif location == "query":
                query[credential["name"]] = value
            elif location == "body":
                _put(body, credential["path"], value)
                has_body = True
        if graphql:
            document = {
                "query": graphql["document"],
                "operationName": graphql["operationName"],
                "variables": body,
            }
            if op["method"] == "GET":
                query.update(
                    query=document["query"],
                    operationName=document["operationName"],
                    variables=json.dumps(body),
                )
                has_body = False
            else:
                body, has_body = document, True

        def wire(value):
            return str(value).lower() if isinstance(value, bool) else value

        query = {
            k: [wire(item) for item in v] if isinstance(v, list) else wire(v)
            for k, v in query.items()
        }
        url = origin + path + ("?" + urlencode(query, doseq=True) if query else "")
        data = None
        if has_body:
            mime = op["mimeType"]
            if graphql or mime == "application/graphql":
                mime = "application/json"
            headers["Content-Type"] = mime
            data = (
                urlencode(body, doseq=True)
                if mime == "application/x-www-form-urlencoded"
                else json.dumps(body, allow_nan=False)
            ).encode()
        return url, headers, data

    def call(self, operation, **arguments):
        url, headers, data = self.prepare(operation, arguments)
        op = self.operations[operation]
        with self._lock:
            if self._last is not None:
                time.sleep(max(0, 1 / self.rate - (time.monotonic() - self._last)))
            self._last = time.monotonic()
        try:
            request = Request(url, data=data, headers=headers, method=op["method"])
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = response.read(MAX_RESPONSE + 1)
                if len(payload) > MAX_RESPONSE:
                    raise SiteError("Response size limit exceeded (4 MiB)")
                if not payload:
                    return None
                try:
                    result = json.loads(payload)
                except (ValueError, UnicodeError) as exc:
                    raise SiteError(
                        "Site returned a non-JSON response; re-record this endpoint"
                    ) from exc
                if op.get("graphql") and isinstance(result, dict) and result.get("errors"):
                    raise SiteError(
                        "GraphQL returned errors; check variables or refresh your session"
                    )
                return result
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status in (401, 403):
                raise SiteError(
                    "Session expired or access denied: re-record or refresh your cookie "
                    "and CSRF token"
                ) from None
            if 300 <= status < 400:
                raise SiteError(
                    "Redirect refused to protect credentials; re-record the final endpoint"
                ) from None
            if status == 429:
                raise SiteError("Site rate limit reached; lower --rate and try later") from None
            raise SiteError(f"Site returned HTTP {status}; re-record if the API changed") from None
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise SiteError(
                "Request failed or timed out; check the site URL and connection"
            ) from exc
