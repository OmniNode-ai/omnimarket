# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The named injected breaks of the J4 join (OMN-22912): consumers that leak on purpose.

Each one is the failing control of a join test: the same run with the consumer swapped must read
NOT WORKING, which shows the scan can see. They are used by the tests and by a drill run under a
drill overlay, never by an operational run.
"""

from __future__ import annotations

from collections.abc import Callable

from omnimarket.models.model_secret_reading import ModelSecretHandle

#: The names a drill run selects, and the break each one injects.
DRILL_CONSUMERS: dict[str, str] = {
    "log-value": "the consumer writes the resolved value into a log line",
    "row-value": "the consumer puts the resolved value into its ledger row cells",
}


class LeakyLogConsumer:
    """Injected break ``log-value``: the key's value goes into a log line."""

    def run(
        self, *, handle: ModelSecretHandle, value: str, log: Callable[[str], None]
    ) -> str:
        log(f"resolved {handle.key} for {handle.consumer}: {value}")
        return f"consumer={handle.consumer} | key={handle.key}"


class LeakyRowConsumer:
    """Injected break ``row-value``: the key's value goes into the ledger row cells."""

    def run(
        self, *, handle: ModelSecretHandle, value: str, log: Callable[[str], None]
    ) -> str:
        log(f"resolved {handle.key} for {handle.consumer}")
        return f"consumer={handle.consumer} | key={handle.key} | value={value}"


__all__ = ["DRILL_CONSUMERS", "LeakyLogConsumer", "LeakyRowConsumer"]
