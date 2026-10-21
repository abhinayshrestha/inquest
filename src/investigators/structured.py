"""Structured model calls with semantic validation and one repair, shared by the Main
and Local Investigators. The checks themselves live in the validation modules."""

from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from pydantic import BaseModel

from src.investigators import policy, prompts
from src.investigators.validation import problem_codes


def tokens_of(message: Any) -> int:
    usage = getattr(message, "usage_metadata", None)
    return int(usage.get("total_tokens", 0)) if usage else 0


async def structured_call[T](
    model: BaseChatModel,
    schema: type[BaseModel],
    messages: list[AnyMessage],
    check: Callable[[Any], tuple[T, list[str]]],
    calls_left: int,
    skip_repair: Callable[[Any], bool] = lambda _: False,
) -> tuple[T | None, list[str], int, int, list[str]]:
    """One structured call plus at most MAX_REPAIRS repairs. Returns the last cleaned
    result (already safe: check() drops what it rejects), the remaining problems, the
    model calls and tokens used, and the first attempt's problems (for the repair audit).
    `skip_repair(parsed)` marks a draft the caller will discard, which is not worth a
    repair call. Model exceptions propagate."""
    runnable = model.with_structured_output(schema, method="json_schema", include_raw=True)
    clean: T | None = None
    problems: list[str] = []
    first: list[str] | None = None
    calls = tokens = 0
    prompt = list(messages)
    for _ in range(1 + policy.MAX_REPAIRS):
        if calls >= calls_left:
            break
        out = await runnable.ainvoke(prompt)
        calls += 1
        tokens += tokens_of(out["raw"])
        if out["parsed"] is None:
            problems = [
                f"[schema] output did not match the JSON schema: {str(out['parsing_error'])[:300]}"
            ]
        else:
            clean, problems = check(out["parsed"])
        if first is None:
            first = problems
        if out["parsed"] is not None and skip_repair(out["parsed"]):
            break
        if not problems:
            break
        prompt = [
            *prompt,
            AIMessage(content=str(out["raw"].content)),
            HumanMessage(prompts.REPAIR.format(problems="\n".join(f"- {p}" for p in problems))),
        ]
    return clean, problems, calls, tokens, first or []


def audit_line(stage: str, first: list[str], final: list[str]) -> str | None:
    repaired = sorted(problem_codes(first) - problem_codes(final))
    unresolved = sorted(problem_codes(final))
    parts = [f"repaired {', '.join(repaired)}"] if repaired else []
    parts += [f"unresolved {', '.join(unresolved)}"] if unresolved else []
    return f"{stage}: {'; '.join(parts)}" if parts else None
