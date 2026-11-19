# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The register's refused rows and command allowlist (OMN-24521).

``allowed_commands`` is required and each of its entries is one shell-style argv prefix:
a row is allowed only when its read.argv starts, token for token, with one entry's tokens.
One broken row never refuses the policy — a row that fails validation, repeats a row id or
runs outside the allowlist becomes a ``ModelRulingRefusedRow`` whose reason is built from
the validation error's fields and messages alone, so the row's own values leak nowhere.
The judge prints every refused row UNREAD with its fix and ``passed`` falls; the policy
itself refuses only when ``allowed_commands`` names no usable prefix, nothing at all is
listed or the schema ceiling of ``MAX_REGISTER_ROWS`` rows is passed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_ruling_register import (
    MAX_REGISTER_ROWS,
    EnumReadStatus,
    EnumRulingVerdict,
    ModelRulingReading,
    ModelRulingRegisterPolicy,
    ModelRulingRegisterRequest,
    ModelRulingRegisterResult,
    ModelRulingRow,
)
from omnimarket.nodes.node_ruling_register_compute.handlers.handler_ruling_register import (
    HandlerRulingRegister,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)
RULING = "2026-07-01T10:00:00Z"


def raw_row(
    row_id: str = "row-1",
    argv: tuple[str, ...] = ("example-command",),
    **changes: Any,
) -> dict[str, Any]:
    """A valid register row as the overlay lists it; ``changes`` override any field."""
    item: dict[str, Any] = {
        "row_id": row_id,
        "ruling": RULING,
        "must_be_true": "the example ruling holds",
        "read": {
            "host": "host-a",
            "argv": list(argv),
            # 1 is the code that reads FALSE; exit_zero must name one.
            "ok_exit_codes": [0, 1],
            "expect": {"kind": "exit_zero"},
        },
        "owner": "example-lane",
        "fix": f"fix {row_id}",
    }
    item.update(changes)
    return item


def register(
    rows: Sequence[dict[str, Any] | ModelRulingRow],
    allowed_commands: tuple[str, ...] = ("example-command",),
) -> ModelRulingRegisterPolicy:
    return ModelRulingRegisterPolicy(
        max_reading_age_seconds=600,
        allowed_commands=allowed_commands,
        rows=tuple(rows),
    )


def ok_reading(row_id: str) -> ModelRulingReading:
    return ModelRulingReading(
        row_id=row_id,
        status=EnumReadStatus.OK,
        exit_code=0,
        stdout="",
        read_at=NOW,
    )


def judged(
    policy: ModelRulingRegisterPolicy, *readings: ModelRulingReading
) -> ModelRulingRegisterResult:
    return HandlerRulingRegister().handle(
        ModelRulingRegisterRequest(
            correlation_id="ruling-1", now=NOW, policy=policy, readings=readings
        )
    )


# ---- allowed_commands is required -----------------------------------------------------------------


@pytest.mark.parametrize(
    "allowed",
    [None, (), ("",), ("   ",), ('entry "unclosed',)],
    ids=["omitted", "empty", "blank_entry", "blank_entry_spaces", "unbalanced_quote"],
)
def test_an_overlay_that_names_no_usable_allowed_commands_is_refused(
    allowed: tuple[str, ...] | None,
) -> None:
    """Each entry must shlex-split into a non-empty argv prefix; before this change
    the field was optional and ``None`` restricted nothing."""
    fields: dict[str, Any] = {
        "max_reading_age_seconds": 600,
        "rows": (raw_row(),),
    }
    if allowed is not None:
        fields["allowed_commands"] = allowed
    with pytest.raises(ValidationError, match="allowed_commands"):
        ModelRulingRegisterPolicy(**fields)


# ---- an entry is an argv prefix, matched token for token ------------------------------------------


def test_a_prefix_entry_matches_token_for_token_and_a_shorter_argv_never_starts_it() -> (
    None
):
    """``git log`` allows ``git log -1`` and refuses ``git push``, the lookalike
    ``gitx log`` and the bare ``git``: exact token equality, no path normalisation,
    no globbing — and the old argv[0] check would have refused the allowed row too."""
    policy = register(
        (
            raw_row("row-log", argv=("git", "log", "-1")),
            raw_row("row-push", argv=("git", "push")),
            raw_row("row-lookalike", argv=("gitx", "log")),
            raw_row("row-bare", argv=("git",)),
        ),
        allowed_commands=("git log",),
    )
    assert [row.row_id for row in policy.rows] == ["row-log"]
    assert [refused.row_id for refused in policy.refused_rows] == [
        "row-push",
        "row-lookalike",
        "row-bare",
    ]
    assert [refused.reason for refused in policy.refused_rows] == [
        "runs git, which is not in allowed_commands",
        "runs gitx, which is not in allowed_commands",
        "runs git, which is not in allowed_commands",
    ]


def test_a_row_outside_allowed_commands_is_refused_and_the_rest_stay_rows() -> None:
    policy = register((raw_row("row-ok"), raw_row("row-out", argv=("other-command",))))
    assert [row.row_id for row in policy.rows] == ["row-ok"]
    (refused,) = policy.refused_rows
    assert refused.row_id == "row-out"
    assert refused.reason == "runs other-command, which is not in allowed_commands"
    # a refusal keeps the row's own usable stamp
    assert refused.ruling == RULING


# ---- one broken row refuses itself, not the overlay -----------------------------------------------


def test_one_broken_row_is_refused_as_data_and_printed_unread_the_rest_judged() -> None:
    """Golden chain with one broken row: the policy loads with the two good rows and
    the bad stamp refused; the judge reads three verdicts, prints one MISSING line for
    the refused row beside the TRUE rows' silence, and ``passed`` falls."""
    policy = register(
        (
            raw_row("row-good-a"),
            raw_row("row-bad", ruling="not-a-stamp"),
            raw_row("row-good-b"),
        )
    )
    assert [row.row_id for row in policy.rows] == ["row-good-a", "row-good-b"]
    (refused,) = policy.refused_rows
    assert refused.row_id == "row-bad"
    assert refused.ruling == ""
    assert refused.owner == "example-lane"
    assert "ruling" in refused.reason

    result = judged(policy, ok_reading("row-good-a"), ok_reading("row-good-b"))
    assert [verdict.verdict for verdict in result.verdicts] == [
        EnumRulingVerdict.TRUE,
        EnumRulingVerdict.TRUE,
        EnumRulingVerdict.UNREAD,
    ]
    assert result.passed is False
    assert (result.true_count, result.false_count, result.unread_count) == (2, 0, 1)
    (line,) = result.lines()
    assert line.startswith("MISSING ruling-not-running:row-bad | ")
    assert refused.reason in line
    assert line.endswith(
        " | FIX: correct this row in the node_ruling_register_compute overlay"
    )
    bad = result.verdicts[-1]
    assert bad.row_id == "row-bad"
    assert bad.ruling == ""
    assert bad.owner == "example-lane"
    assert bad.detail == refused.reason
    assert bad.why == f"register row refused: {refused.reason}"


def test_a_refused_row_whose_own_id_is_unusable_is_named_by_its_place() -> None:
    """The refused row's id must match the row-id pattern; ``Bad_Id!`` does not, so
    the refusal takes the row's zero-based place in the overlay's rows list."""
    policy = register((raw_row("row-good"), raw_row("Bad_Id!")))
    (refused,) = policy.refused_rows
    assert refused.row_id == "refused-row-1"
    assert "row_id" in refused.reason


def test_a_row_id_listed_twice_keeps_the_first_and_refuses_the_later_one() -> None:
    """The overlay may list rows as dicts or as already-built rows; the second
    ``row-twice`` (its own id now taken) is refused under its place's name."""
    policy = register(
        (ModelRulingRow.model_validate(raw_row("row-twice")), raw_row("row-twice"))
    )
    assert [row.row_id for row in policy.rows] == ["row-twice"]
    (refused,) = policy.refused_rows
    assert refused.row_id == "refused-row-1"
    assert refused.reason == "row id listed more than once"


def test_a_refused_row_leaks_no_raw_input_value() -> None:
    """The 300-character secret sits in the row's ``must_be_true`` and in the field
    that fails validation (the stamp); the reason is built from the validation
    error's fields and messages only, so no verdict cell nor tick line carries it."""
    secret = "SECRET-" + "x" * 300
    policy = register(
        (
            raw_row("row-good"),
            raw_row("row-leak", ruling=secret, must_be_true=secret),
        )
    )
    (refused,) = policy.refused_rows
    assert "SECRET" not in refused.reason
    assert "xxx" not in refused.reason
    assert len(refused.reason) <= 160

    result = judged(policy, ok_reading("row-good"))
    bad = result.verdicts[-1]
    for cell in (bad.detail, bad.why, bad.fix, bad.owner):
        assert "SECRET" not in cell
        assert "xxx" not in cell
    for line in result.lines():
        assert "SECRET" not in line
        assert "xxx" not in line


def test_the_register_refuses_more_rows_than_the_schema_ceiling() -> None:
    """``MAX_REGISTER_ROWS`` rows (valid plus refused) list; one more refuses the
    policy outright — the ceiling is a schema bound, not a deployment value."""
    listed = tuple(raw_row(f"row-{number}") for number in range(MAX_REGISTER_ROWS))
    loaded = register(listed)
    assert len(loaded.rows) == MAX_REGISTER_ROWS
    assert loaded.refused_rows == ()
    with pytest.raises(ValidationError, match="schema ceiling"):
        register((*listed, raw_row("row-one-too-many")))


def test_a_policy_dumped_and_validated_again_keeps_its_refused_rows() -> None:
    """A policy that crossed the bus (dump, then validate) still carries its refusals."""
    from omnimarket.models.model_ruling_register import ModelRulingRegisterPolicy

    good = {
        "row_id": "good-row",
        "ruling": "2026-07-01T10:00:00Z",
        "must_be_true": "holds",
        "read": {"host": "h", "argv": ["true"], "expect": {"kind": "stdout_empty"}},
        "owner": "none",
        "fix": "fix it",
    }
    bad = {**good, "row_id": "bad-row", "ruling": "not-a-stamp"}
    policy = ModelRulingRegisterPolicy.model_validate(
        {
            "max_reading_age_seconds": 60,
            "allowed_commands": ["true"],
            "rows": [good, bad],
        }
    )
    assert [row.row_id for row in policy.rows] == ["good-row"]
    assert [row.row_id for row in policy.refused_rows] == ["bad-row"]
    again = ModelRulingRegisterPolicy.model_validate(policy.model_dump(mode="json"))
    assert [row.row_id for row in again.refused_rows] == ["bad-row"]
    assert again == policy
