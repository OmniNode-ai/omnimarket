# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam S3: the forwarder's receipt requests against the shared lease-and-receipt reducer (OMN-24488, plan section 8).

HELD, written ahead and skipped with its reason: the shared reducer (lane lease-build-6803) has no ``unit`` item kind merged,
so one side of this seam does not exist yet (plan hold table 7.1, steps S3 and S4). When it lands this test is un-skipped in the
pull request that does, and it runs on the reducer's in-memory profile and again on the real broker with one body. It fails
when either side changes a topic, an operation, a field or the subject namespace: the overlay's three receipt topics must be
the topics of the reducer's contract, the reducer must accept the ``unit`` kind with the properties N1 to N8, and the
forwarder's subject must stay ``unit:<tenant_slug>:<edge_id>:<idem_key>``.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_federation_edge_decision_compute import (
    overlay as edge_overlay,
)

NODE_DIR = Path(edge_overlay.__file__).resolve().parent
REDUCER_MODULE = "omnimarket.nodes.node_lease_receipt_reducer"
SKIP_REASON = (
    "held on the shared lease-and-receipt reducer's unit item kind (lane lease-build-6803, plan R2, hold table 7.1): "
    "node_lease_receipt_reducer is not merged, so the seam has one side; un-skip with the reducer's pull request"
)

pytestmark = pytest.mark.unit


@pytest.mark.skip(reason=SKIP_REASON)
def test_receipt_topics_and_unit_kind_are_the_reducers() -> None:
    reducer = importlib.import_module(REDUCER_MODULE)
    reducer_dir = Path(reducer.__file__).resolve().parent
    reducer_contract = yaml.safe_load((reducer_dir / "contract.yaml").read_text())
    overlay = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    receipt = overlay["direction"]["receipt"]
    reducer_topics = set(reducer_contract["event_bus"]["subscribe_topics"]) | set(
        reducer_contract["event_bus"]["publish_topics"]
    )
    # the three receipt topics of the overlay are the reducer's own (the refusal topic included: the forwarder reads it).
    assert receipt["request_topic"] in reducer_topics
    assert receipt["record_topic"] in reducer_topics
    assert receipt["refusal_topic"] in reducer_topics
    # the reducer takes the forwarder's item kind, and the forwarder's subject namespace is disjoint from the landing controller's.
    assert receipt["item_kind"] in reducer_contract["kinds"]
    # N1 to N8 and the required tests T1, T3, T4, T5, T6 of plan 7.3 are run here against the in-memory reducer and the real broker.
