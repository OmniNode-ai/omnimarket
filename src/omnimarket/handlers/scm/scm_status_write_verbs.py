# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two commit-status ``onex scm`` verbs served by the GitHub repo write node.

They are declared here, beside the scm handlers, so the dispatch table (a baselined file that only shrinks) does not grow;
``models/scm/model_scm_gateway_verbs.py`` adds them to the table the way it adds the gateway's read verbs.
"""

from __future__ import annotations

from typing import Final

from omnimarket.models.scm import EnumScmNode, ModelScmVerb

_W = EnumScmNode.WRITE

SCM_STATUS_WRITE_VERBS: Final[tuple[ModelScmVerb, ...]] = (
    ModelScmVerb(
        verb="commit-status-create",
        node=_W,
        operation="commit_status_create",
        replaces="gh api --method POST /repos/{o}/{r}/statuses/{sha}",
    ),
    ModelScmVerb(
        verb="required-context-apply",
        node=_W,
        operation="required_context_apply",
        replaces="gh api --method POST /repos/{o}/{r}/branches/{b}/protection/required_status_checks/contexts",
    ),
)

__all__: list[str] = ["SCM_STATUS_WRITE_VERBS"]
