# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one process boundary of the hook delegate effect: running `onex delegate`."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_hook_delegate_effect.models import ModelDelegateCliRun


@runtime_checkable
class ProtocolDelegateCli(Protocol):
    """Runs ``onex <args>`` within a time limit and reports how it ended."""

    def run(self, args: list[str], timeout_seconds: float) -> ModelDelegateCliRun: ...
