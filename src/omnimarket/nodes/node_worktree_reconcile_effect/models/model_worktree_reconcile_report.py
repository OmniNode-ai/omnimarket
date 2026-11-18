# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick's report: the run result and the units its unit reader skipped (OMN-24901)."""

from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileRunResult
from omnimarket.models.model_skipped_unit import ModelSkippedUnit


class ModelWorktreeReconcileRunReport(ModelWorktreeReconcileRunResult):
    """The run result with each skipped unit, its reason and the file path.

    A skip is not a failure and is not counted in ``completed_event.failures``.
    """

    skipped_units: tuple[ModelSkippedUnit, ...] = ()
