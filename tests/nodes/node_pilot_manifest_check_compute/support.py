# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders for the pilot manifest check tests (OMN-22979)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from omnimarket.nodes.node_pilot_manifest_check_compute.handlers.handler_pilot_manifest_check import (
    compute_bar_hash,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_bar_comparator import (
    EnumPilotBarComparator,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_bar_criterion import (
    ModelPilotBarCriterion,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest import (
    ModelPilotManifest,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest_check_request import (
    ModelPilotManifestCheckRequest,
)

CORRELATION_ID = UUID("00000000-0000-4000-8000-000000000016")
FROZEN_AT = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
STARTED_AT = datetime(2026, 11, 7, 11, 0, tzinfo=UTC)

BAR = (
    ModelPilotBarCriterion(
        metric="overall_accuracy", comparator=EnumPilotBarComparator.GE, threshold=0.8
    ),
    ModelPilotBarCriterion(
        metric="planted_recall", comparator=EnumPilotBarComparator.GE, threshold=1.0
    ),
)


def frozen_manifest(**changes: object) -> ModelPilotManifest:
    base: dict[str, object] = {
        "pilot": "memory-cognee-stage1",
        "bar": BAR,
        "bar_hash": compute_bar_hash(BAR),
        "frozen_at": FROZEN_AT,
        "started_at": STARTED_AT,
    }
    base.update(changes)
    return ModelPilotManifest.model_validate(base)


def request(
    manifest: ModelPilotManifest,
    results: dict[str, float] | None = None,
    extension_requested: bool = False,
) -> ModelPilotManifestCheckRequest:
    return ModelPilotManifestCheckRequest(
        correlation_id=CORRELATION_ID,
        manifest=manifest,
        results=results,
        extension_requested=extension_requested,
    )
