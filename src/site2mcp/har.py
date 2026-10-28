"""Bounded HAR 1.2 reader. No network access or raw input in errors."""

import base64
import binascii
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

MAX_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 50_000
MAX_BODY = 1024 * 1024
STATIC = re.compile(r"\.(js|css|png|jpe?g|gif|svg|ico|woff2?|ttf|map|mp4|webp|pdf)(/|$)", re.I)
ANALYTICS = re.compile(
    r"(^|[./_-])(analytics|telemetry|metrics|collect|beacon|doubleclick)([./_-]|$)", re.I
)


class HarError(ValueError):
    """Safe user-facing capture error."""


@dataclass
class Capture:
    entries: list[dict]
    host: str
    dropped: list[dict]
    total: int


def _depth(value, limit):
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise HarError("HAR contains invalid nonfinite JSON numbers")
        if depth > limit:
            raise HarError("HAR nesting depth limit exceeded")
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)


def pairs(items):
    if not isinstance(items, list):
        raise HarError("HAR headers, cookies and parameters must be arrays")
    result = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise HarError("HAR named value is malformed")
        if not isinstance(item.get("value"), str):
            raise HarError("HAR named value must be a string")
        result.append((item["name"], item["value"]))
    return result


def multi(pairs_list):
    result = {}
    for key, value in pairs_list:
        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(value)
        else:
            result[key] = value
    return result


def _oversized(content):
    text = content.get("text") if isinstance(content, dict) else None
    return isinstance(text, str) and len(text.encode()) > MAX_BODY


def _without_text(content):
    # An oversized body is skipped, not parsed: one large bundle must not abort the whole capture.
    return {k: v for k, v in content.items() if k != "text"} if _oversized(content) else content


def _body(content, *, form=False):
    if not isinstance(content, dict):
        raise HarError("HAR body metadata must be an object")
    text = content.get("text")
    mime = str(content.get("mimeType", "")).split(";")[0].lower()
    if text is not None and not isinstance(text, str):
        raise HarError("HAR body text must be a string")
    if text is not None and len(text.encode()) > MAX_BODY:
        raise HarError("HAR body size limit exceeded")
    if content.get("encoding") == "base64" and text is not None:
        try:
            raw = base64.b64decode(text, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HarError("HAR base64 body is invalid") from exc
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            return None, False, mime  # binary content (images, fonts) carries no API schema
    if form and mime == "application/x-www-form-urlencoded":
        return (
            multi(
                pairs(content["params"])
                if "params" in content
                else parse_qsl(text or "", keep_blank_values=True)
            ),
            True,
            mime,
        )
    if form and mime == "multipart/form-data":
        raise HarError("Multipart file uploads are unsupported; remove those entries")
    if text is None or text == "":
        return None, False, mime
    if "json" in mime or text.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(text)
            _depth(parsed, 64)
            return parsed, True, mime or "application/json"
        except (ValueError, RecursionError) as exc:
            raise HarError("HAR JSON body is invalid") from exc
    if form and mime == "application/graphql":
        return {"query": text}, True, mime
    return None, False, mime


def load_har(path, *, host=None, max_bytes=MAX_BYTES, max_entries=MAX_ENTRIES, max_depth=64):
    try:
        with Path(path).open("rb") as source:
            data = source.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise HarError("HAR size limit exceeded (default 32 MiB)")
        raw = json.loads(data.decode("utf-8-sig"))
        _depth(raw, max_depth)
        if not isinstance(raw, dict) or not isinstance(raw.get("log"), dict):
            raise HarError("HAR must contain a log object")
        records = raw["log"].get("entries")
        if not isinstance(records, list):
            raise HarError("HAR log.entries must be an array")
        if len(records) > max_entries:
            raise HarError("HAR entry limit exceeded (default 50000)")
        normalized = []
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise HarError(f"HAR entry {index} must be an object")
            req, res = record.get("request"), record.get("response")
            if not isinstance(req, dict) or not isinstance(res, dict):
                raise HarError(f"HAR entry {index} needs request and response objects")
            url = req.get("url")
            if not isinstance(url, str):
                raise HarError(f"HAR entry {index} needs a URL")
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
                raise HarError(f"HAR entry {index} needs an HTTP(S) URL without userinfo")
            _ = parsed.port
            method = req.get("method", "").upper()
            if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
                raise HarError(f"HAR entry {index} has an unsupported method")
            if not isinstance(res.get("status"), int) or not -1 <= res["status"] <= 599:
                raise HarError(f"HAR entry {index} needs a valid status")
            # Chrome and Playwright write -1 for a request that never got a response.
            status = max(res["status"], 0)
            post, content = req.get("postData", {}), res.get("content", {})
            oversized = _oversized(post) or _oversized(content)
            body, has_body, mime = _body(_without_text(post), form=True)
            response_body, has_response, response_mime = _body(_without_text(content))
            _depth(body, max_depth)
            _depth(response_body, max_depth)
            headers = dict((k.lower(), v) for k, v in pairs(req.get("headers", [])))
            response_headers = dict((k.lower(), v) for k, v in pairs(res.get("headers", [])))
            # URL query is authoritative. Some sanitized exports retain only queryString.
            query = multi(
                parse_qsl(parsed.query, keep_blank_values=True) or pairs(req.get("queryString", []))
            )
            normalized.append(
                {
                    "origin": f"{parsed.scheme}://{parsed.netloc}",
                    "host": parsed.hostname,
                    "path": parsed.path or "/",
                    "method": method,
                    "query": query,
                    "headers": headers,
                    "cookies": dict(pairs(req.get("cookies", []))),
                    "body": body,
                    "hasBody": has_body,
                    "mimeType": mime,
                    "responseBody": response_body,
                    "hasResponse": has_response,
                    "responseMime": response_mime,
                    "responseHeaders": response_headers,
                    "responseCookies": dict(pairs(res.get("cookies", []))),
                    "status": status,
                    "oversized": oversized,
                    "resource": record.get(
                        "_resourceType", record.get("_cause", {}).get("type", "")
                    ),
                }
            )
        chosen = host.lower() if host else None
        if chosen and ("/" in chosen or "@" in chosen):
            raise HarError("--host must be a hostname, optionally with a port")
        if not chosen and normalized:
            documents = [e["host"] for e in normalized if e["resource"] == "document"]
            counts = Counter(
                e["host"]
                for e in normalized
                if not STATIC.search(e["path"]) and not ANALYTICS.search(e["host"] + e["path"])
            )
            chosen = (
                documents[0]
                if documents
                else (counts.most_common(1)[0][0] if counts else normalized[0]["host"])
            )
        dropped = Counter()
        kept = []
        for e in normalized:
            if e["host"] != chosen and urlsplit(e["origin"]).netloc != chosen:
                reason = "third-party host"
            elif STATIC.search(e["path"]):
                reason = "static asset"
            elif ANALYTICS.search(e["host"] + e["path"]):
                reason = "analytics"
            elif e["method"] == "OPTIONS":
                reason = "preflight"
            elif e["oversized"]:
                reason = "body over 1 MiB"
            elif not (
                e["resource"] in ("xhr", "fetch", "xmlhttprequest")
                or "json" in e["responseMime"]
                or e["hasBody"]
            ):
                reason = "non-API traffic"
            elif e["status"] == 0:
                reason = "incomplete response"
            else:
                kept.append(e)
                continue
            # Do not copy raw hosts or paths into diagnostics.
            dropped[reason] += 1
        return Capture(
            kept,
            chosen or "",
            [{"reason": k, "count": v} for k, v in sorted(dropped.items())],
            len(records),
        )
    except HarError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError, RecursionError) as exc:
        raise HarError("Cannot read HAR: invalid file, encoding, URL or JSON structure") from exc
