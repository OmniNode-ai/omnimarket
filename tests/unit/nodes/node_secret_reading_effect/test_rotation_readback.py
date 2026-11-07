# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 and AC2 of OMN-22913: the rotation readback probes every consumer of the overlay's list,
writes one row per consumer, and reads a consumer left on the old key as NOT WORKING by name."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.models.model_secret_rotation import (
    EnumConsumerCallOutcome,
    ModelRotationReadbackCommand,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.adapters import (
    CommandConsumerCalls,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.overlay import (
    ModelRotationConsumer,
)
from tests.unit.nodes.node_secret_reading_effect.rotation_support import (
    APPROVER,
    CONSUMERS,
    DEV_FOLDER,
    DEV_KEY,
    NEW_VALUE,
    OLD_VALUE,
    calls,
    handler,
    overlay,
    store,
)

pytestmark = pytest.mark.unit


def command(write: bool = True) -> ModelRotationReadbackCommand:
    return ModelRotationReadbackCommand(
        correlation_id="rot-1",
        rotation_id="drill-rotation-1",
        rotated_folder=DEV_FOLDER,
        rotated_key=DEV_KEY,
        approver=APPROVER,
        drill=True,
        write=write,
    )


def test_rotation_drill_probes_every_consumer_and_writes_one_row_per_consumer() -> None:
    test_store = store()
    ledger = InMemoryLedger()
    result = handler(test_store, calls(test_store), ledger).handle(command())
    assert result.status == "completed"
    assert [c.consumer for c in result.consumers] == list(CONSUMERS)
    assert all(c.verdict is EnumReadingVerdict.WORKING for c in result.consumers)
    assert result.verdict is EnumReadingVerdict.WORKING
    assert len(ledger.lines) == len(CONSUMERS)
    for name, line in zip(CONSUMERS, ledger.lines, strict=True):
        assert f"consumer={name} " in line + " "
        assert "verdict=WORKING" in line
        assert f"approver={APPROVER}" in line
        assert "rotation=drill-rotation-1" in line
        assert "tag=drill" in line
        assert "drill=yes" in line


def test_every_consumer_of_a_longer_overlay_list_gets_its_own_row() -> None:
    extra = [
        {
            "consumer": f"c{i}",
            "folder": DEV_FOLDER,
            "key": DEV_KEY,
            "probe_command": None,
        }
        for i in range(7)
    ]
    test_store = store()
    ledger = InMemoryLedger()
    result = handler(
        test_store, calls(test_store), ledger, overlay(consumers=extra)
    ).handle(command())
    assert len(result.consumers) == 7
    assert len(ledger.lines) == 7
    assert len({line.split("consumer=")[1].split(" ")[0] for line in ledger.lines}) == 7


def test_stale_consumer_reads_not_working_and_names_the_consumer() -> None:
    test_store = store()
    test_calls = calls(test_store)
    test_calls.hold("decider", OLD_VALUE)
    ledger = InMemoryLedger()
    result = handler(test_store, test_calls, ledger).handle(command())
    by_name = {c.consumer: c for c in result.consumers}
    assert by_name["decider"].verdict is EnumReadingVerdict.NOT_WORKING
    assert by_name["decider"].call is EnumConsumerCallOutcome.REJECTED
    assert by_name["gate"].verdict is EnumReadingVerdict.WORKING
    assert by_name["estimator"].verdict is EnumReadingVerdict.WORKING
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert "consumer-decider-rejected" in result.reasons
    decider_row = next(line for line in ledger.lines if "consumer=decider " in line)
    assert "verdict=NOT_WORKING" in decider_row
    assert "call=rejected" in decider_row
    assert sum("verdict=NOT_WORKING" in line for line in ledger.lines) == 1


def test_stale_consumer_that_restarts_on_the_new_key_reads_working_again() -> None:
    test_store = store()
    test_calls = calls(test_store)
    test_calls.hold("decider", OLD_VALUE)
    node = handler(test_store, test_calls, InMemoryLedger())
    assert node.handle(command(write=False)).verdict is EnumReadingVerdict.NOT_WORKING
    test_calls.hold("decider", NEW_VALUE)
    assert node.handle(command(write=False)).verdict is EnumReadingVerdict.WORKING


def test_consumer_that_cannot_resolve_its_key_reads_not_working() -> None:
    test_store = store()
    test_store.revoke("gate")
    result = handler(test_store, calls(test_store), InMemoryLedger()).handle(
        command(write=False)
    )
    gate = next(c for c in result.consumers if c.consumer == "gate")
    assert gate.verdict is EnumReadingVerdict.NOT_WORKING
    assert gate.call is None
    assert "consumer-gate-refused" in result.reasons


def test_store_down_marks_the_reading_source_unreadable() -> None:
    test_store = store()
    test_store.down = True
    ledger = InMemoryLedger()
    result = handler(test_store, calls(test_store), ledger).handle(command())
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert all("outcome=source-unreadable" in line for line in ledger.lines)


def test_no_row_and_no_result_carries_a_secret_value() -> None:
    test_store = store()
    test_calls = calls(test_store)
    test_calls.hold("decider", OLD_VALUE)
    ledger = InMemoryLedger()
    result = handler(test_store, test_calls, ledger).handle(command())
    text = result.model_dump_json() + "".join(ledger.lines)
    assert OLD_VALUE not in text
    assert NEW_VALUE not in text


def test_a_redelivered_rotation_writes_each_consumer_row_once() -> None:
    test_store = store()
    ledger = InMemoryLedger()
    node = handler(test_store, calls(test_store), ledger)
    node.handle(command())
    again = node.handle(command())
    assert len(ledger.lines) == len(CONSUMERS)
    assert again.ledger == "already-written"


def test_a_probe_only_run_writes_nothing() -> None:
    test_store = store()
    ledger = InMemoryLedger()
    handler(test_store, calls(test_store), ledger).handle(command(write=False))
    assert ledger.lines == []


# -- the real-backend consumer call: the consumer's own probe command --------------------------


def _consumer(argv: tuple[str, ...] | None) -> ModelRotationConsumer:
    return ModelRotationConsumer(
        consumer="svc", folder=DEV_FOLDER, key=DEV_KEY, probe_command=argv
    )


def _runner(code: int | Exception):  # type: ignore[no-untyped-def]
    def run(argv: Sequence[str], timeout: int) -> int:
        if isinstance(code, Exception):
            raise code
        return code

    return run


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0, EnumConsumerCallOutcome.COMPLETED),
        (1, EnumConsumerCallOutcome.REJECTED),
        (2, EnumConsumerCallOutcome.FAILED),
        (subprocess.TimeoutExpired(["x"], 1), EnumConsumerCallOutcome.UNREACHABLE),
        (FileNotFoundError("x"), EnumConsumerCallOutcome.UNREACHABLE),
    ],
)
def test_command_consumer_call_maps_the_probe_exit_to_an_outcome(
    code: int | Exception, expected: EnumConsumerCallOutcome
) -> None:
    adapter = CommandConsumerCalls(_runner(code), timeout_seconds=3)
    assert adapter.complete_call(_consumer(("probe", "--once"))) is expected


def test_command_consumer_call_with_no_command_is_a_failed_call() -> None:
    adapter = CommandConsumerCalls(_runner(0), timeout_seconds=3)
    assert adapter.complete_call(_consumer(None)) is EnumConsumerCallOutcome.FAILED


# -- each assertion can fail: the named break turns it red -------------------------------------


def test_break_stale_consumer_ignored_turns_the_stale_consumer_assertion_red() -> None:
    """Injected break: the consumer stays on the new key. The stale-consumer assertion fails."""
    test_store = store()
    result = handler(test_store, calls(test_store), InMemoryLedger()).handle(command())
    assert result.verdict is EnumReadingVerdict.WORKING
    with pytest.raises(AssertionError):
        assert "consumer-decider-rejected" in result.reasons
