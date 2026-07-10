# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared surface pin for node_antipattern_index_effect.

OMN-13674 (cluster wave-semantic-antipattern-subsystem, archetype effect).

An EFFECT node has no fsm; its declared "state space" is its I/O operations + the
subscribe/publish topics + declared output_state. This pins those declared surfaces
against the literals the bus-coverage suite drives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from tests.integration.node_antipattern_index_effect.test_golden_chain_antipattern_index_effect import (
    TOPIC_INDEX_COMPLETED,
    TOPIC_INDEX_REQUESTED,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_antipattern_index_effect"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(_CONTRACT_PATH.read_text()))


def test_contract_is_effect() -> None:
    contract = _load_contract()
    assert contract["node_type"] == "effect"
    assert contract["descriptor"]["node_archetype"] == "effect"


def test_contract_declares_index_route() -> None:
    handlers = _load_contract()["handler_routing"]["handlers"]
    operations = {h["operation"] for h in handlers}
    assert operations == {"index_registry"}


def test_contract_topics_are_literal() -> None:
    event_bus = _load_contract()["event_bus"]
    assert TOPIC_INDEX_REQUESTED in event_bus["subscribe_topics"]
    assert TOPIC_INDEX_COMPLETED in event_bus["publish_topics"]


def test_contract_declares_idempotent_effect() -> None:
    """The index effect declares itself idempotent (registry-version keyed no-op)."""
    assert _load_contract()["descriptor"]["idempotent"] is True


def test_contract_output_state_reports_counts() -> None:
    required = set(_load_contract()["output_state"]["required"])
    assert {"indexed_count", "skipped_count", "registry_version"} <= required
