# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Mark approved work from supplied completion facts without I/O (OMN-17427)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from ..models import (
    ModelApprovedWorkFreshnessRequest,
    ModelApprovedWorkFreshnessResult,
    ModelApprovedWorkTicketFacts,
    ModelApprovedWorkVerdict,
)


class HandlerApprovedWorkFreshness:
    """Deterministically fold Linear and acceptance facts into declared rows."""

    def handle(
        self, request: ModelApprovedWorkFreshnessRequest
    ) -> ModelApprovedWorkFreshnessResult:
        """Return fresh rows, refusing invalid evidence before producing a result."""
        try:
            checked_at = datetime.fromisoformat(request.checked_at)
        except ValueError as exc:
            raise ValueError("checked_at must be a UTC ISO timestamp") from exc
        if checked_at.tzinfo is None or checked_at.utcoffset() != timedelta(0):
            raise ValueError("checked_at must be a UTC ISO timestamp")

        facts: dict[str, ModelApprovedWorkTicketFacts] = {}
        for supplied_fact in request.facts:
            if supplied_fact.ticket in facts:
                raise ValueError(f"{supplied_fact.ticket}: duplicate ticket facts")
            if supplied_fact.acceptance_status not in {"", "pass", "partial", "open"}:
                raise ValueError(f"{supplied_fact.ticket}: invalid acceptance_status")
            for evidence in supplied_fact.evidence:
                if re.fullmatch(r"[A-Za-z0-9_.-]+#[0-9]+", evidence.pr) is None:
                    raise ValueError(
                        f"{supplied_fact.ticket}: invalid evidence PR {evidence.pr!r}"
                    )
                if re.fullmatch(r"[0-9a-f]{40}", evidence.commit) is None:
                    raise ValueError(
                        f"{supplied_fact.ticket}: invalid evidence commit {evidence.commit!r}"
                    )
            facts[supplied_fact.ticket] = supplied_fact

        rows: list[dict[str, object]] = []
        verdicts: list[ModelApprovedWorkVerdict] = []
        for original in request.rows:
            row = dict(original)
            ticket = str(row.get("ticket", ""))
            fact = facts.get(ticket)
            basis = "unchanged"
            if fact is not None:
                evidence_text = "; ".join(
                    f"{e.pr}@{e.commit[:12]}" for e in fact.evidence
                )
                if fact.linear_state_type in {"completed", "canceled", "duplicate"}:
                    basis = "linear-state"
                    row["done"] = True
                    row["done_basis"] = basis
                    row["done_evidence"] = f"linear:{fact.linear_state_name}" + (
                        f"; {evidence_text}" if evidence_text else ""
                    )
                    row.pop("remaining", None)
                elif fact.acceptance_status == "pass":
                    if not fact.evidence:
                        raise ValueError(f"{ticket}: acceptance pass requires evidence")
                    basis = "acceptance-on-main"
                    row["done"] = True
                    row["done_basis"] = basis
                    row["done_evidence"] = evidence_text
                    row.pop("remaining", None)
                elif fact.acceptance_status == "partial":
                    if (
                        not fact.remaining.strip()
                        or "\r" in fact.remaining
                        or "\n" in fact.remaining
                        or len(fact.remaining) > 300
                    ):
                        raise ValueError(
                            f"{ticket}: partial acceptance requires one remaining line "
                            "of at most 300 characters"
                        )
                    basis = "partial"
                    row["done"] = False
                    row.pop("done_basis", None)
                    row.pop("done_evidence", None)
                    row["remaining"] = fact.remaining
            done = row.get("done")
            verdicts.append(
                ModelApprovedWorkVerdict(
                    row_id=str(row.get("id", "")),
                    ticket=ticket,
                    done=done if isinstance(done, bool) else None,
                    basis=basis,
                    evidence=str(row.get("done_evidence", "")),
                    remaining=str(row.get("remaining", "")),
                    changed=row != original,
                )
            )
            rows.append(row)
        return ModelApprovedWorkFreshnessResult(tuple(rows), tuple(verdicts))
