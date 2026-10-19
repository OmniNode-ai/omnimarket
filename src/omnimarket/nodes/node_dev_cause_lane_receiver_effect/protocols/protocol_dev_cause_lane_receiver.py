# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two ports of the dev cause-lane receiver (OMN-17427)."""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.dev_head_red import ModelDevCauseLaneRequest
from omnimarket.nodes.node_dev_cause_lane_receiver_effect.models import (
    ModelRunnerReceipt,
)


class ProtocolDispatchKeyLedger(Protocol):
    """Durable memory of the dispatch keys this host has taken, across restarts."""

    def claim(self, dispatch_key: str, lane: str) -> bool:
        """Take the key. True for the first caller ever, False for every later one."""
        ...

    def record(self, dispatch_key: str, outcome: str, detail: str) -> None:
        """Write what became of a claimed key, so a failed start stays visible."""
        ...


class ProtocolCauseLaneStarter(Protocol):
    """The remote-lane runner: start one detached cause lane from a brief."""

    def start(
        self, request: ModelDevCauseLaneRequest, brief: str
    ) -> ModelRunnerReceipt: ...
