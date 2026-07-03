# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLedgerStats — pure deterministic ledger chain statistics aggregator.

No I/O, no filesystem access, no side effects. All input arrives via
ModelLedgerStatsRequest. Mirrors the logic of _compute_ledger_stats() in
onex-self-extending-agent/src/dashboard_server.py but operates on
pre-parsed ModelChainRecord instances rather than raw JSON files.
"""

from __future__ import annotations

from omnimarket.nodes.node_ledger_stats_compute.models.model_ledger_stats import (
    ModelLedgerStats,
    ModelModelStats,
)
from omnimarket.nodes.node_ledger_stats_compute.models.model_ledger_stats_request import (
    ModelLedgerStatsRequest,
)


class HandlerLedgerStats:
    """Aggregate chain event records into ledger statistics.

    Pure compute — no external dependencies, no I/O.
    """

    def handle(self, payload: ModelLedgerStatsRequest) -> ModelLedgerStats:
        """Compute ledger statistics from a batch of chain records.

        Args:
            payload: Contains a tuple of ModelChainRecord instances. Named
                ``payload`` (OMN-13276) so the RuntimeLocal adapter's
                single-parameter dispatch passes the validated request
                positionally instead of keyword-fanning the model fields.

        Returns:
            ModelLedgerStats with aggregated counts and rates.
        """
        request = payload
        total_chains = 0
        pass_count = 0
        fail_count = 0
        ledger_hit_count = 0

        # {model_id: {passed, failed, _total_attempts, _runs}}
        by_model_acc: dict[str, dict[str, int | float]] = {}

        for record in request.chains:
            total_chains += 1

            if record.contract_passed:
                pass_count += 1
            else:
                fail_count += 1

            if record.ledger_hit or record.has_reference_chains:
                ledger_hit_count += 1

            entry = by_model_acc.setdefault(
                record.model_id,
                {"passed": 0, "failed": 0, "_total_attempts": 0, "_runs": 0},
            )
            if record.contract_passed:
                entry["passed"] = int(entry["passed"]) + 1
            else:
                entry["failed"] = int(entry["failed"]) + 1
            entry["_total_attempts"] = (
                int(entry["_total_attempts"]) + record.attempt_count
            )
            entry["_runs"] = int(entry["_runs"]) + 1

        by_model: dict[str, ModelModelStats] = {}
        for model_id, entry in by_model_acc.items():
            runs = int(entry["_runs"])
            avg = round(int(entry["_total_attempts"]) / runs, 2) if runs else 0.0
            by_model[model_id] = ModelModelStats(
                passed=int(entry["passed"]),
                failed=int(entry["failed"]),
                avg_attempts=avg,
            )

        ledger_hit_rate = (
            round(ledger_hit_count / total_chains, 2) if total_chains else 0.0
        )

        return ModelLedgerStats(
            total_chains=total_chains,
            pass_count=pass_count,
            fail_count=fail_count,
            by_model=by_model,
            ledger_hit_rate=ledger_hit_rate,
        )


__all__ = ["HandlerLedgerStats"]
