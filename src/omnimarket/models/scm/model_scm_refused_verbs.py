# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``onex scm`` verbs that have no node operation and are refused (OMN-24326).

The table lives with the verb models so the verb dispatch module, a baselined file that only
shrinks, takes no new rows; ``handler_scm_dispatch`` re-exports it unchanged.
"""

from __future__ import annotations

from typing import Final

from omnimarket.models.scm.model_scm_command import ModelScmRefusedVerb

LANDING_CONTROLLER: Final[str] = (
    "the landing controller (hand the PR off with /omni:pr-handoff, needs=land)"
)

#: Verbs with no node operation. Not invented here: refused, naming where the
#: work is done today.
SCM_REFUSED_VERBS: Final[tuple[ModelScmRefusedVerb, ...]] = tuple(
    ModelScmRefusedVerb(verb=verb, handled_by=LANDING_CONTROLLER)
    for verb in (
        *("pr-merge", "merge", "arm", "pr-arm"),
        *("run-rerun", "pr-rerun", "update-branch", "pr-update-branch"),
    )
)
