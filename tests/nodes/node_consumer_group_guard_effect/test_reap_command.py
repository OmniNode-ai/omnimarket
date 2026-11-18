# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A reap runs only on a command a reducer decided, and every delete is read back (OMN-23992)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from omnimarket.nodes.node_consumer_group_reap_effect.handlers.handler_group_reap import (
    HandlerGroupReap,
)
from omnimarket.nodes.node_consumer_group_reap_effect.models import (
    EnumReapRefusal,
    ModelGroupReapCommand,
    ModelGroupReapOutcome,
)
from tests.nodes.node_consumer_group_reap_effect.support import (
    DRIFT,
    GROUP_A,
    GROUP_B,
    STABLE,
    FakeBroker,
    build_handler,
    command,
    due_record,
    execution,
    forged,
    policy,
)

pytestmark = pytest.mark.unit

LIVE = {GROUP_A: "EMPTY", GROUP_B: "EMPTY", STABLE: "STABLE"}
BOUND = policy().empty_since_bound_seconds


def run(handler: HandlerGroupReap, cmd: ModelGroupReapCommand) -> ModelGroupReapOutcome:
    return asyncio.run(handler.handle(cmd))


def codes(outcome: ModelGroupReapOutcome) -> set[EnumReapRefusal]:
    return {refusal.code for refusal in outcome.refusals}


def test_golden_reducer_decided_command_deletes_and_reads_back_absent() -> None:
    broker = FakeBroker(LIVE)
    outcome = run(build_handler(broker), command(due_record(GROUP_A, GROUP_B)))
    assert outcome.status == "reaped"
    assert set(outcome.deleted) == {GROUP_A, GROUP_B}
    assert set(outcome.read_back_absent) == {GROUP_A, GROUP_B}
    assert outcome.read_back_present == ()
    assert broker.groups == {STABLE: "STABLE"}


def test_a_file_decided_command_is_refused_and_deletes_nothing() -> None:
    broker = FakeBroker(LIVE)
    file_decided = command(due_record(GROUP_A, GROUP_B)).model_copy(
        update={"decided_by": "file"}
    )
    outcome = run(build_handler(broker), file_decided)
    assert outcome.status == "refused"
    assert EnumReapRefusal.DECIDED_NOT_BY_REDUCER in codes(outcome)
    assert broker.delete_calls == []
    assert broker.groups == LIVE


def test_a_tampered_group_list_is_refused_by_digest() -> None:
    broker = FakeBroker(LIVE)
    real = command(due_record(GROUP_A))
    tampered = real.model_copy(
        update={
            "groups": (
                *real.groups,
                real.groups[0].model_copy(update={"name": GROUP_B}),
            )
        }
    )
    outcome = run(build_handler(broker), tampered)
    assert outcome.status == "refused"
    assert EnumReapRefusal.DIGEST_MISMATCH in codes(outcome)
    assert broker.delete_calls == []


@pytest.mark.parametrize(
    ("name", "empty_for", "code"),
    [
        (DRIFT, BOUND + 1, EnumReapRefusal.PROTECTED),
        (STABLE, BOUND + 1, EnumReapRefusal.NOT_REAPABLE),
        (GROUP_A, BOUND - 1, EnumReapRefusal.BOUND_NOT_MET),
    ],
)
def test_each_policy_refusal_names_its_code(
    name: str, empty_for: int, code: EnumReapRefusal
) -> None:
    broker = FakeBroker({name: "EMPTY"})
    outcome = run(build_handler(broker), forged((name,), empty_for_seconds=empty_for))
    assert outcome.status == "refused"
    assert codes(outcome) == {code}
    assert broker.delete_calls == []


def test_over_cap_and_disabled_are_refused() -> None:
    broker = FakeBroker(LIVE)
    cmd = command(due_record(GROUP_A, GROUP_B))
    capped = run(build_handler(broker, execution(max_deletes_per_run=1)), cmd)
    assert codes(capped) == {EnumReapRefusal.OVER_CAP}
    off = run(build_handler(broker, execution(enabled=False)), cmd)
    assert codes(off) == {EnumReapRefusal.DISABLED}
    assert broker.delete_calls == []


def test_dry_run_lists_what_it_would_delete_and_touches_nothing() -> None:
    broker = FakeBroker(LIVE)
    outcome = run(
        build_handler(broker, execution(dry_run=True)),
        command(due_record(GROUP_A, GROUP_B)),
    )
    assert outcome.status == "dry_run"
    assert set(outcome.would_delete) == {GROUP_A, GROUP_B}
    assert broker.delete_calls == []
    assert broker.groups == LIVE


def test_a_group_that_left_empty_since_the_decision_is_not_deleted() -> None:
    broker = FakeBroker({GROUP_A: "STABLE", GROUP_B: "EMPTY", STABLE: "STABLE"})
    outcome = run(build_handler(broker), command(due_record(GROUP_A, GROUP_B)))
    assert outcome.status == "reaped"
    assert outcome.deleted == (GROUP_B,)
    assert broker.groups[GROUP_A] == "STABLE"
    assert "dropped 1" in outcome.detail


def test_a_delete_error_stops_the_run_and_no_later_batch_runs() -> None:
    broker = FakeBroker(LIVE)
    broker.fail = {GROUP_A}
    outcome = run(
        build_handler(broker, execution(batch_size=1)),
        command(due_record(GROUP_A, GROUP_B)),
    )
    assert outcome.status == "failed"
    assert outcome.delete_errors == {GROUP_A: "BROKER_ERROR"}
    assert broker.delete_calls == [[GROUP_A]]
    assert GROUP_B in broker.groups


def test_an_unhealthy_broker_refuses_before_the_first_delete() -> None:
    broker = FakeBroker(LIVE)
    broker.unhealthy = "no brokers"
    outcome = run(build_handler(broker), command(due_record(GROUP_A)))
    assert outcome.status == "refused"
    assert codes(outcome) == {EnumReapRefusal.BROKER_UNHEALTHY}
    assert broker.delete_calls == []


def test_a_group_the_read_back_still_lists_makes_the_run_partial() -> None:
    broker = FakeBroker(LIVE)
    broker.stuck = {GROUP_A}
    outcome = run(build_handler(broker), command(due_record(GROUP_A, GROUP_B)))
    assert outcome.status == "partial"
    assert outcome.read_back_present == (GROUP_A,)
    assert outcome.read_back_absent == (GROUP_B,)


def test_a_broker_failure_is_a_typed_status_never_an_exception() -> None:
    broker = FakeBroker(LIVE)

    def broken(_config: object) -> dict[str, str]:
        raise RuntimeError("secret-token")

    handler = build_handler(broker)
    handler._lister = broken
    outcome = run(handler, command(due_record(GROUP_A)))
    assert outcome.status == "failed"
    assert "secret-token" not in outcome.detail


class NoDecidedByCheck(HandlerGroupReap):
    """The control: the one check that tells a reducer's command from a file's is switched off."""

    def _decided_by_reducer(self, command: ModelGroupReapCommand) -> bool:
        return True


def test_control_without_the_decided_by_check_the_file_command_deletes() -> None:
    """The refusal test goes red when the check is removed: the same file-decided command deletes every group."""
    file_decided = command(due_record(GROUP_A, GROUP_B)).model_copy(
        update={"decided_by": "file"}
    )
    real = FakeBroker(LIVE)
    assert run(build_handler(real), file_decided).status == "refused"
    assert real.groups == LIVE

    open_door = FakeBroker(LIVE)
    handler = build_handler(open_door)
    control = NoDecidedByCheck(
        policy=handler._policy,
        overlay=handler._overlay,
        lister=open_door.list,
        deleter=open_door.delete,
        health=open_door.check,
        sleeper=lambda _seconds: None,
        resolve_broker=handler._resolve_broker,
    )
    assert run(control, file_decided).status == "reaped"
    assert open_door.groups == {STABLE: "STABLE"}


def test_a_later_observed_at_cannot_be_written_in_to_pass_the_bound() -> None:
    """The decision digest covers the census time, so editing it to meet the bound is refused."""
    broker = FakeBroker({GROUP_A: "EMPTY"})
    cmd = forged((GROUP_A,), empty_for_seconds=BOUND - 1)
    later = cmd.model_copy(update={"observed_at": cmd.observed_at + timedelta(days=1)})
    outcome = run(build_handler(broker), later)
    assert outcome.status == "refused"
    assert EnumReapRefusal.DIGEST_MISMATCH in codes(outcome)
    assert broker.delete_calls == []
