# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler that aggregates findings from N models via weighted-union dedup.

This is a COMPUTE handler - pure transformation, no I/O.

The dedup algorithm uses Jaccard similarity on tokenised normalized_message fields
combined with file_path + rule_id matching to identify duplicate findings across
models. Configurable threshold controls the similarity cutoff.

Related:
    - OMN-7795: Finding Aggregator COMPUTE node
    - OMN-7781: Unified LLM Workflow Migration epic
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from omnimarket.nodes.node_finding_aggregator_compute.models.model_finding_aggregator_config import (
    ModelFindingAggregatorConfig,
)
from omnimarket.nodes.node_finding_aggregator_compute.models.model_finding_aggregator_input import (
    ModelFindingAggregatorInput,
    ModelSourceFindings,
)
from omnimarket.nodes.node_finding_aggregator_compute.models.model_finding_aggregator_output import (
    EnumAggregatedVerdict,
    ModelAggregatedFinding,
    ModelFindingAggregatorOutput,
)

logger = logging.getLogger(__name__)

# Severity ordering for promotion (higher index = higher severity)
_SEVERITY_ORDER: dict[str, int] = {
    "hint": 0,
    "info": 1,
    "warning": 2,
    "error": 3,
}


def _tokenize(text: str) -> set[str]:
    """Tokenize a message into lowercase word tokens for Jaccard comparison."""
    return set(text.lower().split())


def _jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Compute Jaccard similarity between two token sets."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union


def _extract_finding_fields(raw: dict[str, object]) -> dict[str, object] | None:
    """Extract and validate required fields from a raw finding dict.

    Returns None if required fields are missing.
    """
    required = ("rule_id", "file_path", "line_start", "severity", "normalized_message")
    for field in required:
        if field not in raw:
            return None
    return {
        "rule_id": str(raw["rule_id"]),
        "file_path": str(raw["file_path"]),
        "line_start": int(str(raw["line_start"])),
        "line_end": int(str(raw["line_end"]))
        if raw.get("line_end") is not None
        else None,
        "severity": str(raw["severity"]).lower(),
        "normalized_message": str(raw["normalized_message"]),
    }


def _higher_severity(a: str, b: str) -> str:
    """Return the higher severity of two severity strings."""
    return a if _SEVERITY_ORDER.get(a, 0) >= _SEVERITY_ORDER.get(b, 0) else b


class _FindingCluster:
    """Mutable cluster that accumulates merged findings during dedup."""

    def __init__(self, fields: dict[str, object], model_name: str) -> None:
        self.rule_id: str = str(fields["rule_id"])
        self.file_path: str = str(fields["file_path"])
        self.line_start: int = int(str(fields["line_start"]))
        self.line_end: int | None = (
            int(str(fields["line_end"])) if fields.get("line_end") is not None else None
        )
        self.severity: str = str(fields["severity"])
        self.normalized_message: str = str(fields["normalized_message"])
        self.tokens: set[str] = _tokenize(self.normalized_message)
        self.source_models: list[str] = [model_name]
        self.merged_count: int = 1

    def matches(
        self,
        fields: dict[str, object],
        tokens: set[str],
        threshold: float,
    ) -> bool:
        """Check if a finding matches this cluster via structural + Jaccard similarity."""
        if str(fields["file_path"]) != self.file_path:
            return False
        if str(fields["rule_id"]) != self.rule_id:
            return False
        return _jaccard_similarity(self.tokens, tokens) >= threshold

    def merge(
        self,
        fields: dict[str, object],
        model_name: str,
        severity_promotes: bool,
    ) -> None:
        """Merge a finding into this cluster."""
        self.merged_count += 1
        if model_name not in self.source_models:
            self.source_models.append(model_name)
        if severity_promotes:
            self.severity = _higher_severity(self.severity, str(fields["severity"]))


def _compute_model_weights(
    sources: tuple[ModelSourceFindings, ...],
    configured_weights: dict[str, float],
) -> dict[str, float]:
    """Compute normalized weights for each model.

    Models with explicit weights use those values. Models without explicit weights
    share the remaining weight equally. If no weights are configured, all models
    get equal weight.
    """
    model_names = [s.model_name for s in sources]
    n = len(model_names)
    if not configured_weights:
        return dict.fromkeys(model_names, 1.0 / n)

    weights: dict[str, float] = {}
    unconfigured: list[str] = []
    total_configured = 0.0

    for name in model_names:
        if name in configured_weights:
            weights[name] = configured_weights[name]
            total_configured += configured_weights[name]
        else:
            unconfigured.append(name)

    if unconfigured:
        remaining = max(0.0, 1.0 - total_configured)
        per_model = remaining / len(unconfigured) if remaining > 0 else 1.0 / n
        for name in unconfigured:
            weights[name] = per_model

    # Normalize so weights sum to 1.0
    total = sum(weights.values())
    if total > 0:
        weights = {k: v / total for k, v in weights.items()}

    return weights


def _compute_weighted_score(
    cluster: _FindingCluster,
    model_weights: dict[str, float],
) -> float:
    """Compute weighted score for a cluster based on model agreement and weights."""
    return min(1.0, sum(model_weights.get(m, 0.0) for m in cluster.source_models))


def _determine_verdict(
    findings: tuple[ModelAggregatedFinding, ...],
) -> EnumAggregatedVerdict:
    """Determine overall verdict from merged findings."""
    if not findings:
        return EnumAggregatedVerdict.CLEAN
    for f in findings:
        if f.severity == "error":
            return EnumAggregatedVerdict.BLOCKING_ISSUE
    return EnumAggregatedVerdict.RISKS_NOTED


class HandlerFindingAggregator:
    """Aggregates findings from N models via weighted-union dedup (Jaccard similarity)."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(
        self,
        correlation_id: UUID,
        input_data: ModelFindingAggregatorInput,
    ) -> ModelFindingAggregatorOutput:
        """Aggregate findings from multiple models.

        Algorithm:
            1. Flatten all findings from all sources with model attribution.
            2. For each finding, check existing clusters for a match using
               file_path + rule_id structural match AND Jaccard similarity
               on normalized_message tokens.
            3. If match found: merge into existing cluster.
            4. If no match: create new cluster.
            5. Convert clusters to ModelAggregatedFinding with weighted scores.
            6. Determine verdict based on merged findings.

        Args:
            correlation_id: Pipeline correlation ID.
            input_data: Findings grouped by source model with config.

        Returns:
            ModelFindingAggregatorOutput with merged findings and verdict.
        """
        config: ModelFindingAggregatorConfig = input_data.config
        sources = input_data.sources

        logger.info(
            "Aggregating findings from %d models (correlation_id=%s, threshold=%.2f)",
            len(sources),
            correlation_id,
            config.jaccard_threshold,
        )

        model_weights = _compute_model_weights(sources, config.model_weights)
        clusters: list[_FindingCluster] = []
        total_input = 0
        skipped = 0

        for source in sources:
            for raw_finding in source.findings:
                total_input += 1
                fields = _extract_finding_fields(dict(raw_finding))
                if fields is None:
                    skipped += 1
                    logger.warning(
                        "Skipping finding from %s: missing required fields",
                        source.model_name,
                    )
                    continue

                tokens = _tokenize(str(fields["normalized_message"]))
                matched = False
                for cluster in clusters:
                    if cluster.matches(fields, tokens, config.jaccard_threshold):
                        cluster.merge(
                            fields,
                            source.model_name,
                            config.severity_promotes_on_conflict,
                        )
                        matched = True
                        break

                if not matched:
                    clusters.append(_FindingCluster(fields, source.model_name))

        if skipped > 0:
            logger.warning("Skipped %d findings with missing required fields", skipped)

        merged_findings = tuple(
            ModelAggregatedFinding(
                rule_id=c.rule_id,
                file_path=c.file_path,
                line_start=c.line_start,
                line_end=c.line_end,
                severity=c.severity,
                normalized_message=c.normalized_message,
                source_models=tuple(c.source_models),
                weighted_score=_compute_weighted_score(c, model_weights),
                merged_count=c.merged_count,
            )
            for c in clusters
        )

        verdict = _determine_verdict(merged_findings)
        duplicates_removed = total_input - len(merged_findings) - skipped

        logger.info(
            "Aggregation complete: %d input -> %d merged (%d duplicates removed), verdict=%s",
            total_input,
            len(merged_findings),
            duplicates_removed,
            verdict.value,
        )

        return ModelFindingAggregatorOutput(
            correlation_id=correlation_id,
            verdict=verdict,
            merged_findings=merged_findings,
            total_input_findings=total_input,
            total_merged_findings=len(merged_findings),
            total_duplicates_removed=max(0, duplicates_removed),
            source_model_count=len(sources),
        )
