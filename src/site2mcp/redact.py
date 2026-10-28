"""Conservative, recursive sanitization before anything reaches disk or a model."""

import json
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"
SECRET = re.compile(
    r"password|passwd|pwd|secret|token|authorization|cookie|api.?key|"
    r"(^|[_-])key($|[_-])|private.?key|credential|session|csrf|xsrf",
    re.I,
)
PERSONAL = re.compile(
    r"email|phone|mobile|(^|[_-])name$|first.?name|last.?name|full.?name|"
    r"display.?name|user.?name|given.?name|family.?name|address",
    re.I,
)
EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
PHONE = re.compile(r"\+?\d[\d ()-]{8,}\d")


def sensitive(name: str) -> bool:
    return bool(SECRET.search(name))


def personal(name: str, value=None) -> bool:
    return bool(
        PERSONAL.search(name)
        or (isinstance(value, str) and (EMAIL.fullmatch(value) or PHONE.fullmatch(value)))
    )


def sanitize(value, *, keep_examples=False, known=(), name=""):
    if sensitive(name) or (not keep_examples and personal(name, value)):
        return REDACTED
    if isinstance(value, (str, int, float, bool)) and str(value) in known:
        return REDACTED
    if isinstance(value, dict):
        return {
            (
                f"redacted_field_{index}"
                if k in known or (not keep_examples and personal("", k))
                else k
            ): sanitize(v, keep_examples=keep_examples, known=known, name=k)
            for index, (k, v) in enumerate(value.items())
        }
    if isinstance(value, list):
        return [sanitize(v, keep_examples=keep_examples, known=known, name=name) for v in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                embedded = json.loads(value)
                return json.dumps(sanitize(embedded, keep_examples=keep_examples, known=known))
            except (ValueError, RecursionError):
                pass
        if value.startswith(("http://", "https://")):
            try:
                url = urlsplit(value)
                query = [
                    (key, sanitize(item, keep_examples=keep_examples, known=known, name=key))
                    for key, item in parse_qsl(url.query, keep_blank_values=True)
                ]
                value = urlunsplit((url.scheme, url.netloc, url.path, urlencode(query), ""))
            except ValueError:
                return REDACTED
        for secret in sorted(known, key=len, reverse=True):
            if secret and len(secret) >= 3:
                value = value.replace(secret, REDACTED)
                value = value.replace(unquote(secret), REDACTED)
    return value


def collect(value, *, name="", keep_examples=False):
    """Gather sensitive values to catch copies under innocent field names."""
    found = set()
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                found.update(collect(json.loads(value), keep_examples=keep_examples))
            except (ValueError, RecursionError):
                pass
        if value.startswith(("http://", "https://")):
            try:
                for key, item in parse_qsl(urlsplit(value).query, keep_blank_values=True):
                    found.update(collect(item, name=key, keep_examples=keep_examples))
            except ValueError:
                pass
    if sensitive(name) or (not keep_examples and personal(name, value)):

        def leaves(item):
            if isinstance(item, dict):
                for child in item.values():
                    leaves(child)
            elif isinstance(item, list):
                for child in item:
                    leaves(child)
            elif isinstance(item, (str, int, float, bool)) and str(item):
                found.add(str(item))

        leaves(value)
    if isinstance(value, dict):
        for key, item in value.items():
            if not keep_examples and personal("", key):
                found.add(key)
            found.update(collect(item, name=key, keep_examples=keep_examples))
    elif isinstance(value, list):
        for item in value:
            found.update(collect(item, name=name, keep_examples=keep_examples))
    return found
