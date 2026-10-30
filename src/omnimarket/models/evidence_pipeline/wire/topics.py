# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# onex-topic-sot: canonical evidence-pipeline topic-name registry (OMN-13944)

"""Canonical Kafka topic name constants for the evidence pipeline.

Each constant corresponds to a topic boundary in the directed pipeline flow:

  command → collector → extractor → matcher → OCC writer → gap analyzer → readiness scorer
                                                                 ↕ (parallel dashboard lane)

All topics follow the ONEX canonical format (OMN-1537):
    onex.{kind}.{service}.{event-name}.v{N}

These constants are the single source of truth for evidence pipeline topic names.
Consumers must import from this module rather than hardcoding topic strings.

Contract definition: contracts/evidence-pipeline.yaml
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Command topics — pipeline entry points
# ---------------------------------------------------------------------------

# Trigger topic for the evidence pipeline (ModelEvidencePipelineCommand payload)
EVIDENCE_PIPELINE_START_CMD_V1 = "onex.cmd.omnimarket.evidence-pipeline-start.v1"

# ---------------------------------------------------------------------------
# Event topics — stage boundary crossings
# ---------------------------------------------------------------------------

# Raw evidence collected from source (collector → extractor boundary)
EVIDENCE_COLLECTED_EVT_V1 = "onex.evt.omnimarket.evidence-collected.v1"

# Evidence extracted and structured (extractor → matcher boundary)
EVIDENCE_EXTRACTED_EVT_V1 = "onex.evt.omnimarket.evidence-extracted.v1"

# Evidence validated against contract (matcher → OCC writer boundary)
# Also used for ModelDashboardProjectionEvent / ModelDashboardEvent VALIDATED stage
EVIDENCE_VALIDATED_EVT_V1 = "onex.evt.omnimarket.evidence-validated.v1"

# OCC PR created by the writer node (OCC writer → gap analyzer boundary)
EVIDENCE_OCC_PR_CREATED_EVT_V1 = "onex.evt.omnimarket.evidence-occ-pr-created.v1"

# Gap analysis report produced (gap analyzer → readiness scorer boundary)
EVIDENCE_GAP_ANALYZED_EVT_V1 = "onex.evt.omnimarket.evidence-gap-analyzed.v1"

# Deployment readiness score produced (final pipeline stage output)
EVIDENCE_READINESS_SCORED_EVT_V1 = "onex.evt.omnimarket.evidence-readiness-scored.v1"

# ---------------------------------------------------------------------------
# Dashboard lane topics — parallel projection events
# ---------------------------------------------------------------------------

# Normalized event emitted by the dashboard effect node for projection writes
EVIDENCE_DASHBOARD_PROJECTED_EVT_V1 = (
    "onex.evt.omnimarket.evidence-dashboard-projected.v1"
)

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
