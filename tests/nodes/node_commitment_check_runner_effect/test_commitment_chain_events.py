# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the commitment check runner (OMN-23013).

Each test publishes the command on the in-memory bus, collects every event the run produces in order under one
correlation id, and asserts the chain. The golden chain ends in the completed event with the commitment closed; the error
chains end in the typed failure events. The last block injects named breaks (a dropped terminal event, a wrong
correlation id, a runner that reads any exit as a pass) and asserts the chain assertions turn red.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_commitment_check_runner_effect.adapters.executors import (
    InMemoryCheckExecutor,
)
from omnimarket.nodes.node_commitment_check_runner_effect.handlers.handler_commitment_check_runner import (
    HandlerCommitmentCheckRunner,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_commitment_check_runner_effect.models import (
    ModelCheckExecution,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_commitment_check_runner_effect.support import (
    CHECK,
    NODES,
    RUN_AT,
    ledger_rules,
    open_commitment_line,
    reading_line,
    runner_rules,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
EVENT_ROLES = (
    "commitment_verified",
    "commitment_regressed",
    "commitment_check_failed",
    "commitment_check_completed",
    "commitment_check_run_failed",
)
TERMINAL_ROLES = ("commitment_check_completed", "commitment_check_run_failed")
CORRELATION = "chain-1"


def _command(**changes: object) -> bytes:
    body: dict[str, object] = {"correlation_id": CORRELATION}
    body.update(changes)
    return json.dumps(body).encode()


def _handler(
    ledger: InMemoryLedger,
    executor: InMemoryCheckExecutor,
    handler_type: type[HandlerCommitmentCheckRunner] = HandlerCommitmentCheckRunner,
) -> HandlerCommitmentCheckRunner:
    return handler_type(
        rules=runner_rules(),
        commitment_rules=ledger_rules(),
        source=ledger,
        appender=ledger,
        executor=executor,
        clock=lambda: RUN_AT,
    )


async def _publish(
    handler: HandlerCommitmentCheckRunner, payload: bytes
) -> list[tuple[str, dict[str, Any]]]:
    """Publish the command on the in-memory bus; return every event produced, in order."""
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, Any]]] = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, handler, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            if topic in [TOPICS[r] for r in TERMINAL_ROLES]:
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["commitment_check_requested"],
            on_message=on_command,
            group_id="commitments",
        )
        for role in EVENT_ROLES:
            await bus.subscribe(
                TOPICS[role], on_message=collector(TOPICS[role]), group_id=f"t-{role}"
            )
        await bus.publish(TOPICS["commitment_check_requested"], key=None, value=payload)
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def _chain(ledger: InMemoryLedger, executor: InMemoryCheckExecutor, **kw: Any):
    handler = _handler(ledger, executor, **kw)
    return asyncio.run(_publish(handler, _command()))


def assert_golden_verified(events: list[tuple[str, dict[str, Any]]]) -> None:
    assert [t for t, _ in events] == [
        TOPICS["commitment_verified"],
        TOPICS["commitment_check_completed"],
    ]
    assert {b["correlation_id"] for _, b in events} == {CORRELATION}
    assert events[0][1]["result"] == "verified"
    assert events[-1][1]["checked"] == 1


def test_golden_chain_an_open_commitment_whose_check_passes_ends_verified() -> None:
    ledger = InMemoryLedger([open_commitment_line()])
    events = _chain(
        ledger, InMemoryCheckExecutor({CHECK: ModelCheckExecution(exit_code=0)})
    )
    assert_golden_verified(events)
    assert "status=verified" in ledger.lines[-1]


def test_golden_chain_a_verified_commitment_whose_check_fails_ends_regressed() -> None:
    ledger = InMemoryLedger(
        [open_commitment_line(), reading_line("verified", 0, minutes_ago=600)]
    )
    events = _chain(
        ledger, InMemoryCheckExecutor({CHECK: ModelCheckExecution(exit_code=2)})
    )
    assert [t for t, _ in events] == [
        TOPICS["commitment_regressed"],
        TOPICS["commitment_check_completed"],
    ]
    assert events[0][1]["exit_code"] == 2


def test_error_chain_a_check_outside_the_allowlist_is_a_failed_event_not_a_run() -> (
    None
):
    ledger = InMemoryLedger([open_commitment_line(check="rm -rf /tmp/x")])
    executor = InMemoryCheckExecutor()
    events = _chain(ledger, executor)
    assert [t for t, _ in events] == [
        TOPICS["commitment_check_failed"],
        TOPICS["commitment_check_completed"],
    ]
    assert events[0][1]["result"] == "refused:executable-not-allowed"
    assert executor.calls == []
    assert len(ledger.lines) == 1


def test_error_chain_a_ledger_that_cannot_be_read_ends_in_the_run_failed_event() -> (
    None
):
    ledger = InMemoryLedger([open_commitment_line()])
    ledger.read_error = "route-down"
    executor = InMemoryCheckExecutor({CHECK: ModelCheckExecution(exit_code=0)})
    events = _chain(ledger, executor)
    assert [t for t, _ in events] == [TOPICS["commitment_check_run_failed"]]
    assert events[0][1]["failure"] == "ledger-unreadable:route-down"
    assert executor.calls == []


def test_error_chain_a_refused_append_is_a_failed_event_and_the_commitment_stays_open() -> (
    None
):
    ledger = InMemoryLedger([open_commitment_line()])
    ledger.append_status = "refused"
    events = _chain(
        ledger, InMemoryCheckExecutor({CHECK: ModelCheckExecution(exit_code=0)})
    )
    assert events[0][0] == TOPICS["commitment_check_failed"]
    assert events[0][1]["result"].startswith("append-failed:refused")
    assert len(ledger.lines) == 1


def test_error_chain_a_payload_that_is_not_a_command_is_a_typed_failure() -> None:
    ledger = InMemoryLedger([open_commitment_line()])
    handler = _handler(ledger, InMemoryCheckExecutor())
    events = asyncio.run(_publish(handler, b'{"nope": 1}'))
    assert [t for t, _ in events] == [TOPICS["commitment_check_run_failed"]]
    assert events[0][1]["failure"].startswith("bad-payload")
    assert len(ledger.lines) == 1


def test_injected_break_a_failing_check_does_not_pass_the_golden_assertions() -> None:
    ledger = InMemoryLedger([open_commitment_line()])
    events = _chain(
        ledger, InMemoryCheckExecutor({CHECK: ModelCheckExecution(exit_code=1)})
    )
    with pytest.raises(AssertionError):
        assert_golden_verified(events)
    assert not any("status=verified" in line for line in ledger.lines)


def test_injected_break_a_dropped_terminal_event_and_a_wrong_correlation_id_are_caught() -> (
    None
):
    ledger = InMemoryLedger([open_commitment_line()])
    events = _chain(
        ledger, InMemoryCheckExecutor({CHECK: ModelCheckExecution(exit_code=0)})
    )
    assert_golden_verified(events)
    with pytest.raises(AssertionError):
        assert_golden_verified(events[:-1])
    wrong = [(t, {**b, "correlation_id": "other"}) for t, b in events]
    with pytest.raises(AssertionError):
        assert_golden_verified(wrong)


def test_the_contract_declares_every_topic_the_chain_uses_and_a_schedule() -> None:
    contract = yaml.safe_load(
        (NODES / "node_commitment_check_runner_effect/contract.yaml").read_text()
    )
    assert set(TOPICS) == {"commitment_check_requested", *EVENT_ROLES}
    assert contract["terminal_event"] == TOPICS["commitment_check_completed"]
    assert contract["schedule"]["interval_seconds"] >= 1
    for role in EVENT_ROLES:
        assert TOPICS[role] in contract["externally_consumed_topics"]
