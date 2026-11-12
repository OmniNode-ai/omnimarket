# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Launch the installed Claude Code CLI for `onex claude start` (OMN-23413).

The CLI verifies the gateway binding first; this handler only starts Claude
with the caller's arguments and returns its exit code. It lives here because
every coding-agent harness launch belongs to this node (harness invocation
check), and it is bound in this node's contract under cli_served_handlers.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence


class HandlerClaudeLaunch:
    """Run `claude` with the given arguments; OSError when it cannot start."""

    def handle(self, args: Sequence[str]) -> int:
        return subprocess.run(["claude", *args], check=False).returncode
