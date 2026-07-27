# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelThresholdSpec — a per-signal abort/warn threshold declaration (OMN-14735/OMN-14948, B10).

The **numeric** ``warn_threshold``/``abort_threshold`` values are a required
input from the managed-staging contractor deliverable A6 ("Supply monitoring
thresholds" — see
``docs/plans/2026-07-17-managed-staging-verified-state-and-task-split.md``).
A6 was delivered 2026-07-22 (OMN-14732) for all five signal domains; the real
values are declared in ``thresholds.yaml`` and resolved to instances of this
model by
:func:`omnimarket.nodes.node_canary_monitoring_gate_compute.handlers.threshold_config_loader.default_threshold_specs`
(OMN-14948) — never as a literal number typed directly into Python source.

Both fields default to ``None``, representing an **unresolved** threshold as
a first-class, typed state rather than a fabricated placeholder number. This
remains load-bearing beyond the initial A6 handoff: any caller that builds a
spec without real numbers (a future signal domain, a misconfigured request)
still gets an honest unresolved state. ``is_resolved`` reports whether real
numbers have landed; the gate handler treats an unresolved spec as
``UNRESOLVED``, never as "pass" or a guessed number.

Do not set ``warn_threshold``/``abort_threshold`` to a literal number
anywhere in this repository outside ``thresholds.yaml`` without citing a
real source (ticket + contractor artifact reference) in ``source``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_signal_reading import (
    SignalName,
)

# "gte": breach when value >= threshold (e.g. auth-failure rate, replication
#        lag seconds — higher is worse).
# "lte": breach when value <= threshold (e.g. broker in-sync-replica count,
#        available headroom — lower is worse).
Comparison = Literal["gte", "lte"]

# Sentinel for `source` on a threshold spec that has no numeric values yet.
# Never a fabricated approver/date — an explicit "nothing has landed" marker.
UNRESOLVED_SOURCE = "A6_PENDING"


class ModelThresholdSpec(BaseModel):
    """Declares the warn/abort thresholds for one monitoring signal domain.

    Attributes:
        signal_name: Which monitoring signal domain this threshold governs.
        comparison: Direction in which a breach is detected (see module
            docstring).
        warn_threshold: Numeric warn-level threshold in the reading's unit.
            ``None`` when A6 has not yet supplied a real number.
        abort_threshold: Numeric abort-level threshold in the reading's unit.
            ``None`` when A6 has not yet supplied a real number.
        source: Free-text provenance for the numeric values (e.g. an A6
            ticket/artifact citation). Defaults to :data:`UNRESOLVED_SOURCE`
            when no numbers are set.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_name: SignalName = Field(
        ..., description="Which monitoring signal domain this threshold governs."
    )
    comparison: Comparison = Field(
        ...,
        description="Breach direction: gte (higher is worse) or lte (lower is worse).",
    )
    warn_threshold: float | None = Field(
        default=None,
        description="Numeric warn-level threshold. None until A6 supplies it.",
    )
    abort_threshold: float | None = Field(
        default=None,
        description="Numeric abort-level threshold. None until A6 supplies it.",
    )
    source: str = Field(
        default=UNRESOLVED_SOURCE,
        min_length=1,
        description="Provenance for the numeric values (ticket/artifact citation).",
    )

    @property
    def is_resolved(self) -> bool:
        """True only when both numeric thresholds have been supplied by A6."""
        return self.warn_threshold is not None and self.abort_threshold is not None

    @model_validator(mode="after")
    def _validate_resolution_consistency(self) -> ModelThresholdSpec:
        has_warn = self.warn_threshold is not None
        has_abort = self.abort_threshold is not None
        if has_warn != has_abort:
            raise ValueError(
                "warn_threshold and abort_threshold must both be set or both "
                "be None — a half-resolved threshold is not a valid A6 input"
            )
        if has_warn and self.source == UNRESOLVED_SOURCE:
            raise ValueError(
                "a resolved threshold (warn/abort both set) must cite a real "
                f"source, not the unresolved sentinel {UNRESOLVED_SOURCE!r}"
            )
        return self


__all__ = ["UNRESOLVED_SOURCE", "Comparison", "ModelThresholdSpec"]
