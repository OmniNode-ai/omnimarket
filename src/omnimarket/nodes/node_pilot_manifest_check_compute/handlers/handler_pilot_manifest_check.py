# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pilot manifest check (OMN-22979, rebuild plan 3.5 row 16 and 7.2).

A pilot starts only when its manifest carries a bar and the hash of that bar,
the hash matches the bar and the bar was frozen no later than the start. A pilot
that fails its frozen bar reads rejected; a request to extend it is refused and
recorded, and no state reads extended.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_bar_comparator import (
    EnumPilotBarComparator,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_check_finding import (
    EnumPilotCheckFinding,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_status import (
    EnumPilotStatus,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_bar_criterion import (
    ModelPilotBarCriterion,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest_check_overlay import (
    ModelPilotManifestCheckOverlay,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest_check_request import (
    ModelPilotManifestCheckRequest,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest_check_result import (
    ModelPilotManifestCheckResult,
)

_F = EnumPilotCheckFinding
_C = EnumPilotBarComparator


def compute_bar_hash(
    bar: Sequence[ModelPilotBarCriterion], algorithm: str = "sha256"
) -> str:
    """Hash the bar's criteria in a canonical form: sorted by metric, sorted keys."""
    canonical = json.dumps(
        [c.model_dump(mode="json") for c in sorted(bar, key=lambda c: c.metric)],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.new(algorithm, canonical.encode("utf-8")).hexdigest()


def _met(criterion: ModelPilotBarCriterion, value: float) -> bool:
    if criterion.comparator is _C.GE:
        return value >= criterion.threshold
    if criterion.comparator is _C.LE:
        return value <= criterion.threshold
    return value == criterion.threshold


class HandlerPilotManifestCheck:
    """Compute: manifest and results in, verdict out. No I/O."""

    def __init__(self, overlay: ModelPilotManifestCheckOverlay | None = None) -> None:
        self._overlay = overlay or ModelPilotManifestCheckOverlay()

    def handle(
        self, request: ModelPilotManifestCheckRequest
    ) -> ModelPilotManifestCheckResult:
        manifest = request.manifest
        findings: list[EnumPilotCheckFinding] = []
        computed: str | None = None
        if not manifest.bar:
            findings.append(_F.BAR_MISSING)
        else:
            computed = compute_bar_hash(manifest.bar, self._overlay.hash_algorithm)
        if manifest.bar_hash is None or not manifest.bar_hash.strip():
            findings.append(_F.HASH_MISSING)
        elif computed is not None and manifest.bar_hash.strip().lower() != computed:
            findings.append(_F.HASH_MISMATCH)
        if self._overlay.require_frozen_before_start and (
            manifest.frozen_at is None or manifest.frozen_at > manifest.started_at
        ):
            findings.append(_F.NOT_FROZEN_BEFORE_START)

        if findings:
            return ModelPilotManifestCheckResult(
                correlation_id=request.correlation_id,
                pilot=manifest.pilot,
                check_passed=False,
                findings=tuple(findings),
                computed_bar_hash=computed,
                status=EnumPilotStatus.REFUSED,
            )
        if request.results is None:
            return ModelPilotManifestCheckResult(
                correlation_id=request.correlation_id,
                pilot=manifest.pilot,
                check_passed=True,
                computed_bar_hash=computed,
                status=EnumPilotStatus.READY,
            )
        failed = tuple(
            c.metric
            for c in manifest.bar
            if (
                self._overlay.missing_metric_fails_bar
                and c.metric not in request.results
            )
            or (c.metric in request.results and not _met(c, request.results[c.metric]))
        )
        rejected = bool(failed)
        return ModelPilotManifestCheckResult(
            correlation_id=request.correlation_id,
            pilot=manifest.pilot,
            check_passed=True,
            computed_bar_hash=computed,
            status=EnumPilotStatus.REJECTED if rejected else EnumPilotStatus.ACCEPTED,
            failed_metrics=failed,
            extension_refused=rejected and request.extension_requested,
        )
