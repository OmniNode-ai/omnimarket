# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the standing-rulings register (OMN-24478).

A ruling is recorded and then, until this register, nothing read the running system against it. One
register row is: the ruling's stamp, one sentence of what must be true in the running system, a read
(a command and the host it runs on) with how its answer is judged, and the owner lane or ``none``.

The register is deployment data (the ``node_ruling_register_compute`` overlay). The effect node takes
each row's read and returns a ``ModelRulingReading``; the compute node judges the readings into a
``ModelRulingRegisterResult``. A read that did not run, ran with an exit code the row does not accept,
is older than the policy's bound or answers in a shape its judge cannot parse is UNREAD, never TRUE.
This module is the seam between the effect, the compute node and the throughput tick decision.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

_ID = r"^[a-z][a-z0-9-]{0,63}$"
_STAMP = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
LOOP_PREFIX = "ruling-not-running"


class EnumRulingVerdict(StrEnum):
    """TRUE: the read answered and the ruling holds. FALSE: it answered and the ruling does not hold. UNREAD: no usable answer."""

    TRUE = "TRUE"
    FALSE = "FALSE"
    UNREAD = "UNREAD"


class EnumRulingExpect(StrEnum):
    """How a read's answer is judged: exit code zero, empty output, output equal to a value, or an integer at most a bound."""

    EXIT_ZERO = "exit_zero"
    STDOUT_EMPTY = "stdout_empty"
    STDOUT_EQUALS = "stdout_equals"
    INT_AT_MOST = "int_at_most"


class EnumReadStatus(StrEnum):
    """OK: the command ran and exited with a code the row accepts. UNREAD: it did not run, timed out or exited with another code."""

    OK = "ok"
    UNREAD = "unread"


class ModelRulingExpect(BaseModel):
    """What a TRUE answer looks like. ``value`` is read by stdout_equals, ``bound`` by int_at_most."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumRulingExpect
    value: str = ""
    bound: int | None = None

    @model_validator(mode="after")
    def _kind_has_its_operand(self) -> ModelRulingExpect:
        if self.kind is EnumRulingExpect.STDOUT_EQUALS and not self.value:
            raise ValueError("stdout_equals names no value")
        if self.kind is EnumRulingExpect.INT_AT_MOST and self.bound is None:
            raise ValueError("int_at_most names no bound")
        return self


class ModelRulingRead(BaseModel):
    """A read: the argv to run, the host it runs on, the exit codes that mean it answered, and the judge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    argv: tuple[str, ...] = Field(min_length=1)
    ok_exit_codes: tuple[int, ...] = (0,)
    expect: ModelRulingExpect


class ModelRulingRow(BaseModel):
    """One standing ruling: its stamp, what must be true, how to read it, who owns it and what to do when it is not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(pattern=_ID)
    ruling: str = Field(pattern=_STAMP)
    must_be_true: str = Field(min_length=1)
    read: ModelRulingRead
    owner: str = Field(min_length=1)
    fix: str = Field(min_length=1)


class ModelRulingRegisterPolicy(BaseModel):
    """The ``node_ruling_register_compute`` overlay: the rows and how stale a reading may be."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_reading_age_seconds: int = Field(ge=1)
    rows: tuple[ModelRulingRow, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _row_ids_are_unique(self) -> ModelRulingRegisterPolicy:
        ids = [row.row_id for row in self.rows]
        if len(ids) != len(set(ids)):
            raise ValueError("the register lists a row more than once")
        return self


class ModelRulingReading(BaseModel):
    """What one read saw. ``detail`` says why a reading is UNREAD; it is empty for an OK reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(pattern=_ID)
    status: EnumReadStatus
    exit_code: int | None = None
    stdout: str = ""
    read_at: AwareDatetime
    detail: str = ""


class ModelRulingVerdict(BaseModel):
    """One row's judgement; ``why`` and ``fix`` are the tick line's cells."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(pattern=_ID)
    ruling: str = Field(pattern=_STAMP)
    owner: str = Field(min_length=1)
    verdict: EnumRulingVerdict
    detail: str
    why: str
    fix: str

    @property
    def loop(self) -> str:
        """The loop the tick names: ``ruling-not-running:<row>``."""
        return f"{LOOP_PREFIX}:{self.row_id}"

    def line(self) -> str:
        """The tick's line for a row that is not TRUE; a TRUE row has none."""
        return f"MISSING {self.loop} | {self.why} | FIX: {self.fix}"


class ModelRulingReadRequest(BaseModel):
    """What the effect takes: the policy whose reads to run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    policy: ModelRulingRegisterPolicy


class ModelRulingReadResult(BaseModel):
    """What the effect returns: one reading per row, in row order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    readings: tuple[ModelRulingReading, ...]


class ModelRulingRegisterRequest(BaseModel):
    """What the compute node judges: the policy, the readings taken and the clock."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    now: AwareDatetime
    policy: ModelRulingRegisterPolicy
    readings: tuple[ModelRulingReading, ...]


class ModelRulingRegisterResult(BaseModel):
    """The register reading: a verdict per row, in row order, and the counts the tick prints."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    passed: bool
    verdicts: tuple[ModelRulingVerdict, ...]
    true_count: int = Field(ge=0)
    false_count: int = Field(ge=0)
    unread_count: int = Field(ge=0)

    def lines(self) -> tuple[str, ...]:
        """One MISSING line per FALSE or UNREAD row."""
        return tuple(
            verdict.line()
            for verdict in self.verdicts
            if verdict.verdict is not EnumRulingVerdict.TRUE
        )


__all__ = [
    "LOOP_PREFIX",
    "EnumReadStatus",
    "EnumRulingExpect",
    "EnumRulingVerdict",
    "ModelRulingExpect",
    "ModelRulingRead",
    "ModelRulingReadRequest",
    "ModelRulingReadResult",
    "ModelRulingReading",
    "ModelRulingRegisterPolicy",
    "ModelRulingRegisterRequest",
    "ModelRulingRegisterResult",
    "ModelRulingRow",
    "ModelRulingVerdict",
]
