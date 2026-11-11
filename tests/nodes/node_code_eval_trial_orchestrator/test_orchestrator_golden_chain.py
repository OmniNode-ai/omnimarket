# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23627 AC11 through the orchestrator handler: two tasks, two models, bound 3, a stub host
and a scorer passing only controls give 12 try rows, 2 control rows, 14 score requests and one
close, and the same on redelivery. Exemplar: ``test_golden_chain.py`` (the reducer's own chain).
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    ModelLabWorkUnitRequest,
)
from tests.nodes.node_code_eval_trial_orchestrator.orchestrator_support import (
    RUN_ID,
    Bus,
    make_bus,
    run_request,
)

pytestmark = pytest.mark.unit


def golden(*, redeliver: bool) -> Bus:
    bus = make_bus()
    bus.send(run_request(2, 2))
    bus.drain(redeliver=redeliver)
    return bus


def assert_golden(bus: Bus) -> None:
    rows = bus.rows
    # E3's row_kind column is not on the row model until omnimarket pull request 5835
    # merges, so controls and tries are told apart by the score requests that made them.
    controls = [s for s in bus.scores if not s.try_identity.model_arm]
    tries = [s for s in bus.scores if s.try_identity.model_arm]
    assert len(rows) == 14
    assert (len(controls), len(tries)) == (2, 12)
    assert len(bus.scores) == 14
    assert len(bus.closes) == 1
    assert len({r.correlation_id for r in rows}) == 14
    assert not bus.closes[0].abandoned
    assert {r.run_id for r in rows} == {str(RUN_ID)}


def test_golden_chain_two_tasks_two_models_bound_three() -> None:
    assert_golden(golden(redeliver=False))


def test_golden_chain_is_the_same_on_redelivery() -> None:
    plain, redelivered = golden(redeliver=False), golden(redeliver=True)
    assert_golden(redelivered)
    assert [u.work_unit_id for u in redelivered.units] == [
        u.work_unit_id for u in plain.units
    ]
    assert redelivered.closes[0].sequences == plain.closes[0].sequences


def test_a_redelivered_run_command_reads_nothing_and_changes_nothing() -> None:
    bus = make_bus()
    bus.send(run_request(2, 2))
    reads = len(bus.reader.reads)
    assert bus.send(run_request(2, 2)) == []
    assert len(bus.reader.reads) == reads


def test_every_unit_is_a_code_draft_on_a_free_host_named_by_the_run() -> None:
    bus = golden(redeliver=False)
    assert bus.units
    for unit in bus.units:
        assert isinstance(unit, ModelLabWorkUnitRequest)
        assert unit.kind == "code_draft"
        assert unit.lane == str(RUN_ID)
        assert unit.argv[:3] == ["onex", "code-edit", "run"]
        assert unit.code_draft is not None
        assert unit.code_draft.caller_lane == str(RUN_ID)
    assert {u.target_host for u in bus.units} <= {"h1", "h2", "h3", "h4"}
