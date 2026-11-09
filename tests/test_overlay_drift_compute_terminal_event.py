# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_overlay_drift_compute declares its terminal event in the contract.

The state-coverage gate reads only ``tests/``; the node's chain tests live
beside the node, so the terminal event is asserted here against the contract.
"""

from pathlib import Path

import yaml

CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_overlay_drift_compute/contract.yaml"
)


def test_node_overlay_drift_compute_terminal_event_is_run_completed() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.overlay-consumer-join-run-completed.v1"
    )
    assert (
        contract["topics"]["overlay_consumer_join_run_completed"]
        == contract["terminal_event"]
    )
