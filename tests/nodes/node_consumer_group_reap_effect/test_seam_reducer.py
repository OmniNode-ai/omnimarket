# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the reducer's record is the reap effect's command (OMN-23992).

The record crosses a bus as JSON and builds a command the effect accepts. The control shadows the digest function on one
side only: the effect then refuses the command, so the test goes red when either side changes the decision's shape.
"""

from __future__ import annotations

import asyncio

import pytest

import omnimarket.nodes.node_consumer_group_reap_effect.handlers.handler_group_reap as reap_module
from omnimarket.nodes.node_consumer_group_reap_effect.models import EnumReapRefusal
from omnimarket.nodes.node_consumer_group_state_reducer.models import (
    ModelGroupStateRecord,
)
from tests.nodes.node_consumer_group_reap_effect.support import (
    GROUP_A,
    STABLE,
    FakeBroker,
    build_handler,
    command,
    due_record,
)

pytestmark = pytest.mark.unit


def test_the_reducer_record_after_a_json_round_trip_builds_an_accepted_command() -> (
    None
):
    broker = FakeBroker({GROUP_A: "EMPTY", STABLE: "STABLE"})
    record = ModelGroupStateRecord.model_validate_json(
        due_record(GROUP_A).model_dump_json()
    )
    outcome = asyncio.run(build_handler(broker).handle(command(record)))
    assert outcome.status == "reaped"
    assert outcome.deleted == (GROUP_A,)


def test_control_a_digest_shaped_differently_on_the_effect_side_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = FakeBroker({GROUP_A: "EMPTY", STABLE: "STABLE"})
    cmd = command(due_record(GROUP_A))
    monkeypatch.setattr(reap_module, "decision_digest", lambda *_args: "0" * 64)
    outcome = asyncio.run(build_handler(broker).handle(cmd))
    assert outcome.status == "refused"
    assert {r.code for r in outcome.refusals} == {EnumReapRefusal.DIGEST_MISMATCH}
    assert broker.delete_calls == []
