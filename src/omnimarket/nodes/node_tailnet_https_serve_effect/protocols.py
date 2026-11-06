# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three seams of the listener: the upstream probe, the serve backend, the report."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_tailnet_https_serve_effect.models import ModelServeResult


class ServeStepError(Exception):
    """A seam could not do its step. ``step`` is ``probe``, ``serve``, ``env`` or ``report``."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step


class ProtocolUpstreamProbe(Protocol):
    def answers(self, upstream_url: str, path: str, timeout_seconds: int) -> bool:
        """True when the upstream answers the path with a status below 400."""


class ProtocolServeBackend(Protocol):
    def served_target(self, https_port: int) -> str:
        """The proxy target served on the https port, empty when nothing is served there."""

    def serve(self, https_port: int, upstream_url: str) -> None:
        """Serve the upstream on the https port, in the background."""

    def off(self, https_port: int) -> None:
        """Turn off the listener on that one port and no other."""

    def place_env(self, path: str, text: str) -> None:
        """Write the file the boot-time unit reads."""

    def remove_env(self, path: str) -> None: ...


class ProtocolServeReporter(Protocol):
    def report(self, result: ModelServeResult) -> None: ...
