# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_tech_debt_sweep_orchestrator [OMN-12212].

ORCHESTRATOR node. Scans all Python repos under omni_home for 6 categories of
tech debt (type-ignore, noqa, todo-fixme, any-types, skipped-tests,
stale-ignores), deduplicates findings against open Linear tickets via
content-hash dedup keys, and creates one Linear epic per category with
closeable tickets grouped by repo and top-level source directory.

STUB — not yet implemented.
"""

from omnimarket.nodes.node_tech_debt_sweep_orchestrator.models.model_tech_debt_sweep_request import (
    ModelTechDebtSweepRequest,
    ModelTechDebtSweepResult,
)


class HandlerTechDebtSweepOrchestrator:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(self, request: ModelTechDebtSweepRequest) -> ModelTechDebtSweepResult:
        raise NotImplementedError(  # stub-ok
            "node_tech_debt_sweep_orchestrator is not yet implemented (OMN-12212). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
