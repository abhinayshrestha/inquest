import asyncio
import inspect
from typing import Any

import pytest
from pydantic import ValidationError

from src.integrations import signoz
from src.integrations.signoz.schemas import LogSearchRequest, TraceStatsRequest
from src.tools import TOOLS, list_tools
from src.tools.executor import _uncancellable
from src.tools.sanitize import sanitize_arguments, scrub, scrub_json, validation_message

SECRET = "sz-test-key-0123456789abcdef"


def test_registry_covers_every_query_operation():
    operations = {
        name
        for name in signoz.__all__
        if inspect.iscoroutinefunction(getattr(signoz, name)) and name != "check_health"
    }
    assert {spec.run.__name__ for spec in TOOLS.values()} == operations
    for name, spec in TOOLS.items():
        assert name == f"signoz.{spec.run.__name__}"
        assert spec.run.__module__ == "src.integrations.signoz.operations"


def _property_names(schema: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        names |= set((schema.get("properties") or {}).keys())
        for value in schema.values():
            names |= _property_names(value)
    elif isinstance(schema, list):
        for value in schema:
            names |= _property_names(value)
    return names


def test_tool_schemas_expose_no_context_filter_or_scope():
    for descriptor in list_tools():
        names = _property_names(descriptor.input_schema)
        assert (
            not {"filter", "scope", "investigation_id", "assignment_id", "environment_id"} & names
        )
        assert all(not n.endswith("_id") for n in names - {"trace_id"}), descriptor.name


def test_tool_schemas_forbid_extra_fields():
    for descriptor in list_tools():
        assert descriptor.input_schema.get("additionalProperties") is False, descriptor.name


def test_validation_message_never_echoes_input():
    with pytest.raises(ValidationError) as exc:
        LogSearchRequest.model_validate(
            {"time_range": {"start": "x", "end": SECRET}, "password": SECRET, "limit": 999}
        )
    message = validation_message(exc.value)
    assert SECRET not in message
    assert "limit" in message and "password" in message


def test_sanitize_arguments_is_bounded_and_redacted():
    raw = {
        "query": "password=hunter2 " + "x" * 500,
        "key": SECRET,
        "nested": {"a": {"b": {"c": {"d": "deep"}}}},
        "many": list(range(100)),
        "obj": object(),
    }
    clean = sanitize_arguments(raw, [SECRET])
    text = str(clean)
    assert "hunter2" not in text and SECRET not in text
    assert len(clean["query"]) <= 200
    assert len(clean["many"]) == 20
    assert clean["nested"]["a"]["b"]["c"] == "…"
    assert clean["obj"] == "<object>"
    assert sanitize_arguments("not a dict", []) == {"_arguments": "not a dict"}
    # Item caps alone still allow 20 x 20 x 200 chars; the byte cap catches that.
    assert "_omitted" in sanitize_arguments({f"k{i}": ["x" * 200] * 20 for i in range(20)}, [])


def test_scrub_removes_secrets_and_credential_patterns():
    text = scrub(f"bad key {SECRET}\n token=abc123 Bearer abcdefghijkl", [SECRET], 2_000)
    assert SECRET not in text and "abc123" not in text and "abcdefghijkl" not in text
    assert "\n" not in text
    assert len(scrub("x" * 5_000, [], 100)) == 100


def test_scrub_json_replaces_echoed_secret_inside_values():
    value = {"groups": [{"group": f"GET /k/{SECRET}"}], "quote": 'a"b'}
    clean = scrub_json(value, [SECRET])
    assert SECRET not in str(clean)
    assert clean["quote"] == 'a"b'
    assert scrub_json(value, []) is value


def test_request_models_reject_free_form_filters():
    with pytest.raises(ValidationError):
        TraceStatsRequest.model_validate(
            {"time_range": {"start": "2026-10-14T14:00:00Z", "end": "2026-10-14T15:00:00Z"},
             "filter": "service.name = 'other'"}
        )  # fmt: skip


async def test_uncancellable_completes_work_before_reraising():
    done = asyncio.Event()

    async def bookkeeping() -> str:
        await asyncio.sleep(0.05)
        done.set()
        return "written"

    async def caller() -> tuple[str, bool]:
        return await _uncancellable(bookkeeping())

    task = asyncio.create_task(caller())
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()  # repeated cancellation must not abandon the write either
    result, cancelled = await task
    assert done.is_set()
    assert (result, cancelled) == ("written", True)


async def test_uncancellable_reports_no_cancellation():
    async def work() -> int:
        return 7

    assert await _uncancellable(work()) == (7, False)
