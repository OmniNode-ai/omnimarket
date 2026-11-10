# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The backends of the rotation readback's consumer call.

In memory: consumers hold the store's current value unless a test holds one on an old value.
Command: the consumer's own probe command from the overlay, run without a shell, its output
discarded, so a value the consumer holds can never reach a row, an event or a log.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence

from omnimarket.models.model_secret_rotation import EnumConsumerCallOutcome
from omnimarket.nodes.node_secret_reading_effect.adapters import (
    InMemorySecretProbeStore,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.overlay import (
    ModelRotationConsumer,
)

#: Runs an argv within a timeout and returns its exit code; raises on a timeout or a missing binary.
CommandRunner = Callable[[Sequence[str], int], int]


class InMemoryConsumerCalls:
    """Consumers over an in-memory store. ``hold`` leaves a consumer on a given value; ``down``
    makes every consumer unreachable."""

    def __init__(self, store: InMemorySecretProbeStore) -> None:
        self._store = store
        self._held: dict[str, str] = {}
        self.down = False

    def hold(self, consumer: str, value: str) -> None:
        self._held[consumer] = value

    def complete_call(self, consumer: ModelRotationConsumer) -> EnumConsumerCallOutcome:
        if self.down:
            return EnumConsumerCallOutcome.UNREACHABLE
        current = self._store.resolve(consumer.handle()).value
        if current is None:
            return EnumConsumerCallOutcome.FAILED
        held = self._held.get(consumer.consumer)
        if held is not None and held != current.get_secret_value():
            return EnumConsumerCallOutcome.REJECTED
        return EnumConsumerCallOutcome.COMPLETED


def run_command(argv: Sequence[str], timeout_seconds: int) -> int:
    """Run the probe with its output discarded; the exit code is the whole answer."""
    completed = subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout_seconds,
        check=False,
    )
    return completed.returncode


class CommandConsumerCalls:
    """The consumer's own probe command: 0 completed, 1 rejected, anything else failed."""

    def __init__(
        self, runner: CommandRunner = run_command, *, timeout_seconds: int
    ) -> None:
        self._runner = runner
        self._timeout = timeout_seconds

    def complete_call(self, consumer: ModelRotationConsumer) -> EnumConsumerCallOutcome:
        if not consumer.probe_command:
            return EnumConsumerCallOutcome.FAILED
        try:
            code = self._runner(consumer.probe_command, self._timeout)
        except (subprocess.TimeoutExpired, OSError):
            return EnumConsumerCallOutcome.UNREACHABLE
        except ValueError:
            # An argument the OS cannot take (an embedded NUL): the call cannot be made.
            return EnumConsumerCallOutcome.FAILED
        if code == 0:
            return EnumConsumerCallOutcome.COMPLETED
        if code == 1:
            return EnumConsumerCallOutcome.REJECTED
        return EnumConsumerCallOutcome.FAILED


__all__ = [
    "CommandConsumerCalls",
    "CommandRunner",
    "InMemoryConsumerCalls",
    "run_command",
]
