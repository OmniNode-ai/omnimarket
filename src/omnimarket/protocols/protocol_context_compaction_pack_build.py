# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The package build the compaction orchestrator calls (OMN-21113, TA.11).

The live implementation is the context-pack orchestrator over the
``compaction_orchestrator`` profile. A build reports failure as a typed
outcome; an exception from it is classed ``build_error`` by the caller.
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.context_compaction.model_context_compaction_build_outcome import (
    ModelContextCompactionBuildOutcome,
)
from omnimarket.models.context_compaction.model_context_compaction_requested import (
    ModelContextCompactionRequested,
)


class ProtocolContextCompactionPackBuild(Protocol):
    def build(
        self, request: ModelContextCompactionRequested
    ) -> ModelContextCompactionBuildOutcome: ...


__all__: list[str] = ["ProtocolContextCompactionPackBuild"]
