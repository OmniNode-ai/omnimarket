# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures: the refusal handler on an in-memory broker log, one break by name."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.nodes.node_broker_auth_refusal_reading_effect.adapters.broker_log import (
    InMemoryBrokerLog,
)
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.handlers.handler_broker_auth_refusal import (
    HandlerBrokerAuthRefusal,
)
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.overlay import (
    ModelBrokerAuthRefusalOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_broker_auth_refusal_reading_effect"
)
EXAMPLE = NODE / "overlay.example.yaml"
RUN_AT = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
HOST = "example-host"
NEEDLE = "example_authentication_exception"

#: The burst the dev broker logged on 2026-11-09: three refusals in one minute, 14:57:32 to 14:57:35.
BURST = (
    f"2026-11-09T14:57:32.919171949Z WARN connection_context.cc:926 - Error processing request: {NEEDLE} (Unexpected auth request 3 expected handshake)",
    f"2026-11-09T14:57:33.947031503Z WARN connection_context.cc:926 - Error processing request: {NEEDLE} (Unexpected auth request 3 expected handshake)",
    f"2026-11-09T14:57:35.968984634Z WARN connection_context.cc:926 - Error processing request: {NEEDLE} (Unexpected auth request 3 expected handshake)",
)
#: Lines the broker logs that are not refusals.
UNRELATED = (
    "2026-11-09T14:57:34.000000000Z INFO storage - segment rolled",
    "2026-11-09T14:57:36.000000000Z WARN kafka - fetch took 120 ms",
)


def overlay() -> ModelBrokerAuthRefusalOverlay:
    return ModelBrokerAuthRefusalOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )


class World:
    """A broker whose log is clean until a test breaks it by name."""

    def __init__(self) -> None:
        self.lines: tuple[str, ...] | None = UNRELATED
        #: The lines the broker really logged, whatever the reader sees.
        self.broker_logged: tuple[str, ...] = UNRELATED

    def break_client_refused_in_a_burst(self) -> None:
        """The dev broker's case: a client refused three times in one minute."""
        self.broker_logged = (*BURST, *UNRELATED)
        self.lines = self.broker_logged

    def break_reader_is_blind(self) -> None:
        """Injected break: the broker logged the refusals and the reader is given none of them."""
        self.break_client_refused_in_a_burst()
        self.lines = UNRELATED

    def break_log_unreadable(self) -> None:
        self.lines = None

    def handler(self, ledger: InMemoryLedger | None) -> HandlerBrokerAuthRefusal:
        return HandlerBrokerAuthRefusal(
            overlay=overlay(),
            host=HOST,
            source=InMemoryBrokerLog(self.lines),
            appender=ledger,
            clock=lambda: RUN_AT,
        )


__all__ = ["BURST", "HOST", "NEEDLE", "RUN_AT", "World", "overlay"]
