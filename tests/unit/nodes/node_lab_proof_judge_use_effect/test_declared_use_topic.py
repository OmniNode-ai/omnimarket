# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared output state is referenced as a literal in an asserting position."""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.unit.nodes.node_lab_proof_judge_use_effect.conftest import USE_TOPIC

_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_lab_proof_judge_use_effect/contract.yaml"
)


def _contract() -> dict[str, object]:
    return yaml.safe_load(_CONTRACT.read_text())


def test_overlay_topic_is_the_contract_terminal_event() -> None:
    contract = _contract()
    assert contract["terminal_event"] == USE_TOPIC
    assert USE_TOPIC == "onex.evt.omnimarket.lab-proof-judge-use-recorded.v1"


def test_drifted_topic_is_not_the_declared_one() -> None:
    contract = _contract()
    assert (
        contract["terminal_event"]
        != "onex.evt.omnimarket.lab-proof-judge-use-recorded.v2"
    )
