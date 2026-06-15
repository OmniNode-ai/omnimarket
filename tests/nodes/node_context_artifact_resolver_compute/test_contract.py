# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract test for node_context_artifact_resolver_compute (OMN-12948).

Validates the contract.yaml structure: archetype, handler wiring, topic
declarations (both ends, byte-identical command/event), and that the artifact
source registry declares logical names only -- no absolute filesystem paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

# Forbidden absolute-path prefixes the contract registry must never contain.
# test-literal-ok: these are negative-assertion needles, not real paths.
_USER_HOME_PREFIX = "/Users" + "/"
_VOLUME_PREFIX = "/Volumes" + "/"

_NODE_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_context_artifact_resolver_compute"
)
_CONTRACT_PATH = _NODE_DIR / "contract.yaml"
_METADATA_PATH = _NODE_DIR / "metadata.yaml"


@pytest.fixture
def contract() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(_CONTRACT_PATH.read_text())
    return data


def test_contract_is_compute_pure(contract: dict[str, Any]) -> None:
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["node_archetype"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["descriptor"]["idempotent"] is True


def test_handler_wiring(contract: dict[str, Any]) -> None:
    handler = contract["handler"]
    assert handler["class"] == "HandlerArtifactResolver"
    assert handler["module"].endswith("handler_artifact_resolver")
    assert handler["input_model"].endswith("ModelArtifactResolverRequest")
    assert handler["output_model"].endswith("ModelArtifactResolverResult")


def test_topics_declared_both_ends(contract: dict[str, Any]) -> None:
    bus = contract["event_bus"]
    completed = "onex.evt.omnimarket.context-artifact-resolve-completed.v1"
    failed = "onex.evt.omnimarket.context-artifact-resolve-failed.v1"
    requested = "onex.cmd.omnimarket.context-artifact-resolve-requested.v1"
    assert requested in bus["subscribe_topics"]
    assert completed in bus["publish_topics"]
    assert failed in bus["publish_topics"]
    assert contract["terminal_event"] == completed
    # terminal event is also a publish topic (byte-identical)
    assert contract["terminal_event"] in bus["publish_topics"]


def test_source_registry_uses_logical_names_only(contract: dict[str, Any]) -> None:
    registry = contract["artifact_source_registry"]
    # Every canonical factor declared.
    for factor in (
        "golden_chain",
        "exemplar",
        "local_failures",
        "architecture_patterns",
        "claude_md",
    ):
        assert factor in registry
        entry = registry[factor]
        assert "logical_source" in entry
        assert "provenance" in entry
        assert "sectioned" in entry
        # No absolute filesystem paths leak into contract config.
        logical = entry["logical_source"]
        assert not logical.startswith(_USER_HOME_PREFIX)
        assert not logical.startswith(_VOLUME_PREFIX)
        assert not logical.startswith("/")


def test_metadata_entry_point_and_compat() -> None:
    meta = yaml.safe_load(_METADATA_PATH.read_text())
    assert meta["node_role"] == "compute"
    assert (
        meta["entry_points"]["onex.nodes"]["node_context_artifact_resolver_compute"]
        == "omnimarket.nodes.node_context_artifact_resolver_compute"
    )
    assert meta["capabilities"]["requires_network"] is False
