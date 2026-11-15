# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host configuration of the fence server: which ledger it reads (OMN-20682)."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ModelTriageFencesOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger: Path
    # The PR watcher's state file the open-PR fences read (OMN-20911). Unset means
    # ``$ONEX_PR_WATCHER_STATE``, else ``$OMNI_HOME/.claude_scratch/pr-watcher/state.json``.
    watcher_state: Path | None = None
