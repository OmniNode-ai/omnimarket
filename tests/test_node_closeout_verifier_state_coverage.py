# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""State-coverage contract test for node_closeout_verifier_compute (OMN-14269).

Asserts the node's declared output states — the ``outputs`` field set plus the
terminal / published event topic — so the strict state-coverage gate has a
top-level test referencing each declared state literal.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_closeout_verifier_compute as _closeout_node_pkg

_CONTRACT_PATH = Path(_closeout_node_pkg.__file__).parent / "contract.yaml"


@pytest.mark.unit
def test_closeout_verifier_declares_output_states() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    assert set(contract["outputs"]) >= {
        "evidence_artifacts",
        "test_result",
        "verifier_identity",
    }
    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.closeout-verify-completed.v1"
    )
    assert (
        "onex.evt.omnimarket.closeout-verify-completed.v1"
        in contract["event_bus"]["publish_topics"]
    )
