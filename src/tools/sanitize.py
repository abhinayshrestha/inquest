import json
import re
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from src.integrations.signoz.normalize import clip, json_size, redact

MAX_INVALID_ARGUMENT_BYTES = 8_192
MAX_ARGUMENT_STRING_CHARS = 200
MAX_ARGUMENT_ITEMS = 20
MAX_ARGUMENT_DEPTH = 4
MAX_VALIDATION_ERRORS = 5
SECRET_MARK = "[redacted]"


def scrub(text: str, secrets: Iterable[str], limit: int) -> str:
    """Last pass over any string the executor persists or returns."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, SECRET_MARK)
    text = re.sub(r"\s+", " ", redact(text)[0]).strip()
    return clip(text, limit)[0]


def scrub_json(value: Any, secrets: Iterable[str]) -> Any:
    # Adapter output is already redacted; this only catches a configured secret value
    # echoed back inside telemetry, which pattern-based redaction cannot know about.
    present = [s for s in secrets if s]
    if not present:
        return value
    text = json.dumps(value, ensure_ascii=False)
    found = False
    for secret in present:
        # Match the JSON-encoded form so the replacement keeps string escaping valid.
        encoded = json.dumps(secret, ensure_ascii=False)[1:-1]
        found |= encoded in text
        text = text.replace(encoded, SECRET_MARK)
    return json.loads(text) if found else value


def _shrink(value: Any, depth: int) -> Any:
    if depth >= MAX_ARGUMENT_DEPTH:
        return "…"
    if isinstance(value, dict):
        items = list(value.items())[:MAX_ARGUMENT_ITEMS]
        return {clip(str(k), 100)[0]: _shrink(v, depth + 1) for k, v in items}
    if isinstance(value, list | tuple):
        return [_shrink(v, depth + 1) for v in value[:MAX_ARGUMENT_ITEMS]]
    if isinstance(value, str):
        return clip(redact(value)[0], MAX_ARGUMENT_STRING_CHARS)[0]
    if value is None or isinstance(value, bool | int | float):
        return value
    return f"<{type(value).__name__}>"


def sanitize_arguments(arguments: Any, secrets: Iterable[str]) -> dict[str, Any]:
    """Bounded, redacted copy of arguments that failed validation, for the audit row."""
    shrunk = _shrink(arguments, 0)
    if not isinstance(shrunk, dict):
        shrunk = {"_arguments": shrunk}
    try:
        shrunk = scrub_json(shrunk, secrets)
        if json_size(shrunk) <= MAX_INVALID_ARGUMENT_BYTES:
            return shrunk
    except (TypeError, ValueError):
        pass
    return {"_omitted": f"arguments exceeded {MAX_INVALID_ARGUMENT_BYTES} bytes"}


def validation_message(exc: ValidationError) -> str:
    # loc and msg only: pydantic's `input` echoes the submitted values.
    parts = [
        f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}"
        for e in exc.errors(include_url=False, include_input=False)[:MAX_VALIDATION_ERRORS]
    ]
    return "; ".join(parts)
