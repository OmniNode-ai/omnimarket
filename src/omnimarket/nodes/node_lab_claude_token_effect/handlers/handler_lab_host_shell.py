# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runs a script on a lab host the way landing placement does: ssh in batch mode for a lab
host, ``bash -c`` in a scrubbed environment for the launching host. A token rides stdin."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from omnimarket.nodes.node_lab_claude_token_effect.models.model_lab_claude_token_request import (
    ModelLabTokenHost,
)
from omnimarket.nodes.node_lab_claude_token_effect.protocols.protocol_lab_token_ports import (
    ShellAnswer,
)

SSH_OPTS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=5",
    "-o",
    "ServerAliveInterval=30",
)
LOCAL_EXEC_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"


def host_argv(host: ModelLabTokenHost, script: str) -> list[str]:
    """The argv that runs ``script`` on ``host``; it never carries a token."""
    if not host.local:
        assert host.target is not None
        return ["ssh", *SSH_OPTS, host.target, script]
    home = os.environ.get("HOME") or str(Path.home())
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or Path(home).name
    return [
        "/usr/bin/env",
        "-i",
        f"HOME={home}",
        f"USER={user}",
        f"LOGNAME={user}",
        f"PATH={LOCAL_EXEC_PATH}",
        "/bin/bash",
        "-c",
        script,
    ]


class HandlerLabHostShell:
    """``ProtocolLabHostShell`` over ssh / bash. Output is returned to the handler, which
    only judges it; stderr is folded in so a refusal reason is never lost."""

    def run(
        self, host: ModelLabTokenHost, script: str, *, stdin: str = "", timeout_s: float
    ) -> ShellAnswer:
        try:
            done = subprocess.run(
                host_argv(host, script),
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ShellAnswer(124, f"timed out after {timeout_s:.0f}s")
        except OSError as exc:
            return ShellAnswer(255, str(exc))
        return ShellAnswer(done.returncode, (done.stdout or "") + (done.stderr or ""))
