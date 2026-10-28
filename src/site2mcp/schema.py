"""JSON Schema inference. Constraints describe observations, not API guarantees."""

import re
from datetime import datetime
from uuid import UUID

from .redact import personal, sensitive


def string_format(value):
    try:
        UUID(value)
        return "uuid"
    except (ValueError, AttributeError):
        pass
    if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        return "email"
    if re.match(r"^\d{4}-\d\d-\d\dT", value):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return "date-time"
        except ValueError:
            pass
    return None


def kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "string"


def infer_schema(samples, name="", *, known=()):
    if not samples:
        return {}
    kinds = {kind(sample) for sample in samples}
    if kinds <= {"number", "integer"}:
        kinds = {"number"} if "number" in kinds else {"integer"}
    if len(kinds) > 1:
        return {
            "anyOf": [
                infer_schema([s for s in samples if kind(s) == k], name, known=known)
                for k in sorted(kinds)
            ]
        }
    typ = next(iter(kinds))
    schema = {"type": typ}
    if typ == "object":
        keys = sorted({key for sample in samples for key in sample})
        if any(personal("", key) or key in known for key in keys):
            schema["additionalProperties"] = infer_schema(
                [value for sample in samples for value in sample.values()], name, known=known
            )
            return schema
        schema["properties"] = {
            key: infer_schema([s[key] for s in samples if key in s], key, known=known)
            for key in keys
        }
        required = [key for key in keys if all(key in sample for sample in samples)]
        if required:
            schema["required"] = required
    elif typ == "array":
        schema["items"] = infer_schema(
            [item for sample in samples for item in sample], name, known=known
        )
    elif typ == "string":
        formats = {string_format(s) for s in samples}
        if len(formats) == 1 and None not in formats:
            schema["format"] = next(iter(formats))
        values = sorted(set(samples))
        safe = not sensitive(name) and not any(personal(name, s) for s in values)
        safe = safe and not any(secret in s for secret in known if secret for s in values)
        # Repetition is evidence for a small vocabulary; a single sample is not an enum.
        if safe and not formats - {None} and 1 < len(values) <= 8 and len(samples) > len(values):
            if all(len(s) <= 40 for s in values):
                schema["enum"] = values
    return schema
