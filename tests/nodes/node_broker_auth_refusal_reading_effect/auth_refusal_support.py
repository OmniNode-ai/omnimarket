# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures: the refusal handler on an in-memory broker log, one break by name."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.nodes.node_broker_auth_refusal_reading_effect.adapters.broker_log import (
    InMemoryBrokerLog,
    InMemoryControlRuns,
)
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.handlers.handler_broker_auth_refusal import (
    HandlerBrokerAuthRefusal,
)
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.overlay import (
    ModelBrokerAuthRefusalOverlay,
)
from omnimarket.nodes.node_broker_auth_refusal_reading_effect.protocols import (
    ControlRun,
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
#: The refusal the deploy's own negative control drew on the dev broker on 2026-11-09: one line at 15:32:43.997Z,
#: inside the control container's run (started 15:32:43.298Z, finished 15:32:44.065Z).
CONTROL_LINE = f"2026-11-09T15:32:43.997373838Z WARN connection_context.cc:926 - Error processing request: {NEEDLE} (Unexpected auth request 3 expected handshake)"
CONTROL_RUN = ControlRun(
    container="example-project-control",
    started_at=datetime(2026, 11, 9, 15, 32, 43, 298302, tzinfo=UTC),
    finished_at=datetime(2026, 11, 9, 15, 32, 44, 65886, tzinfo=UTC),
)
#: A client that sent credentials the broker refused, 2 s after the control's span closed.
AUTH_FAILED_LINE = f"2026-11-09T15:32:47.500000000Z WARN connection_context.cc:926 - Error processing request: {NEEDLE} (Authentication failed. Shutting down connection)"
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
        #: The control runs the host holds; none is a host no deploy has run the control on.
        self.controls: tuple[ControlRun, ...] | None = ()
        #: The control runs the host really ran, whatever the reader is shown.
        self.control_ran: tuple[ControlRun, ...] = ()

    def deploy_runs_the_control(self) -> None:
        """A deploy ran the negative control: the broker refused it once, as it must."""
        self.control_ran = (CONTROL_RUN,)
        self.controls = self.control_ran
        self.broker_logged = (CONTROL_LINE, *UNRELATED)
        self.lines = self.broker_logged

    def break_control_marker_is_lost(self) -> None:
        """Injected break: the control ran and was refused, and the reader is given no record of its run."""
        self.deploy_runs_the_control()
        self.controls = ()

    def break_control_marker_is_skewed(self) -> None:
        """Injected break: the control's span is read an hour late, so the refusal falls outside it."""
        self.deploy_runs_the_control()
        later = CONTROL_RUN
        self.controls = (
            ControlRun(
                later.container,
                later.started_at.replace(hour=later.started_at.hour + 1),
                later.finished_at.replace(hour=later.finished_at.hour + 1),
            ),
        )

    def break_another_client_during_the_control(self) -> None:
        """The case the control must not hide: a client without credentials refused inside the control's span."""
        self.deploy_runs_the_control()
        self.broker_logged = (
            CONTROL_LINE,
            CONTROL_LINE.replace("15:32:43.997373838Z", "15:32:43.800000000Z"),
            *UNRELATED,
        )
        self.lines = self.broker_logged

    def break_control_unreadable(self) -> None:
        self.controls = None

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
            control_runs=InMemoryControlRuns(self.controls),
            appender=ledger,
            clock=lambda: RUN_AT,
        )


__all__ = [
    "AUTH_FAILED_LINE",
    "BURST",
    "CONTROL_LINE",
    "CONTROL_RUN",
    "HOST",
    "NEEDLE",
    "RUN_AT",
    "World",
    "overlay",
]
