# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Top-level contract-state-coverage mirror for node_projection_live_events.

OMN-13781's state-coverage-gate (``scripts/validate_state_coverage.py``)
scopes its test corpus to the top-level ``tests/`` tree only — it does not
scan node-local test directories such as
``src/omnimarket/nodes/node_projection_live_events/tests/``. That node-local
suite already asserts the node's two declared output states for real
(``test_golden_chain_projection_live_events.py::
test_contract_exposes_live_events_snapshot``), but the gate cannot see it,
so this node is carried in ``scripts/validation/state_coverage_baseline.txt``
as a pre-existing gap. Touching the node's contract (OMN-13992: adding
``event_bus.dlq_topics``) promotes that baselined gap to a hard FAIL in
strict mode. This thin mirror closes the gap for this node without
duplicating the node-local handler test suite.
"""

from __future__ import annotations

import yaml

_CONTRACT_PATH = "src/omnimarket/nodes/node_projection_live_events/contract.yaml"
_APPLIED_TOPIC = "onex.evt.omnimarket.projection-live-events-applied.v1"
_SNAPSHOT_TOPIC = "onex.snapshot.projection.live-events.v1"


def test_contract_terminal_event_matches_applied_topic() -> None:
    with open(_CONTRACT_PATH) as f:
        contract = yaml.safe_load(f)
    assert contract["terminal_event"] == _APPLIED_TOPIC
    assert _APPLIED_TOPIC in contract["externally_consumed_topics"]


def test_contract_projection_api_exposes_snapshot_topic() -> None:
    with open(_CONTRACT_PATH) as f:
        contract = yaml.safe_load(f)
    exposures = contract["projection_api"]["exposures"]
    exposure = next(item for item in exposures if item["topic"] == _SNAPSHOT_TOPIC)
    assert exposure["table"] == "live_events"
