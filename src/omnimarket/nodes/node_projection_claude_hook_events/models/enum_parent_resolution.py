# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How an agent span's parent was established (OMN-19513)."""

from __future__ import annotations

from enum import StrEnum


class EnumParentResolution(StrEnum):
    """Why ``parent_agent_id`` holds the value it holds.

    A null ``parent_agent_id`` means two different things -- "spawned by the
    main thread" and "we could not tell" -- and a reader cannot separate them
    from the null alone. This column is what separates them.

    ``MAIN_THREAD``: the spawning ``PreToolUse`` was seen and carried no
    ``agent_id``. ``RESOLVED``: it was seen and carried one, so this is a
    nested subagent. ``WORKFLOW``: the harness sidecar was a Workflow sidecar
    (a ``workflow_run_id``, no spawning tool call). ``UNKNOWN``: the sidecar was
    unreadable at emit time, or the spawning ``PreToolUse`` has not been seen.
    ``UNKNOWN`` is repaired in place when the parent arrives later.
    """

    MAIN_THREAD = "main_thread"
    RESOLVED = "resolved"
    WORKFLOW = "workflow"
    UNKNOWN = "unknown"
