# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerClassifyHeadChecks: the ``classify_head_checks`` operation seam.

A pure definition-B compute over one PR head's check facts, returning the
PR-level verdict the landing workflow's reducer acts on. This change freezes
the operation's contract and models only. The classification itself is the
next task; until then the handler raises ``NotImplementedError``, and the
contract does not route any message to it (it is declared under
``operations``, not ``handler_routing``), so no runtime dispatches it.

The per-check reason codes it will produce come from
``omnimarket.merge_control.reason_code_classifier``, not from a second
vocabulary.
"""

from __future__ import annotations

from typing import Literal

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_head_check_facts import (
    ModelHeadCheckFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_head_check_verdict import (
    ModelHeadCheckVerdict,
)


class HandlerClassifyHeadChecks:
    """Classifies one head's check facts into one verdict (not yet implemented)."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(self, request: ModelHeadCheckFacts) -> ModelHeadCheckVerdict:
        """Return the verdict for ``request``'s head. Not implemented in the seam."""
        raise NotImplementedError(  # stub-ok: frozen seam; the classifier lands in OMN-19830
            "classify_head_checks is a frozen seam; its classifier lands in the "
            f"next task (head {request.repository}#{request.pr_number}@{request.head_sha})"
        )


__all__: list[str] = ["HandlerClassifyHeadChecks"]
