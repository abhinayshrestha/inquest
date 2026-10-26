"""Response blocks: how a validated answer is presented (D023).

Version 1. Blocks are produced only by deterministic backend code (services.presentation)
from validated answers and recorded evidence; never by the model. Every data-bearing
block (metric, table, chart) carries the evidence refs it was projected from, its
observation window and units. All models forbid unknown fields and non-finite numbers,
so a tampered or malformed document fails validation instead of rendering.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

PRESENTATION_VERSION = 1

EvidenceRef = Annotated[str, Field(pattern=r"^ev_[0-9a-f]{8}$")]
Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,23}$")]
Label = Annotated[str, Field(min_length=1, max_length=120)]
Unit = Annotated[str, Field(min_length=1, max_length=24)]
Note = Annotated[str, Field(max_length=400)]
Number = Annotated[float, Field(allow_inf_nan=False)]
Cell = Annotated[str, Field(max_length=200)] | Number | None


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Window(_Block):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _ordered(self) -> "Window":
        if self.end < self.start:
            raise ValueError("window ends before it starts")
        return self


class MarkdownBlock(_Block):
    type: Literal["markdown"] = "markdown"
    markdown: str = Field(min_length=1, max_length=8000)


class CodeBlock(_Block):
    type: Literal["code"] = "code"
    language: str = Field(default="", pattern=r"^[a-z0-9+#-]{0,20}$")
    code: str = Field(min_length=1, max_length=4000)


class MetricItem(_Block):
    label: Label
    stat: Literal[
        "mean", "median", "min", "max", "last", "total", "count", "rate", "p50", "p95", "p99"
    ]
    value: Number
    unit: Unit | None
    # Observations behind the value (spans, log records, metric points); None when the
    # source does not say.
    sample_count: int | None = Field(ge=0)
    step_seconds: int | None = Field(default=None, ge=1)
    window: Window
    evidence_ref: EvidenceRef


class MetricBlock(_Block):
    type: Literal["metric"] = "metric"
    title: Label
    items: list[MetricItem] = Field(min_length=1, max_length=8)


class Column(_Block):
    key: Key
    label: Label
    kind: Literal["text", "number"]
    unit: Unit | None = None


class TableBlock(_Block):
    type: Literal["table"] = "table"
    title: Label
    columns: list[Column] = Field(min_length=1, max_length=8)
    rows: list[dict[Key, Cell]] = Field(min_length=1, max_length=50)
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=8)
    window: Window
    note: Note | None = None

    @model_validator(mode="after")
    def _cells_match_columns(self) -> "TableBlock":
        kinds = {c.key: c.kind for c in self.columns}
        for row in self.rows:
            for key, value in row.items():
                if key not in kinds:
                    raise ValueError(f"row has unknown column {key!r}")
                if value is not None and (kinds[key] == "number") != (not isinstance(value, str)):
                    raise ValueError(f"column {key!r} holds the wrong kind of value")
        return self


class Series(_Block):
    key: Key
    label: Label
    unit: Unit | None


class ChartPoint(_Block):
    # A category name (bar) or an ISO timestamp (line).
    x: str = Field(min_length=1, max_length=120)
    values: dict[Key, Number | None]
    # Observations behind this point (e.g. spans in this group); None when unknown.
    sample_count: int | None = Field(default=None, ge=0)


class ChartBlock(_Block):
    type: Literal["chart"] = "chart"
    kind: Literal["bar", "line"]
    title: Label
    x_label: Label
    series: list[Series] = Field(min_length=1, max_length=4)
    points: list[ChartPoint] = Field(min_length=2, max_length=120)
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=8)
    window: Window
    note: Note | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "ChartBlock":
        keys = {s.key for s in self.series}
        for point in self.points:
            if not set(point.values) <= keys:
                raise ValueError("point has values for unknown series")
            if self.kind == "line":
                datetime.fromisoformat(point.x.replace("Z", "+00:00"))
        if self.kind == "line" and [p.x for p in self.points] != sorted(p.x for p in self.points):
            raise ValueError("time series points must be in time order")
        return self


class EvidenceItem(_Block):
    ref: EvidenceRef
    title: str = Field(min_length=1, max_length=255)
    evidence_type: str = Field(min_length=1, max_length=40)
    summary: str = Field(max_length=400)
    window: Window | None


class EvidenceBlock(_Block):
    type: Literal["evidence"] = "evidence"
    items: list[EvidenceItem] = Field(min_length=1, max_length=12)


Block = Annotated[
    MarkdownBlock | CodeBlock | MetricBlock | TableBlock | ChartBlock | EvidenceBlock,
    Field(discriminator="type"),
]


class Presentation(_Block):
    version: Literal[1] = PRESENTATION_VERSION
    turn_id: uuid.UUID
    generated_at: AwareDatetime
    blocks: list[Block] = Field(min_length=1, max_length=16)
