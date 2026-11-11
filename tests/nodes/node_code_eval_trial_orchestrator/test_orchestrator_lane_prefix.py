# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23882: a unit receipt reaches its run when the overlay's ``unit_lane_prefix`` is not empty.

The orchestrator sets each unit's lane to the overlay prefix followed by the run id, and the receipt
carries that lane back. The overlay of the overlays repository and the node's example overlay both
state ``code-eval-``; a run key read from the whole lane would drop every receipt and the run would
never close. Exemplar: ``test_orchestrator_golden_chain.py``.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_ingress import (
    run_id_of,
)
from tests.nodes.node_code_eval_trial_orchestrator.orchestrator_support import (
    RUN_ID,
    completed_host,
    leg_config,
    make_bus,
    run_request,
)

pytestmark = pytest.mark.unit

PREFIX = "code-eval-"


def test_a_run_with_a_lane_prefix_closes_with_every_row() -> None:
    bus = make_bus(config=leg_config(unit_lane_prefix=PREFIX))
    bus.send(run_request(2, 2))
    bus.drain()
    assert all(unit.lane == f"{PREFIX}{RUN_ID}" for unit in bus.units)
    assert len(bus.rows) == 14
    assert len(bus.closes) == 1
    assert not bus.closes[0].abandoned


def test_the_run_id_of_a_prefixed_receipt_is_the_run() -> None:
    bus = make_bus(config=leg_config(unit_lane_prefix=PREFIX))
    bus.send(run_request(1, 1))
    bus.drain(max_steps=1)  # the control's score frees the first try's unit
    unit = bus.units[0]
    assert unit.lane == f"{PREFIX}{RUN_ID}"
    assert run_id_of(completed_host(unit)) == RUN_ID


def test_control_a_lane_that_holds_no_run_id_names_no_run() -> None:
    bus = make_bus(config=leg_config(unit_lane_prefix=PREFIX))
    bus.send(run_request(1, 1))
    bus.drain(max_steps=1)
    receipt = completed_host(bus.units[0], lane=f"{PREFIX}not-a-run")
    assert run_id_of(receipt) is None
