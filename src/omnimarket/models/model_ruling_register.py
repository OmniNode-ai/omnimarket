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

One broken row is refused as data, never the whole policy: a row that fails validation, repeats a row
id or runs outside ``allowed_commands`` becomes a ``ModelRulingRefusedRow`` whose reason is built
from the validation error's fields and messages only, so the row's own values leak nowhere. The
compute node reports every refused row UNREAD; the policy refuses only when nothing at all is listed
or the schema ceiling is passed. This module is the seam between the effect, the compute node and the
throughput tick decision.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from enum import StrEnum

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

_ID = r"^[a-z][a-z0-9-]{0,63}$"
_STAMP = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
#: A refused row may carry no stamp of its own (the raw one was malformed), so the empty
#: string stands for ``no ruling stamp`` beside the stamp pattern itself.
_STAMP_OR_EMPTY = r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)?$"
#: How many rows (valid plus refused) an overlay may list. A schema ceiling, not a deployment value.
MAX_REGISTER_ROWS = 64
#: A refused row's reason is one printable line of at most this many characters.
_REASON_CHARS = 160
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

    @model_validator(mode="after")
    def _exit_zero_names_a_false_code(self) -> ModelRulingRead:
        if self.expect.kind is EnumRulingExpect.EXIT_ZERO and not (
            0 in self.ok_exit_codes and any(code != 0 for code in self.ok_exit_codes)
        ):
            raise ValueError(
                "exit_zero names no nonzero exit code that reads FALSE; "
                "list it in ok_exit_codes"
            )
        return self


class ModelRulingRow(BaseModel):
    """One standing ruling: its stamp, what must be true, how to read it, who owns it and what to do when it is not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(pattern=_ID)
    ruling: str = Field(pattern=_STAMP)
    must_be_true: str = Field(min_length=1)
    read: ModelRulingRead
    owner: str = Field(min_length=1)
    fix: str = Field(min_length=1)


def _one_printable_line(text: str, limit: int) -> str:
    """The one printable line of ``text``, cut to ``limit`` characters.

    The model-side twin of the compute handler's ``one_line``: a reason that came out of a
    validation error carries no line break, no control character and no tail past the limit.
    """
    pieces: list[str] = []
    for char in text:
        if char.isprintable() and not char.isspace():
            pieces.append(char)
        elif pieces and pieces[-1] != " ":
            pieces.append(" ")
    return "".join(pieces).strip()[:limit]


def _refusal_reason(exc: ValidationError) -> str:
    """One line naming each failed field and its message — never the failed value.

    ``errors(include_input=False)`` keeps the raw input out of the reason: a bad row's
    ``must_be_true`` or a read's argv can leak through no message.
    """
    parts = [
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors(include_input=False, include_context=False)
    ]
    return _one_printable_line("; ".join(parts), _REASON_CHARS)


def _command_prefixes(entries: object) -> list[tuple[str, ...]] | None:
    """Each ``allowed_commands`` entry shlex-split into its argv prefix, or ``None``.

    ``None`` means the entries themselves do not validate; the field's own validator
    refuses the policy, so the per-row check need not run.
    """
    if not isinstance(entries, (list, tuple)):
        return None
    prefixes: list[tuple[str, ...]] = []
    for entry in entries:
        if not isinstance(entry, str):
            return None
        try:
            prefixes.append(tuple(shlex.split(entry)))
        except ValueError:
            return None
    return prefixes


def _runs_an_allowed_command(
    argv: tuple[str, ...], prefixes: list[tuple[str, ...]]
) -> bool:
    """Whether ``argv`` starts, token for token, with one allowed prefix.

    Exact token equality: no path normalisation, no globbing — ``git log`` allows
    ``git log -1`` and refuses ``git``, ``gitx log`` and ``/usr/bin/git log``.
    """
    return any(argv[: len(prefix)] == prefix for prefix in prefixes)


class ModelRulingRefusedRow(BaseModel):
    """A register row the policy refused, kept so one bad row refuses itself, not the overlay.

    ``row_id`` is the raw row's own id when it is well formed and unused, else
    ``refused-row-<index>`` for the row's zero-based place in the overlay's ``rows`` list.
    ``ruling`` is the raw stamp when it matches the stamp pattern, else empty. ``reason`` is
    one printable line built from validation error fields and messages only: the raw input
    value never enters it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(pattern=_ID)
    owner: str = Field(min_length=1)
    ruling: str = Field(pattern=_STAMP_OR_EMPTY)
    reason: str

    @field_validator("reason")
    @classmethod
    def _one_line_at_most_the_reason_cap(cls, value: str) -> str:
        if len(value) > _REASON_CHARS or any(not char.isprintable() for char in value):
            raise ValueError(
                "a refused row's reason is one printable line of at most "
                f"{_REASON_CHARS} characters"
            )
        return value


def _refused_row(
    item: object, index: int, used: set[str], reason: str
) -> ModelRulingRefusedRow:
    """The typed refusal of the raw row at ``index``, naming the ids taken so far in ``used``.

    A row whose own ``row_id`` is missing, malformed or already taken is named
    ``refused-row-<index>``; an owner nothing usable names reads ``none``; a stamp that does
    not match the pattern reads empty. The assigned id joins ``used``.
    """
    raw_id = (
        item.get("row_id")
        if isinstance(item, Mapping)
        else getattr(item, "row_id", None)
    )
    if not (
        isinstance(raw_id, str) and re.fullmatch(_ID, raw_id) and raw_id not in used
    ):
        raw_id = f"refused-row-{index}"
    used.add(raw_id)
    raw_owner = (
        item.get("owner") if isinstance(item, Mapping) else getattr(item, "owner", None)
    )
    owner = raw_owner if isinstance(raw_owner, str) and raw_owner.strip() else "none"
    raw_ruling = (
        item.get("ruling")
        if isinstance(item, Mapping)
        else getattr(item, "ruling", None)
    )
    ruling = (
        raw_ruling
        if isinstance(raw_ruling, str) and re.fullmatch(_STAMP, raw_ruling)
        else ""
    )
    return ModelRulingRefusedRow(
        row_id=raw_id,
        owner=owner,
        ruling=ruling,
        reason=_one_printable_line(reason, _REASON_CHARS),
    )


class ModelRulingRegisterPolicy(BaseModel):
    """The ``node_ruling_register_compute`` overlay: the rows, how stale a reading may be and the argv prefixes the rows may run.

    An ``allowed_commands`` entry is one shell-style string holding an argv prefix; a row is
    allowed when its ``read.argv`` starts token for token with one entry's tokens. A row that
    fails validation, repeats a row id or runs outside the prefixes lands in
    ``refused_rows`` — a refused row never stays in ``rows``. The policy refuses only when
    nothing at all is listed, or more than ``MAX_REGISTER_ROWS`` rows are.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_reading_age_seconds: int = Field(ge=1)
    rows: tuple[ModelRulingRow, ...]
    max_detail_chars: int = Field(default=120, ge=1)
    allowed_commands: tuple[str, ...] = Field(min_length=1)
    refused_rows: tuple[ModelRulingRefusedRow, ...] = ()

    @field_validator("allowed_commands")
    @classmethod
    def _entries_are_argv_prefixes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for entry in value:
            if not entry.strip():
                raise ValueError("an allowed_commands entry is empty")
            try:
                shlex.split(entry)
            except ValueError:
                raise ValueError(
                    "an allowed_commands entry is not a shell-style argv prefix: "
                    f"{entry!r} does not shlex-split"
                ) from None
        return value

    @model_validator(mode="before")
    @classmethod
    def _refuse_bad_rows_into_refused_rows(cls, data: object) -> object:
        """Split the raw ``rows``: a valid row stays, a broken one becomes a refused row.

        A row that does not validate is refused with the validation error's fields and
        messages; a valid row repeating a kept id is refused; a valid row whose
        ``read.argv`` starts with no allowed prefix is refused. One bad row never refuses
        the policy.
        """
        if not isinstance(data, Mapping) or "rows" not in data:
            return data
        listed = data["rows"]
        if not isinstance(listed, (list, tuple)):
            return data
        if len(listed) > MAX_REGISTER_ROWS:
            raise ValueError(
                f"the register lists {len(listed)} rows; the schema ceiling is "
                f"{MAX_REGISTER_ROWS} rows (valid plus refused)"
            )
        prefixes = _command_prefixes(data.get("allowed_commands"))
        rows: list[ModelRulingRow] = []
        refused: list[ModelRulingRefusedRow] = []
        used: set[str] = set()
        for index, item in enumerate(listed):
            try:
                row = ModelRulingRow.model_validate(item)
            except ValidationError as exc:
                refused.append(_refused_row(item, index, used, _refusal_reason(exc)))
                continue
            if row.row_id in used:
                refused.append(
                    _refused_row(item, index, used, "row id listed more than once")
                )
                continue
            if prefixes is not None and not _runs_an_allowed_command(
                row.read.argv, prefixes
            ):
                refused.append(
                    _refused_row(
                        item,
                        index,
                        used,
                        f"runs {row.read.argv[0]}, which is not in allowed_commands",
                    )
                )
                continue
            rows.append(row)
            used.add(row.row_id)
        # A policy dumped and validated again carries its refusals already: keep them.
        carried = data.get("refused_rows") or ()
        if not rows and not refused and not carried:
            raise ValueError("the register lists no row")
        return {**data, "rows": rows, "refused_rows": [*carried, *refused]}


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
    """One row's judgement; ``why`` and ``fix`` are the tick line's cells.

    A cell is one printable line: a verdict that carries a line break or a control
    character is refused, so no read's output can forge a second tick line.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(pattern=_ID)
    # A refused row may carry no stamp, so the empty stamp stands for none.
    ruling: str = Field(pattern=_STAMP_OR_EMPTY)
    owner: str = Field(min_length=1)
    verdict: EnumRulingVerdict
    detail: str
    why: str
    fix: str

    @field_validator("detail", "why", "fix")
    @classmethod
    def _one_printable_line(cls, value: str) -> str:
        if any(not char.isprintable() for char in value):
            raise ValueError(
                "a verdict cell is one printable line: no line break, "
                "no control character"
            )
        return value

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
    """What the effect returns: one reading per row, in row order, and the rows whose event did not reach the journal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    readings: tuple[ModelRulingReading, ...]
    sink_errors: tuple[str, ...] = ()


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
    "MAX_REGISTER_ROWS",
    "EnumReadStatus",
    "EnumRulingExpect",
    "EnumRulingVerdict",
    "ModelRulingExpect",
    "ModelRulingRead",
    "ModelRulingReadRequest",
    "ModelRulingReadResult",
    "ModelRulingReading",
    "ModelRulingRefusedRow",
    "ModelRulingRegisterPolicy",
    "ModelRulingRegisterRequest",
    "ModelRulingRegisterResult",
    "ModelRulingRow",
    "ModelRulingVerdict",
]
