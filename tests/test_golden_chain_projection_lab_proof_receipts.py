# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for ``node_projection_lab_proof_receipts`` (OMN-19566, T2 slice 2).

omnibase_infra prepr_runtime_pool.py run (one pr-head receipt per proved PR)
    -> onex.evt.omnibase-infra.lab-proof-receipt.v1          (lab fact publisher)
    -> omninode_internal.lab_proof_receipts                  (one row per key)
    -> onex.evt.omnimarket.projection-lab-proof-receipts-applied.v1 (terminal)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_lab_proof_receipts.handlers.handler_lab_proof_receipts_writer import (
    LabProofReceiptsProjectionWriter,
)

pytestmark = pytest.mark.unit

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_lab_proof_receipts"
    / "contract.yaml"
)
_RECEIPT_TOPIC = "onex.evt.omnibase-infra.lab-proof-receipt.v1"  # onex-topic-allow: the omnibase_infra producer's topic
_TERMINAL_TOPIC = "onex.evt.omnimarket.projection-lab-proof-receipts-applied.v1"  # onex-topic-allow: this node's declared terminal
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-lab-proof-receipts-malformed.v1"  # onex-topic-allow: this node's declared DLQ


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_golden_chain_hops_are_the_declared_topics() -> None:
    contract = _contract()
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [_RECEIPT_TOPIC]
    assert bus["publish_topics"] == [_TERMINAL_TOPIC]
    assert bus["dlq_topics"] == [_DLQ_TOPIC]
    assert contract["terminal_event"] == _TERMINAL_TOPIC
    produced = [t["topic"] for t in contract["externally_produced_topics"]]
    assert produced == [_RECEIPT_TOPIC]


def test_golden_chain_the_key_is_the_plans_five_tuple() -> None:
    db_io = _contract()["db_io"]
    assert db_io["dedupe_key"] == [
        "repo",
        "pr_number",
        "head_sha",
        "profile_id",
        "profile_version",
    ]
    assert db_io["ordering_key"] == "finished_at"


def test_golden_chain_only_the_writer_is_routed() -> None:
    routed = [
        entry["handler"]["name"] for entry in _contract()["handler_routing"]["handlers"]
    ]
    assert routed == ["LabProofReceiptsProjectionWriter"]
    assert LabProofReceiptsProjectionWriter.onex_runtime_inprocess_dispatch is True
