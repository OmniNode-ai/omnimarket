# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the foreground Bash guard (shape rules and OMN-20893 wait rules)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelForegroundBashRequest:
    """One Bash call. ``main_session`` is False inside a subagent or workflow lane (``agent_id``).

    ``operator_facing`` is False in a headless session: the wait and fetch rules then do not
    apply, because nobody is waiting on that session's foreground. ``tool_name`` also routes
    WebFetch and WebSearch through this guard without treating their arguments as shell text.

    ``permission_mode`` is the session's permission mode from the hook payload (empty when the
    payload names none). In ``bypassPermissions`` nothing can stop on a prompt, so chaining is not
    refused there (OMN-20893, RULING 2026-10-13T04:14:36Z).
    """

    command: str
    main_session: bool
    operator_facing: bool
    timeout_ms: int | None = None
    permission_mode: str = ""
    tool_name: str = "Bash"
