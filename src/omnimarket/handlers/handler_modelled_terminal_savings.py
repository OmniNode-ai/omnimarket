# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The modelled saving of a delegate-skill terminal that reports none (OMN-21208).

A harness-tier terminal (the harness-delegate skill publishes its own) carries
its served tokens and recorded cost but no ``cost_savings_usd``, so the reported
path writes no row and both savings totals leave the run out, while the run's
own decision row shows its modelled saving. This prices the same run through
the canonical metering compute, the one definition of a per-run saving, against
the same baseline the local metering refresh selects. No prices or money
arithmetic are defined here.
"""

from __future__ import annotations

from decimal import Decimal

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillSavingsProjection,
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_metering_summary_compute import (
    HandlerMeteringSummary,
    ModelMeteringRecord,
    ModelMeteringSummaryRequest,
    ModelMeteringWindow,
)
from omnimarket.nodes.node_projection_metering_summary import (
    baseline as baseline_pricing,
)
from omnimarket.pricing import build_premium_counterfactual
from omnimarket.projection import sqlite_metering_summary


def modelled_terminal_savings(
    event: ModelDelegateSkillTerminalProjection,
) -> ModelDelegateSkillSavingsProjection | None:
    """The run's modelled saving row, or None when it has none to claim.

    Only a completed run that passed its gate, served tokens, and neither
    reported a saving nor declared its baseline unresolved is priced here.

    The row's ``savings_method`` reads ``measured`` for the same reason a local
    run's does: the saving is computed from the run's recorded tokens and
    recorded cost against the pinned baseline price (OMN-13629's measurement),
    which is the modelled counterfactual every savings row carries. Labelling it
    ``estimated`` would drop it from the measured-only totals it belongs in.
    """
    metrics = event.metrics
    if (
        metrics.cost_savings_usd is not None
        or event.baseline_state != "RESOLVED"
        or event.status != "completed"
        or not event.quality_gate_passed
        or metrics.input_tokens + metrics.output_tokens == 0
    ):
        return None
    model_local = event.model_name or event.provider
    if not model_local:
        return None
    selection = sqlite_metering_summary.resolve_metering_baseline_model()
    baseline = baseline_pricing.resolve_baseline(selection.model)
    if baseline is None:
        return None
    local_cost_usd = Decimal(str(metrics.cost_usd))
    occurred_at = event.emitted_at
    run = (
        HandlerMeteringSummary()
        .handle(
            ModelMeteringSummaryRequest(
                window=ModelMeteringWindow(label="run", end=occurred_at),
                records=(
                    ModelMeteringRecord(
                        correlation_id=str(event.correlation_id),
                        occurred_at=occurred_at,
                        model=model_local,
                        task_type=event.task_type,
                        tokens_in=metrics.input_tokens,
                        tokens_out=metrics.output_tokens,
                        spend_usd=local_cost_usd,
                    ),
                ),
                baseline=baseline,
            )
        )
        .by_run[0]
    )
    if run.savings_usd is None or run.savings_usd <= 0:
        return None
    return ModelDelegateSkillSavingsProjection(
        event_timestamp=occurred_at.replace(microsecond=0),
        session_id=event.correlation_id,
        model_local=model_local,
        model_cloud_baseline=selection.model,
        baseline_source=selection.selection_case,
        pricing_manifest_version=selection.pricing_manifest_version,
        local_cost_usd=local_cost_usd,
        cloud_cost_usd=local_cost_usd + run.savings_usd,
        savings_usd=run.savings_usd,
        repo_name=event.repo_name,
        machine_id=event.machine_id,
        tenant_id=event.tenant_id,
        task_type=event.task_type,
        prompt_tokens=metrics.input_tokens,
        completion_tokens=metrics.output_tokens,
    )


def apply_modelled_terminal_savings(
    row: dict[str, object], event: ModelDelegateSkillTerminalProjection
) -> None:
    """Retain a derived saving and baseline after preserving stored evidence.

    The savings writer already derives this figure for a harness terminal that
    reports none. Its matching delegation row must carry the same baseline.
    Explicit or previously pinned evidence takes precedence over derivation.
    """
    if row.get("premium_counterfactual") is not None or row.get("cost_savings_usd"):
        return
    projection = modelled_terminal_savings(event)
    if projection is None:
        return
    counterfactual = build_premium_counterfactual(
        prompt_tokens=event.metrics.input_tokens,
        completion_tokens=event.metrics.output_tokens,
        premium_model=projection.model_cloud_baseline,
    )
    if counterfactual is None:
        return
    if counterfactual.counterfactual_cost_usd != projection.cloud_cost_usd:
        raise ValueError("modelled saving and its pinned baseline disagree")
    row["premium_counterfactual"] = counterfactual.model_dump(mode="json")
    row["cost_savings_usd"] = projection.savings_usd
    row["pricing_manifest_version"] = projection.pricing_manifest_version
