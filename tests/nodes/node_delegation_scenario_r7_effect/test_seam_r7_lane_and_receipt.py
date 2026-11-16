# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of scenario R7 (OMN-23696): the lane and cadence its rows are read under, and the host its receipt names.

Two seams, each failing when either side changes shape:

1. The row. R7 writes its STATUS row under the lane and cadence its own contract names. The green-days reader and the readiness verdict
   read R7 under the lane their contracts name, and the delegation board's overlay (another repository, a lane named
   ``node_delegation_scenario_r7``) reads the same. A lane renamed on one side leaves the reader with no rows and the scenario red for
   ever.
2. The receipt. R7's host assertions read ``ModelAttemptFact.host``, which ``run_leg`` fills from the delegation terminal's attempt
   lineage. A fact with no host, or a leg that stops filling it, turns the assertions red instead of passing on an empty value.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.delegation_readiness_probe import (
    LegTransportScope,
    ModelAttemptFact,
    ScriptedLegTransport,
    run_leg,
)
from omnimarket.nodes.node_delegation_scenario_r7_effect.handlers.handler_scenario_r7 import (
    NODE_DIR,
    load_inputs,
    load_runtime,
)

pytestmark = pytest.mark.unit

NODES_DIR = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
READERS = (
    "node_delegation_scenario_days_effect",
    "node_delegation_readiness_verdict_effect",
)
OVERLAY_BOARD_LANE = "node_delegation_scenario_r7"


def reader_entry(reader: str) -> dict[str, str]:
    contract = yaml.safe_load((NODES_DIR / reader / "contract.yaml").read_text())
    entries = [s for s in contract["runtime"]["scenarios"] if s["scenario_id"] == "R7"]
    assert len(entries) == 1, f"{reader} names R7 {len(entries)} times"
    return entries[0]


@pytest.mark.parametrize("reader", READERS)
def test_the_readers_read_r7_under_the_lane_and_cadence_its_node_writes(
    reader: str,
) -> None:
    runtime = load_runtime()
    entry = reader_entry(reader)
    assert entry["lane"] == runtime.lane
    assert entry["cadence"] == runtime.cadence.value


def test_the_node_writes_under_the_lane_the_board_overlay_reads() -> None:
    assert load_runtime().lane == OVERLAY_BOARD_LANE


def test_control_a_renamed_lane_is_a_disagreement_with_both_readers() -> None:
    renamed = load_runtime().model_copy(
        update={"lane": "node_delegation_scenario_r7_effect"}
    )
    for reader in READERS:
        assert reader_entry(reader)["lane"] != renamed.lane


def test_an_attempt_fact_with_no_host_reads_none_not_an_invented_host() -> None:
    fact = ModelAttemptFact(backend_id="b", failure_class=None, accepted=True)
    assert fact.host is None


def test_the_leg_fills_the_host_of_every_attempt_from_the_terminal(
    tmp_path: Path,
) -> None:
    inputs = load_inputs(NODE_DIR / load_runtime().inputs_relpath)
    with LegTransportScope(ScriptedLegTransport()) as transport:
        outcome = run_leg(
            inputs.leg_spec(),
            transport,
            work_dir=tmp_path,
            tenant_slug=load_runtime().probe_tenant_slug,
        )
    assert len(outcome.attempts) == 2
    assert [attempt.host for attempt in outcome.attempts] == [inputs.inmemory_host] * 2
