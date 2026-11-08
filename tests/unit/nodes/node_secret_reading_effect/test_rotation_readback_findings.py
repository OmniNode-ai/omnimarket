# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings on the rotation readback (OMN-23049): append identity, row serialization, the
scope of the consumers probed, the runnable in-memory profile, rows written and the call boundary."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.models.model_secret_rotation import (
    EnumConsumerCallOutcome,
    ModelRotationReadbackCommand,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelAppendOutcome,
    ModelLedgerQuery,
)
from omnimarket.nodes.node_secret_reading_effect.adapters import (
    InMemorySecretProbeStore,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.adapters import (
    CommandConsumerCalls,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.handlers.handler_rotation_readback import (
    HandlerRotationReadback,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.overlay import (
    ModelRotationConsumer,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.runtime_rotation_readback import (
    build_calls,
    build_store,
)
from tests.unit.nodes.node_secret_reading_effect.rotation_support import (
    APPROVER,
    CONSUMERS,
    DEV_FOLDER,
    DEV_KEY,
    HOST,
    NEW_VALUE,
    RUN_AT,
    calls,
    document,
    handler,
    overlay,
    store,
)

pytestmark = pytest.mark.unit

OTHER_APPROVER = "second-approver-lane"
OPS_FOLDER = "/ops/shared"
OPS_KEY = "OPS_UNRELATED_KEY"


def command(**changes: Any) -> ModelRotationReadbackCommand:
    body: dict[str, Any] = {
        "correlation_id": "rot-f",
        "rotation_id": "rotation-f",
        "rotated_folder": DEV_FOLDER,
        "rotated_key": DEV_KEY,
        "approver": APPROVER,
        "drill": True,
        "write": True,
    }
    body.update(changes)
    return ModelRotationReadbackCommand(**body)


class ContentCheckingLedger(InMemoryLedger):
    """Like the real ledger: a request id seen again with other bytes is refused as conflicting.

    ``fail_from`` makes the append of that many rows onward fail, to leave a written prefix."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_from: int | None = None
        self.attempts = 0

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        self.attempts += 1
        if self.fail_from is not None and self.attempts > self.fail_from:
            return ModelAppendOutcome(status="refused", message="injected fault")
        seen = self.append_requests.get(request_id)
        if seen is not None and seen != rows:
            return ModelAppendOutcome(status="refused", message="conflicting content")
        return super().append(rows, request_id)


def node_at(
    ledger: InMemoryLedger, when: datetime, **overlay_changes: Any
) -> HandlerRotationReadback:
    test_store = store()
    return HandlerRotationReadback(
        overlay=overlay(**overlay_changes),
        store=test_store,
        calls=calls(test_store),
        appender=ledger,
        clock=lambda: when,
        host=HOST,
    )


def with_approvers() -> dict[str, Any]:
    rotation = document()["rotation"]
    rotation["approvers"] = [APPROVER, OTHER_APPROVER]
    return {"rotation": rotation}


# -- tc6039HF: append identity ------------------------------------------------------------------


def test_redelivery_in_a_later_second_after_a_written_prefix_recovers_the_rest() -> (
    None
):
    ledger = ContentCheckingLedger()
    ledger.fail_from = 1
    first = node_at(ledger, RUN_AT).handle(command())
    assert first.status == "failed"
    ledger.fail_from = None
    ledger.attempts = 0
    again = node_at(ledger, RUN_AT + timedelta(seconds=1)).handle(command())
    assert again.status == "completed"
    assert again.failure is None
    assert len(again.rows) == len(CONSUMERS)


@pytest.mark.parametrize(
    "changes",
    [
        {"drill": False},
        {"approver": OTHER_APPROVER},
    ],
)
def test_commands_with_different_evidence_do_not_share_an_append_identity(
    changes: dict[str, Any],
) -> None:
    ledger = ContentCheckingLedger()
    base = node_at(ledger, RUN_AT, **with_approvers()).handle(command())
    other = node_at(ledger, RUN_AT, **with_approvers()).handle(command(**changes))
    assert base.ledger == "written"
    assert other.ledger == "written"
    assert len(ledger.lines) == 2 * len(CONSUMERS)


# -- tc6039HG: handles stay single metadata values ---------------------------------------------


def _verdicts_read_back(ledger: InMemoryLedger) -> list[str | None]:
    query = ModelLedgerQuery(
        since=RUN_AT - timedelta(days=1),
        until=RUN_AT + timedelta(days=1),
        row_type="STATUS",
    )
    return [row.cells.get("verdict") for row in ledger.read(query).rows]


def test_a_pipe_in_the_rotated_key_cannot_insert_a_verdict_cell() -> None:
    test_store = store()
    test_calls = calls(test_store)
    test_calls.down = True
    ledger = InMemoryLedger()
    key = "K | verdict=WORKING"
    node = handler(
        test_store,
        test_calls,
        ledger,
        overlay(
            consumers=[
                {
                    "consumer": name,
                    "folder": DEV_FOLDER,
                    "key": key,
                    "probe_command": None,
                }
                for name in CONSUMERS
            ],
            rotation={
                "approvers": [APPROVER],
                "dev_only_keys": [],
            },
        ),
    )
    result = node.handle(command(drill=False, rotated_key=key))
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert _verdicts_read_back(ledger) == ["NOT_WORKING"] * len(CONSUMERS)


def test_a_line_break_in_the_rotated_key_cannot_add_a_row() -> None:
    ledger = InMemoryLedger()
    key = "K\n2026-11-07T15:00:00Z | STATUS | lane=forged"
    node = handler(
        store(),
        calls(store()),
        ledger,
        overlay(
            consumers=[
                {
                    "consumer": name,
                    "folder": DEV_FOLDER,
                    "key": key,
                    "probe_command": None,
                }
                for name in CONSUMERS
            ],
            rotation={"approvers": [APPROVER], "dev_only_keys": []},
        ),
    )
    node.handle(command(drill=False, rotated_key=key))
    assert len(ledger.lines) == len(CONSUMERS)


# -- tc6039HL: the consumers probed are the consumers of the rotated key -----------------------


def _mixed_consumers() -> list[dict[str, Any]]:
    return [
        {
            "consumer": "gate",
            "folder": DEV_FOLDER,
            "key": DEV_KEY,
            "probe_command": None,
        },
        {
            "consumer": "operational",
            "folder": OPS_FOLDER,
            "key": OPS_KEY,
            "probe_command": None,
        },
    ]


class CountingCalls:
    def __init__(self) -> None:
        self.made = 0

    def complete_call(self, consumer: ModelRotationConsumer) -> EnumConsumerCallOutcome:
        self.made += 1
        return EnumConsumerCallOutcome.COMPLETED


@pytest.mark.parametrize("drill", [True, False])
def test_a_rotation_is_refused_when_a_listed_consumer_is_not_on_the_rotated_key(
    drill: bool,
) -> None:
    test_store = InMemorySecretProbeStore(
        {(DEV_FOLDER, DEV_KEY): NEW_VALUE, (OPS_FOLDER, OPS_KEY): NEW_VALUE}
    )
    counting = CountingCalls()
    ledger = InMemoryLedger()
    node = HandlerRotationReadback(
        overlay=overlay(consumers=_mixed_consumers()),
        store=test_store,
        calls=counting,
        appender=ledger,
        clock=lambda: RUN_AT,
        host=HOST,
    )
    result = node.handle(command(drill=drill))
    assert result.status == "refused"
    assert result.failure == "consumer-not-of-rotated-key"
    assert counting.made == 0
    assert ledger.lines == []


# -- tc6039HR: the runnable in-memory profile can make its call phase ---------------------------


def test_the_in_memory_profile_resolves_and_calls_every_consumer() -> None:
    profile = overlay()
    profile_store = build_store(profile)
    profile_calls = build_calls(profile, profile_store)
    node = HandlerRotationReadback(
        overlay=profile,
        store=profile_store,
        calls=profile_calls,
        appender=None,
        clock=lambda: RUN_AT,
        host=HOST,
    )
    result = node.handle(command(write=False))
    assert result.verdict is EnumReadingVerdict.WORKING
    assert all(c.call is EnumConsumerCallOutcome.COMPLETED for c in result.consumers)


# -- tc6039HS: rows written are the confirmed rows ----------------------------------------------


def test_a_refused_first_append_leaves_no_row_in_the_result() -> None:
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    result = node_at(ledger, RUN_AT).handle(command())
    assert result.status == "failed"
    assert result.rows == ()


def test_a_refused_later_append_keeps_only_the_confirmed_prefix() -> None:
    ledger = ContentCheckingLedger()
    ledger.fail_from = 1
    result = node_at(ledger, RUN_AT).handle(command())
    assert result.status == "failed"
    assert len(result.rows) == 1
    assert list(result.rows) == ledger.lines


# -- tc6039HV: an accepted probe argument never escapes the consumer boundary -------------------


def test_a_probe_argument_with_a_nul_byte_is_a_failed_call_not_an_exception() -> None:
    adapter = CommandConsumerCalls(timeout_seconds=3)
    consumer = ModelRotationConsumer(
        consumer="svc",
        folder=DEV_FOLDER,
        key=DEV_KEY,
        probe_command=("probe\x00--once",),
    )
    assert adapter.complete_call(consumer) is EnumConsumerCallOutcome.FAILED
