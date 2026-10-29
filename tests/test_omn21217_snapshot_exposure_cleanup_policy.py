# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21217: every onex.snapshot exposure declares its own cleanup policy (Jonah's ruling on OMN-21217, 2026-10-28: compact on every snapshot exposure; node_projection_consumer_flow keeps its explicit compact,delete). The broker default is delete, and the topic provisioner reads topic_config.kafka_config only from projection_api exposures, so a policy elsewhere in the contract does not reach the topic."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
SNAPSHOT_PREFIX = "onex.snapshot."
EXPLICIT = {"node_projection_consumer_flow": "compact,delete"}


def _snapshot_exposures(contract: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every onex.snapshot exposure the projection parser would serve.

    Mirrors the parser's two shapes: an ``exposures`` list, when present, is
    the whole set; otherwise the section itself is the one exposure.
    """
    projection_api = contract.get("projection_api")
    if not isinstance(projection_api, dict):
        return []
    raw_exposures = projection_api.get("exposures")
    if raw_exposures is None:
        candidates: list[object] = [projection_api]
    elif isinstance(raw_exposures, list):
        candidates = list(raw_exposures)
    else:
        candidates = []
    return [
        item
        for item in candidates
        if isinstance(item, dict)
        and isinstance(item.get("topic"), str)
        and item["topic"].startswith(SNAPSHOT_PREFIX)
    ]


def _policy(item: dict[str, Any]) -> str | None:
    """Safely read topic_config.kafka_config.cleanup.policy from an exposure item."""
    topic_config = item.get("topic_config")
    if not isinstance(topic_config, dict):
        return None
    kafka_config = topic_config.get("kafka_config")
    if not isinstance(kafka_config, dict):
        return None
    policy = kafka_config.get("cleanup.policy")
    return policy if isinstance(policy, str) else None


def _contracts() -> list[tuple[str, dict[str, Any]]]:
    """Load every contract.yaml under NODES as (node_name, document)."""
    contracts: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(NODES.rglob("contract.yaml")):
        text = path.read_text(encoding="utf-8")
        document = yaml.safe_load(text)
        if isinstance(document, dict):
            contracts.append((path.parent.name, document))
    return contracts


def test_every_snapshot_exposure_declares_its_cleanup_policy() -> None:
    missing: list[str] = []
    for node, contract in _contracts():
        for exposure in _snapshot_exposures(contract):
            if _policy(exposure) is None:
                missing.append(f"{node}:{exposure.get('topic')}")
    assert not missing, (
        f"{len(missing)} snapshot exposure(s) lack a cleanup policy; first 20: {missing[:20]}"
    )


def test_new_snapshot_policies_are_compact() -> None:
    mismatches: list[str] = []
    for node, contract in _contracts():
        if node in EXPLICIT:
            continue
        for exposure in _snapshot_exposures(contract):
            policy = _policy(exposure)
            if policy is not None and policy != "compact":
                mismatches.append(f"{node}:{exposure.get('topic')}={policy}")
    assert not mismatches, f"non-compact snapshot policies found: {mismatches}"


def test_consumer_flow_keeps_its_explicit_policy() -> None:
    expected = EXPLICIT["node_projection_consumer_flow"]
    for node, contract in _contracts():
        if node != "node_projection_consumer_flow":
            continue
        exposures = _snapshot_exposures(contract)
        assert exposures, (
            "node_projection_consumer_flow contract has no snapshot exposure"
        )
        for exposure in exposures:
            assert _policy(exposure) == expected, (
                f"node_projection_consumer_flow exposure {exposure.get('topic')} "
                f"has policy {_policy(exposure)!r}, expected {expected!r}"
            )
        return
    pytest.fail("node_projection_consumer_flow contract not found under NODES")


def test_a_policy_on_a_sibling_item_does_not_count() -> None:
    synthetic: dict[str, Any] = {
        "projection_api": {
            "exposures": [
                {"topic": "onex.snapshot.projection.x.v1"},
                {
                    "topic": "onex.evt.omnimarket.y.v1",
                    "topic_config": {"kafka_config": {"cleanup.policy": "compact"}},
                },
            ]
        }
    }
    exposures = _snapshot_exposures(synthetic)
    assert len(exposures) == 1
    assert _policy(exposures[0]) is None


def test_a_section_topic_beside_an_exposures_list_is_not_an_exposure() -> None:
    synthetic: dict[str, Any] = {
        "projection_api": {
            "topic": "onex.snapshot.projection.legacy.v1",
            "exposures": [
                {
                    "topic": "onex.snapshot.projection.x.v1",
                    "topic_config": {"kafka_config": {"cleanup.policy": "compact"}},
                }
            ],
        }
    }
    exposures = _snapshot_exposures(synthetic)
    assert [item["topic"] for item in exposures] == ["onex.snapshot.projection.x.v1"]
