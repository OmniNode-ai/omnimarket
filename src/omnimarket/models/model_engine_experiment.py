# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure engine-ladder experiment rows shared by placement and throughput (OMN-21396)."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from omnimarket.models.model_engine_ladder import ladder_of

MEASURED_ONLY_ENGINES: tuple[str, ...] = ("antigravity",)
HARNESS_EXPERIMENT_ENGINES: tuple[str, ...] = ("local-agent", *MEASURED_ONLY_ENGINES)
CLAUDE_RUNGS = ("sonnet", "opus")
LOW_RISK_CLASSES = ("doc", "test", "lint", "small-fix")
ESCALATING_OUTCOMES = ("failed", "blocked-own-work", "checks-red")
SUCCESS_OUTCOME = "done"
EXPERIMENT_KIND = "experiment"
NO_ENGINE = "none"
# OMN-21396: measured refusals cover writes outside lane roots, denied ps/sysctl (or
# a Linux PID namespace), the runner's never-approval Linear policy, and nested
# sandbox-exec/bwrap denied by sandbox_apply or namespace creation, regardless of roots.
CODEX_UNGRANTABLE_CAPABILITIES: tuple[str, ...] = (
    "host-paths-write",
    "host-process-read",
    "linear-write",
    "nested-sandbox",
)
# The readiness gates each rung must pass beyond the ROUTE rules, in the order they are judged.
RUNG_GATES: dict[str, tuple[str, ...]] = {
    "code-edit": (
        "landing-worker",
        "risk-class",
        "no-writable-or-check",
        "no-local-slot",
    ),
    "glm": ("landing-worker", "risk-class"),
    "codex": ("sandbox-capability",),
    "sonnet": (),
    "opus": (),
}
# The rungs no Claude usage-limit or auth-expired mark applies to.
QUOTA_FREE_RUNGS = frozenset({"code-edit", "glm"})


class ModelEngineExperimentRow(BaseModel):
    """One finished engine attempt, ready for a ledger STATUS row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    ticket: str | None = None
    attempt: int = Field(ge=1)
    engine: str
    model: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    failure_cause: str = "none"
    duration_s: int = Field(ge=0)
    next_engine: str = NO_ENGINE
    at: str | None = None
    harness: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    task_class: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    configuration: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )
    item_key: str | None = Field(
        default=None, min_length=1, exclude_if=lambda value: value is None
    )

    @field_validator("engine")
    @classmethod
    def validate_engine(cls, value: str, info: ValidationInfo) -> str:
        if value not in (*ladder_of(info).rungs, *HARNESS_EXPERIMENT_ENGINES):
            raise ValueError("engine must be a placement or harness experiment engine")
        return value

    @field_validator("next_engine")
    @classmethod
    def validate_next_engine(cls, value: str, info: ValidationInfo) -> str:
        if value != NO_ENGINE and value not in ladder_of(info).rungs:
            raise ValueError("next_engine must be none or a rung of the engine ladder")
        return value

    @field_validator("failure_cause")
    @classmethod
    def sanitise_failure_cause(cls, value: str) -> str:
        return re.sub(r"\s+", "-", value.replace("|", "/"))

    @field_validator(
        "lane",
        "ticket",
        "model",
        "outcome",
        "harness",
        "task_class",
        "configuration",
        "item_key",
    )
    @classmethod
    def validate_cell(cls, value: str | None) -> str | None:
        if value is not None and (" | " in value or "\n" in value or "\r" in value):
            raise ValueError("ledger values must fit in one cell on one line")
        return value

    @field_validator("at")
    @classmethod
    def validate_at(cls, value: str | None) -> str | None:
        if value is not None:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
                raise ValueError("at must be YYYY-MM-DDTHH:MM:SSZ")
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return value

    def cells(self) -> list[str]:
        """The ledger cells, with lane and ticket supplied separately by its caller."""
        return [
            f"kind={EXPERIMENT_KIND}",
            f"attempt={self.attempt}",
            f"engine={self.engine}",
            f"model={self.model}",
            f"outcome={self.outcome}",
            f"failure_cause={self.failure_cause}",
            f"duration_s={self.duration_s}",
            f"next_engine={self.next_engine}",
        ] + [
            f"{key}={getattr(self, key)}"
            for key in ("harness", "task_class", "configuration", "item_key")
            if getattr(self, key) is not None
        ]

    def text(self) -> str:
        """One short human-readable description of the attempt and its next rung."""
        cause = f": {self.failure_cause}" if self.failure_cause != "none" else ""
        next_step = (
            "no next engine"
            if self.next_engine == NO_ENGINE
            else f"next {self.next_engine}"
        )
        return f"attempt {self.attempt} on {self.engine} ({self.model}) {self.outcome}{cause}; {next_step}"


def format_experiment_line(row: ModelEngineExperimentRow, at: str) -> str:
    """Format a STATUS row without writing to the ledger."""
    return (
        f"{at} | STATUS | lane={row.lane} | "
        + (f"ticket={row.ticket} | " if row.ticket else "")
        + " | ".join(row.cells())
        + f" | {row.text()}"
    )


def parse_experiment_rows(ledger_text: str) -> list[ModelEngineExperimentRow]:
    """Read valid experiment STATUS rows, ignoring unrelated cells and malformed lines."""
    rows: list[ModelEngineExperimentRow] = []
    fields = set(ModelEngineExperimentRow.model_fields) - {"at"}
    for line in ledger_text.splitlines():
        cells = line.split(" | ")
        if (
            len(cells) < 3
            or cells[1] != "STATUS"
            or f"kind={EXPERIMENT_KIND}" not in cells[2:]
        ):
            continue
        values = dict(
            cell.split("=", 1)
            for cell in cells[2:]
            if "=" in cell and cell.split("=", 1)[0] in fields
        )
        try:
            rows.append(
                ModelEngineExperimentRow.model_validate({**values, "at": cells[0]})
            )
        except ValidationError:
            continue
    return rows
