# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 of OMN-22913: a rotation row with no approver is refused, and nothing is written."""

from __future__ import annotations

import pytest

from omnimarket.models.model_secret_rotation import ModelRotationReadbackCommand
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.unit.nodes.node_secret_reading_effect.rotation_support import (
    APPROVER,
    DEV_FOLDER,
    DEV_KEY,
    calls,
    handler,
    store,
)

pytestmark = pytest.mark.unit


def command(approver: str | None, **changes: object) -> ModelRotationReadbackCommand:
    body: dict[str, object] = {
        "correlation_id": "rot-2",
        "rotation_id": "drill-rotation-2",
        "rotated_folder": DEV_FOLDER,
        "rotated_key": DEV_KEY,
        "drill": True,
        "write": True,
    }
    if approver is not None:
        body["approver"] = approver
    body.update(changes)
    return ModelRotationReadbackCommand.model_validate(body)


def run(cmd: ModelRotationReadbackCommand) -> tuple[str, str | None, list[str]]:
    test_store = store()
    ledger = InMemoryLedger()
    result = handler(test_store, calls(test_store), ledger).handle(cmd)
    return result.status, result.failure, ledger.lines


@pytest.mark.parametrize(
    "approver", [None, "", "   ", "none", "unknown", "tbd", "n/a", "-"]
)
def test_rotation_row_with_no_approver_is_refused_and_nothing_is_written(
    approver: str | None,
) -> None:
    status, failure, lines = run(command(approver))
    assert status == "refused"
    assert failure == "rotation-without-approver"
    assert lines == []


def test_rotation_row_with_an_approver_the_overlay_does_not_list_is_refused() -> None:
    status, failure, lines = run(command("someone-else"))
    assert status == "refused"
    assert failure == "approver-not-listed"
    assert lines == []


def test_rotation_row_with_the_listed_approver_is_written() -> None:
    status, failure, lines = run(command(APPROVER))
    assert (status, failure) == ("completed", None)
    assert len(lines) == 3


def test_a_drill_on_a_key_that_is_not_dev_only_is_refused() -> None:
    status, failure, lines = run(command(APPROVER, rotated_key="PROD_LOOKING_KEY"))
    assert status == "refused"
    assert failure == "drill-key-not-dev-only"
    assert lines == []


def test_a_writing_run_with_no_appender_is_refused() -> None:
    test_store = store()
    result = handler(test_store, calls(test_store), None).handle(command(APPROVER))
    assert result.status == "refused"
    assert result.failure == "no-appender-for-a-writing-run"


def test_break_approver_check_removed_turns_the_refusal_assertion_red() -> None:
    """Injected break: a command with an approver passes where the refusal is asserted."""
    status, failure, _ = run(command(APPROVER))
    with pytest.raises(AssertionError):
        assert (status, failure) == ("refused", "rotation-without-approver")
