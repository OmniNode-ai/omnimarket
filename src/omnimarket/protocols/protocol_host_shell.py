# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ProtocolHostShell: run one script on a host (OMN-19130).

The bring-up plan's transport port. The script reaches the host on stdin, never
in an argv, so a value it carries is never visible in a process list. The ssh
and local adapter is ``handlers/handler_host_shell_transport.py``; the scripted
adapter for tests is ``handlers/handler_host_shell_scripted.py``.
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.host_bringup.model_host_shell import (
    ModelHostShellResult,
    ModelHostTransport,
)


class ProtocolHostShell(Protocol):
    def run(
        self,
        transport: ModelHostTransport,
        script: str,
        *,
        privileged: bool,
        timeout_s: int,
    ) -> ModelHostShellResult:
        """Run ``script`` through ``sh -s`` on the host, under ``sudo -n`` when privileged."""
        ...


__all__ = ["ProtocolHostShell"]
