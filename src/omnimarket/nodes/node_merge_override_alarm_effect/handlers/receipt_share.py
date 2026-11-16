# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Share of merges that had a landing receipt, per source, from the verdict rows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from omnimarket.nodes.node_merge_override_alarm_effect.models import (
    ModelMergeOverrideReceiptShare,
)


def receipt_share(
    verdict_rows: Sequence[Mapping[str, object]],
) -> ModelMergeOverrideReceiptShare:
    per_source: dict[str, tuple[int, int]] = {}
    for row in verdict_rows:
        merges, with_receipt = per_source.get(str(row["source"]), (0, 0))
        per_source[str(row["source"])] = (
            merges + 1,
            with_receipt + (1 if row["receipt_seen"] else 0),
        )
    return ModelMergeOverrideReceiptShare(
        per_source=per_source,
        total=sum(merges for merges, _ in per_source.values()),
        with_receipt=sum(seen for _, seen in per_source.values()),
    )


__all__ = ["receipt_share"]
