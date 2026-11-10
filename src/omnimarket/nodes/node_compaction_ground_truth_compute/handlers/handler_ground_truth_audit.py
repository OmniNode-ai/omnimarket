# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Audit of a derived set: a strict selection, and TE.1 propositions covered (AC1, AC5).

Pure. The plan author audits, per kind, the row stamps that must be in the set (positives)
and the closed, irrelevant and out-of-scope rows that must be absent (negatives). The audit
fails on a missing positive, a present negative, an item whose rows hit no positive of its
kind (a kind with no audited list has no positives, so any item of it is unexpected), or a
set whose size equals the number of rows in the prefix. It also fails on a labelled
proposition whose ``covered_by`` row (``<TYPE> <stamp>``) is in no derived item.
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_compaction_ground_truth_compute.models import (
    EnumGroundTruthKind,
    ModelAuditFinding,
    ModelGroundTruthAuditRequest,
    ModelGroundTruthAuditResult,
)

__all__ = ["HandlerCompactionGroundTruthAudit"]

_COVER = re.compile(
    r"^(?P<type>[A-Z][A-Z-]*)\s+(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\b"
)


class HandlerCompactionGroundTruthAudit:
    def handle(
        self, request: ModelGroundTruthAuditRequest
    ) -> ModelGroundTruthAuditResult:
        result = request.result
        derived: dict[EnumGroundTruthKind, set[str]] = {
            kind: set() for kind in EnumGroundTruthKind
        }
        for item in result.items:
            derived[item.kind].update(row.stamp for row in item.rows)
        audited = {e.kind: e for e in request.expectations}

        missing: list[ModelAuditFinding] = []
        present: list[ModelAuditFinding] = []
        unexpected: list[ModelAuditFinding] = []
        for kind in EnumGroundTruthKind:
            positives = set(audited[kind].positives) if kind in audited else set()
            negatives = set(audited[kind].negatives) if kind in audited else set()
            missing.extend(
                ModelAuditFinding(
                    kind=kind, stamp=stamp, detail="positive not in the set"
                )
                for stamp in sorted(positives - derived[kind])
            )
            present.extend(
                ModelAuditFinding(
                    kind=kind, stamp=stamp, detail="negative is in the set"
                )
                for stamp in sorted(negatives & derived[kind])
            )
            unexpected.extend(
                ModelAuditFinding(
                    kind=kind,
                    stamp=item.rows[0].stamp,
                    detail=f"item {item.key} hits no audited positive",
                )
                for item in result.items
                if item.kind is kind and not positives & {r.stamp for r in item.rows}
            )

        row_types = {
            (row.stamp, row.row_type) for item in result.items for row in item.rows
        }
        uncovered: list[ModelAuditFinding] = []
        for proposition in request.propositions:
            if proposition.covered_by is None:
                continue
            match = _COVER.match(proposition.covered_by)
            ok = (
                match is not None
                and (
                    match.group("stamp"),
                    match.group("type"),
                )
                in row_types
            )
            if not ok:
                uncovered.append(
                    ModelAuditFinding(
                        stamp=match.group("stamp") if match else None,
                        proposition_id=proposition.proposition_id,
                        detail=(
                            f"{proposition.case_id} boundary {proposition.boundary}: "
                            f"covering row {proposition.covered_by!r} is in no derived item"
                        ),
                    )
                )

        total = len(result.items)
        size_equals_prefix = total in (result.prefix_row_count, result.rows_as_of_count)
        return ModelGroundTruthAuditResult(
            set_hash=result.set_hash,
            items_total=total,
            prefix_row_count=result.prefix_row_count,
            rows_as_of_count=result.rows_as_of_count,
            missing_positives=tuple(missing),
            present_negatives=tuple(present),
            unexpected_items=tuple(unexpected),
            uncovered_propositions=tuple(uncovered),
            size_equals_prefix=size_equals_prefix,
            passed=not (
                missing or present or unexpected or uncovered or size_equals_prefix
            ),
        )
