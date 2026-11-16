# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure classification of one merge: landing, override_merge or merge_without_override."""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.nodes.node_merge_override_alarm_effect.handlers.match_override import (
    match_override,
)
from omnimarket.nodes.node_merge_override_alarm_effect.models import (
    EnumMergeOverrideVerdict,
    ModelLedgerRow,
    ModelMergeOverrideClassification,
    ModelMergeOverrideReceipt,
    ModelMergeOverrideSettings,
)


def classify_merge(
    *,
    repository: str,
    pr_number: int,
    head_sha: str,
    merger: str | None,
    receipt: ModelMergeOverrideReceipt | None,
    override_rows: Sequence[ModelLedgerRow],
    settings: ModelMergeOverrideSettings,
) -> ModelMergeOverrideClassification:
    """A landing merge has a receipt for this head and, where S19 holds, the landing App as merger."""
    app_required = repository in settings.s19_repositories
    if (
        receipt is not None
        and receipt.head_sha == head_sha
        and (not app_required or merger == settings.landing_app_login)
    ):
        return ModelMergeOverrideClassification(
            verdict=EnumMergeOverrideVerdict.LANDING
        )
    override = match_override(override_rows, repository, pr_number, head_sha)
    if override is not None:
        return ModelMergeOverrideClassification(
            verdict=EnumMergeOverrideVerdict.OVERRIDE_MERGE,
            ruling_row_id=override.row_id,
        )
    if receipt is None:
        reason = "no landing receipt and no override row"
    elif receipt.head_sha != head_sha:
        reason = "the landing receipt names another head"
    else:
        reason = f"merged by {merger!r}, not the landing App"
    return ModelMergeOverrideClassification(
        verdict=EnumMergeOverrideVerdict.MERGE_WITHOUT_OVERRIDE, reason=reason
    )


__all__ = ["classify_merge"]
