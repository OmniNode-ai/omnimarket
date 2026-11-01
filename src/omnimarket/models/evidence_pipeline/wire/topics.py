# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Contract-backed evidence-pipeline wire topic constants.

Active boundaries read the owning node's contract. The three legacy wire names
remain in the adjacent compatibility contract so moving the public DTO package
does not silently rename its wire topics. They are not active node outputs.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_section,
    contract_subscribe_topics,
)

_NODES_ROOT = Path(__file__).resolve().parents[3] / "nodes"
_LEGACY_CONTRACT = Path(__file__).with_name("contract.yaml")


def _node_topic(node: str, *, subscribe: bool = False) -> str:
    """Resolve a single topic, refusing a missing or ambiguous boundary."""
    contract_path = _NODES_ROOT / node / "contract.yaml"
    loader = contract_subscribe_topics if subscribe else contract_publish_topics
    topics = loader(contract_path)
    if len(topics) != 1:
        raise ValueError(f"{contract_path}: expected exactly one topic, got {topics!r}")
    return topics[0]


def _legacy_topic(name: str) -> str:
    """Keep historical wire exports separate from active node declarations."""
    topic = contract_section(_LEGACY_CONTRACT, "legacy_topics", name)
    if not isinstance(topic, str) or not topic:
        raise ValueError(f"{_LEGACY_CONTRACT}: legacy topic {name!r} must be a string")
    return topic


# ---------------------------------------------------------------------------
# Command topics — pipeline entry points
# ---------------------------------------------------------------------------

# Trigger topic for the evidence pipeline (ModelEvidencePipelineCommand payload)
EVIDENCE_PIPELINE_START_CMD_V1 = _node_topic(
    "node_evidence_pipeline_orchestrator", subscribe=True
)

# ---------------------------------------------------------------------------
# Event topics — stage boundary crossings
# ---------------------------------------------------------------------------

# Raw evidence collected from source (collector → extractor boundary)
EVIDENCE_COLLECTED_EVT_V1 = _node_topic("node_evidence_collector_effect")

# Evidence extracted and structured (extractor → matcher boundary)
EVIDENCE_EXTRACTED_EVT_V1 = _node_topic("node_evidence_extractor_compute")

# Evidence validated against contract (matcher → OCC writer boundary)
# Also used for ModelDashboardProjectionEvent / ModelDashboardEvent VALIDATED stage
EVIDENCE_VALIDATED_EVT_V1 = _node_topic("node_contract_matcher_compute")

# OCC PR created by the writer node (OCC writer → gap analyzer boundary)
EVIDENCE_OCC_PR_CREATED_EVT_V1 = _legacy_topic("occ_pr_created")

# Gap analysis report produced (gap analyzer → readiness scorer boundary)
EVIDENCE_GAP_ANALYZED_EVT_V1 = _node_topic("node_gap_analyzer_compute")

# Deployment readiness score produced (final pipeline stage output)
EVIDENCE_READINESS_SCORED_EVT_V1 = _legacy_topic("readiness_scored")

# ---------------------------------------------------------------------------
# Dashboard lane topics — parallel projection events
# ---------------------------------------------------------------------------

# Normalized event emitted by the dashboard effect node for projection writes
EVIDENCE_DASHBOARD_PROJECTED_EVT_V1 = _legacy_topic("dashboard_projected")

__all__: list[str] = [
    "EVIDENCE_COLLECTED_EVT_V1",
    "EVIDENCE_DASHBOARD_PROJECTED_EVT_V1",
    "EVIDENCE_EXTRACTED_EVT_V1",
    "EVIDENCE_GAP_ANALYZED_EVT_V1",
    "EVIDENCE_OCC_PR_CREATED_EVT_V1",
    "EVIDENCE_PIPELINE_START_CMD_V1",
    "EVIDENCE_READINESS_SCORED_EVT_V1",
    "EVIDENCE_VALIDATED_EVT_V1",
]
