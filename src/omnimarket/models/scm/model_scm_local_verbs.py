# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``onex scm`` verbs answered from local state alone (OMN-20913, OMN-23846).

The table lives with the verb models so the verb dispatch module, a baselined file that only
shrinks, takes no new rows; ``handler_scm_dispatch`` re-exports it.
"""

from __future__ import annotations

from typing import Final

from omnimarket.models.scm.model_scm_command import (
    EnumScmLocalRead,
    ModelScmLocalVerb,
)

#: Verbs no node contract has an operation for: answered from the canonical
#: clones and the watcher state alone. The measured gh read refusals they
#: replace (the shared call log) are PR commit lists, file contents, one commit
#: and one commit's check runs.
SCM_LOCAL_VERBS: Final[tuple[ModelScmLocalVerb, ...]] = (
    ModelScmLocalVerb(
        verb="pr-commits",
        read=EnumScmLocalRead.PR_COMMITS,
        replaces="gh api repos/<o>/<r>/pulls/<n>/commits",
    ),
    ModelScmLocalVerb(
        verb="file-read",
        read=EnumScmLocalRead.FILE_CONTENTS,
        replaces="gh api repos/<o>/<r>/contents/<path>",
    ),
    ModelScmLocalVerb(
        verb="commit-view",
        read=EnumScmLocalRead.COMMIT_VIEW,
        replaces="gh api repos/<o>/<r>/commits/<ref>",
    ),
    ModelScmLocalVerb(
        verb="commit-checks",
        read=EnumScmLocalRead.COMMIT_CHECKS,
        replaces="gh api repos/<o>/<r>/commits/<sha>/check-runs",
    ),
    ModelScmLocalVerb(
        verb="red-facts",
        read=EnumScmLocalRead.RED_FACTS,
        replaces="onex scm job-log / job-log-cached raw log reads for the failing line",
    ),
)

__all__: list[str] = ["SCM_LOCAL_VERBS"]
