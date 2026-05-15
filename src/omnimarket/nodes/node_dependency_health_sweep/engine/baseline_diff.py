# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Baseline diff engine for node_dependency_health_sweep.

Computes the delta between the current set of findings and a persisted baseline
snapshot. The composite key prevents collisions when rules or graphify versions
change:

    (repo, finding_type, severity, file_path, symbol, detail_hash,
     graphify_version, rule_version)

detail_hash = hashlib.sha256(finding.detail.encode()).hexdigest()[:16]

Returns None (no diff) when baseline_path is None or the file is absent.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_finding import (
    ModelDepHealthFinding,
)
from omnimarket.nodes.node_dependency_health_sweep.models.model_graph_types import (
    ModelBaselineSnapshot,
    ModelDiffResult,
)

logger = logging.getLogger(__name__)


def _composite_key(
    finding: ModelDepHealthFinding,
    graphify_version: str,
) -> tuple[str, str, str, str | None, str | None, str, str, str]:
    detail_hash = hashlib.sha256(finding.detail.encode()).hexdigest()[:16]
    return (
        finding.repo,
        finding.finding_type.value,
        finding.severity.value,
        finding.file_path,
        finding.symbol,
        detail_hash,
        graphify_version,
        finding.rule_version,
    )


class BaselineDiffEngine:
    """Compare current findings against a persisted baseline snapshot."""

    def diff(
        self,
        current: list[ModelDepHealthFinding],
        baseline_path: Path | None,
        current_graphify_version: str = "",
    ) -> ModelDiffResult | None:
        """Compute new vs. resolved findings relative to baseline.

        Args:
            current: Findings produced by the current sweep run.
            baseline_path: Path to the JSON baseline file. If None or absent,
                returns None (caller treats as "no baseline").
            current_graphify_version: Version string for current graphify run.
                Used in the composite key so findings produced by a different
                graphify version are treated as new.

        Returns:
            ModelDiffResult with new_findings, resolved_findings, and delta,
            or None when no baseline comparison is possible.
        """
        if baseline_path is None:
            return None

        if not baseline_path.exists():
            return None

        try:
            raw = json.loads(baseline_path.read_text(encoding="utf-8"))
            snapshot = ModelBaselineSnapshot.model_validate(raw)
        except Exception:
            logger.warning(
                "Failed to load baseline from %s — treating as absent", baseline_path
            )
            return None

        baseline_graphify_version = snapshot.graphify_version

        baseline_keys = {
            _composite_key(f, baseline_graphify_version) for f in snapshot.findings
        }
        current_keys = {
            _composite_key(f, current_graphify_version or baseline_graphify_version)
            for f in current
        }

        # Map keys → findings for lookup
        baseline_map = {
            _composite_key(f, baseline_graphify_version): f for f in snapshot.findings
        }
        current_map = {
            _composite_key(f, current_graphify_version or baseline_graphify_version): f
            for f in current
        }

        new_keys = current_keys - baseline_keys
        resolved_keys = baseline_keys - current_keys

        new_findings = [current_map[k] for k in new_keys]
        resolved_findings = [baseline_map[k] for k in resolved_keys]
        delta = len(new_findings) - len(resolved_findings)

        return ModelDiffResult(
            new_findings=new_findings,
            resolved_findings=resolved_findings,
            delta=delta,
        )
