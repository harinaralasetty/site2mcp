"""Infer a sanitized, executable API manifest from normalized observations."""

import copy
import json
import re
from collections import defaultdict
from http.cookies import SimpleCookie
from urllib.parse import unquote

from .redact import collect, personal, sanitize, sensitive
from .schema import infer_schema


def snake(value):
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower()
    if not value or value[0].isdigit():
        value = "operation_" + value
    return value[:80]


def singular(value):
    if value.endswith("ies"):
        return value[:-3] + "y"
    return value[:-1] if value.endswith("s") and not value.endswith("ss") else value


def template_path(path, known=()):
    parts = path.split("/")
    notes = []
    used = set()
    for i, part in enumerate(parts):
        decoded = unquote(part)
        numeric = bool(re.fullmatch(r"\d+", decoded))
        uuid = bool(re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", decoded))
        opaque = bool(re.fullmatch(r"[0-9a-fA-F]{8,}", decoded))
        private = bool(decoded and (decoded in known or personal("", decoded)))
        private = private or (i > 0 and sensitive(unquote(parts[i - 1])) and bool(decoded))
        if numeric or uuid or opaque or private:
            previous = parts[i - 1] if i else "item"
            base = singular(snake(previous)).replace("_", " ").title().replace(" ", "")
            base = (base[:1].lower() + base[1:] if base else "item") + "Id"
            name, n = base, 2
            while name in used:
                name, n = f"{base}{n}", n + 1
            used.add(name)
            parts[i] = "{" + name + "}"
            notes.append(
                {
                    "parameter": name,
                    "confidence": "high" if numeric or uuid else "medium",
                    "reason": "numeric id"
                    if numeric
                    else "UUID"
                    if uuid
                    else "opaque or private segment",
                }
            )
    return "/".join(parts), notes


def query_value(value):
    if isinstance(value, list):
        return [query_value(v) for v in value]
    if value in ("true", "false"):
        return value == "true"
    if re.fullmatch(r"-?(0|[1-9]\d*)", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def _credential(location, name, kind="apiKey", path=None, **extra):
    label = "_".join(path or [name])
    return {
        "location": location,
        "name": name,
        "kind": kind,
        "env": "SITE2MCP_" + re.sub(r"[^A-Za-z0-9]", "_", label).upper(),
        **({"path": path} if path else {}),
        **extra,
    }


def _body_credentials(body, path=()):
    result = []
    if isinstance(body, dict):
        for key, value in body.items():
            child_path = (*path, key)
            if sensitive(key):
                result.append(_credential("body", key, path=list(child_path)))
            else:
                result.extend(_body_credentials(value, child_path))
    elif isinstance(body, list):
        for index, value in enumerate(body):
            result.extend(_body_credentials(value, (*path, str(index))))
    return result


def _without_credentials(schema):
    result = copy.deepcopy(schema)
    if result.get("type") == "object":
        props = result.get("properties", {})
        result["properties"] = {}
        for key, value in props.items():
            if sensitive(key):
                continue
            cleaned = _without_credentials(value)
            if (
                value.get("type") == "object"
                and value.get("properties")
                and not cleaned.get("properties")
            ):
                continue
            result["properties"][key] = cleaned
        required = [k for k in result.get("required", []) if k in result["properties"]]
        if required:
            result["required"] = required
        else:
            result.pop("required", None)
    if "items" in result:
        result["items"] = _without_credentials(result["items"])
    if "anyOf" in result:
        result["anyOf"] = [_without_credentials(s) for s in result["anyOf"]]
    return result


def _graphql(entry):
    body = entry["body"] if isinstance(entry["body"], dict) else entry["query"]
    document = body.get("query")
    if not isinstance(document, str):
        return None
    explicit = (
        "operationName" in body
        or entry["mimeType"] == "application/graphql"
        or entry["path"].rstrip("/").endswith("/graphql")
    )
    looks_graphql = re.search(r"^\s*(?:query|mutation|subscription)\b[^{}]*{", document)
    if not explicit and not looks_graphql and not document.lstrip().startswith("{"):
        return None
    # A deliberately narrow parser: refuse literals/multiple operations rather than
    # save personal data inside a query or accidentally execute another operation.
    document = re.sub(r"#[^\n]*", "", document).strip()
    if '"' in document or "'" in document:
        raise ValueError("GraphQL inline literals unsupported; record variables instead")
    if "=" in document or re.search(r"(?<![A-Za-z0-9_$])-?\d", document):
        raise ValueError("GraphQL inline/default values unsupported; use variables")
    lexical = re.findall(r"[A-Za-z_][A-Za-z_0-9]*|[{}():$]", document)
    parentheses = []
    for index, token in enumerate(lexical):
        if token == "(":
            parentheses.append(index == 2 and lexical[0] in ("query", "mutation", "subscription"))
        elif token == ")":
            if not parentheses:
                raise ValueError("GraphQL document is malformed")
            parentheses.pop()
        if parentheses and not parentheses[-1] and token == ":":
            if index + 1 >= len(lexical) or lexical[index + 1] != "$":
                raise ValueError("GraphQL argument literals unsupported; use variables")
    if parentheses:
        raise ValueError("GraphQL document is malformed")
    tokens = re.findall(r"[A-Za-z_][A-Za-z_0-9]*|[{}()]", document)
    depth = 0
    declarations = []
    for index, token in enumerate(tokens):
        if depth == 0 and token in ("query", "mutation", "subscription"):
            following = tokens[index + 1] if index + 1 < len(tokens) else ""
            name = following if re.fullmatch(r"[A-Za-z_]\w*", following) else ""
            declarations.append((token, name))
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
        if depth < 0:
            raise ValueError("GraphQL document is malformed")
    if depth != 0 or len(declarations) != 1:
        raise ValueError("GraphQL requires exactly one named operation")
    typ, name = declarations[0]
    if not name or typ == "subscription" or body.get("operationName", name) != name:
        raise ValueError("GraphQL operation name mismatch or unsupported subscription")
    variables = body.get("variables", {})
    if isinstance(variables, str):
        variables = json.loads(variables)
    if not isinstance(variables, dict):
        raise ValueError("GraphQL variables must be an object")
    return {"operationName": name, "document": document, "type": typ, "variables": variables}


def infer(capture, *, name="Recorded site", keep_examples=False):
    known = set()
    for e in capture.entries:
        for value in (
            e["body"],
            e["query"],
            e["responseBody"],
            e["headers"],
            e["responseHeaders"],
            e["cookies"],
            e["responseCookies"],
        ):
            known.update(collect(value, keep_examples=keep_examples))
        for headers in (e["headers"], e["responseHeaders"]):
            for key, value in headers.items():
                if key in ("cookie", "set-cookie"):
                    cookies = SimpleCookie()
                    try:
                        cookies.load(value)
                    except Exception:
                        continue
                    known.update(m.value for m in cookies.values())
                if key == "authorization":
                    known.add(value.split(" ", 1)[-1])
        known.update(e["cookies"].values())
        known.update(e["responseCookies"].values())
    groups = defaultdict(list)
    dropped = list(capture.dropped)
    for e in capture.entries:
        path, notes = template_path(e["path"], known)
        try:
            gql = _graphql(e)
        except (ValueError, TypeError):
            dropped.append({"reason": "unsupported or ambiguous GraphQL document", "count": 1})
            continue
        if gql and any(secret in gql["document"] for secret in known if len(secret) >= 3):
            dropped.append({"reason": "GraphQL document contains private values", "count": 1})
            continue
        groups[(e["origin"], path, e["method"], gql["operationName"] if gql else "")].append(
            (e, notes, gql)
        )
    operations = []
    names = set()
    for (origin, path, method, _), samples in sorted(groups.items()):
        entries = [s[0] for s in samples]
        gql = samples[0][2]
        resource_parts = [p for p in path.split("/") if p and not p.startswith("{")]
        resource = snake(resource_parts[-1] if resource_parts else "root")
        item = path.endswith("}")
        action = {
            "GET": "get" if item else "list",
            "HEAD": "head",
            "POST": "create",
            "PUT": "update",
            "PATCH": "update",
            "DELETE": "delete",
        }[method]
        op_name = (
            "graphql_" + snake(gql["operationName"])
            if gql
            else (action + "_" + (singular(resource) if item or method != "GET" else resource))
        )
        base, suffix = op_name, 2
        while op_name in names:
            op_name, suffix = f"{base}_{suffix}", suffix + 1
        names.add(op_name)
        credentials = []
        for e in entries:
            if e["cookies"] or "cookie" in e["headers"]:
                credentials.append(_credential("header", "Cookie", "cookie"))
            for key, value in e["headers"].items():
                if key == "cookie" or not sensitive(key):
                    continue
                kind = (
                    "bearer"
                    if key == "authorization" and value.lower().startswith("bearer ")
                    else ("csrf" if "csrf" in key or "xsrf" in key else "apiKey")
                )
                extra = {}
                if kind == "csrf":
                    extra["roundTrip"] = any(
                        value in list(old["responseCookies"].values())
                        or value in list(old["responseHeaders"].values())
                        or value in json.dumps(old["responseBody"])
                        for old in capture.entries
                    )
                credentials.append(_credential("header", key, kind, **extra))
            for key in e["query"]:
                if sensitive(key):
                    credentials.append(_credential("query", key))
            credentials.extend(_body_credentials(gql["variables"] if gql else e["body"]))
        credentials = list({json.dumps(c, sort_keys=True): c for c in credentials}.values())
        query_samples = [
            {
                k: query_value(v)
                for k, v in e["query"].items()
                if not sensitive(k) and not (gql and k in ("query", "variables", "operationName"))
            }
            for e in entries
        ]
        query_schema = infer_schema(query_samples, known=known)
        if gql:
            bodies = [s[2]["variables"] for s in samples]
        else:
            bodies = [e["body"] for e in entries if e["hasBody"]]
        body_schema = infer_schema(bodies, known=known) if bodies else None
        path_schema = {"type": "object", "properties": {}, "required": []}
        for param in re.findall(r"\{([^}]+)}", path):
            path_schema["properties"][param] = {"type": "string", "minLength": 1}
            path_schema["required"].append(param)
        input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
        required = []
        if path_schema["properties"]:
            input_schema["properties"]["path"] = path_schema
            required.append("path")
        if query_schema.get("properties"):
            input_schema["properties"]["query"] = query_schema
            if query_schema.get("required"):
                required.append("query")
        if body_schema is not None:
            key = "variables" if gql else "body"
            input_schema["properties"][key] = _without_credentials(body_schema)
            if (
                bodies
                and len(bodies) == len(entries)
                and (
                    input_schema["properties"][key].get("required")
                    or body_schema.get("type") != "object"
                )
            ):
                required.append(key)
        if required:
            input_schema["required"] = required
        responses = {}
        for status in sorted({e["status"] for e in entries}):
            body_samples = [
                e["responseBody"] for e in entries if e["status"] == status and e["hasResponse"]
            ]
            responses[str(status)] = (
                infer_schema(body_samples, known=known) if body_samples else None
            )
        pagination = sorted(
            {
                k
                for e in entries
                for k in e["query"]
                if k.lower()
                in {
                    "page",
                    "limit",
                    "offset",
                    "cursor",
                    "after",
                    "before",
                    "per_page",
                    "pagesize",
                    "page_size",
                }
            }
        )
        if any(
            isinstance(e["responseBody"], dict)
            and any(k in e["responseBody"] for k in ("next", "nextCursor", "hasMore"))
            for e in entries
        ):
            pagination.append("response continuation")
        read_only = gql["type"] == "query" if gql else method in ("GET", "HEAD")
        description = f"{method} {path}. Inferred from recorded traffic."
        if not read_only:
            description += " Changes data. Requires --allow-writes."
        op = {
            "name": op_name,
            "method": method,
            "path": path,
            "origin": origin,
            "description": description,
            "readOnly": read_only,
            "pathNotes": samples[0][1],
            "querySchema": query_schema,
            "bodySchema": body_schema,
            "bodyRequired": bool(gql) or bool(bodies) and len(bodies) == len(entries),
            "inputSchema": input_schema,
            "credentials": credentials,
            "responses": responses,
            "pagination": pagination,
            "mimeType": entries[0]["mimeType"] or "application/json",
            "samples": len(entries),
        }
        if gql:
            op["graphql"] = {k: v for k, v in gql.items() if k != "variables"}
        if keep_examples:
            op["examples"] = sanitize(
                {
                    "query": entries[0]["query"],
                    "body": entries[0]["body"],
                    "response": entries[0]["responseBody"],
                },
                keep_examples=True,
                known=known,
            )
        operations.append(op)
    return {
        "version": 1,
        "name": sanitize(name, known=known),
        "host": capture.host,
        "totalEntries": capture.total,
        "dropped": dropped,
        "operations": operations,
        "notes": [
            "Schemas, requiredness, enums and path templates are observational heuristics.",
            "No captured credentials are retained. Supply fresh credentials at runtime.",
        ],
    }
