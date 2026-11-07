# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures: the landing canary on an in-memory test repository and a clock the wait advances."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from omnimarket.nodes.node_landing_canary_effect.adapters.canary_repo import (
    InMemoryCanaryRepo,
)
from omnimarket.nodes.node_landing_canary_effect.handlers.handler_landing_canary import (
    HandlerLandingCanary,
)
from omnimarket.nodes.node_landing_canary_effect.overlay import (
    ModelLandingCanaryOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_landing_canary_effect"
)
EXAMPLE = NODE / "overlay.example.yaml"
RUN_AT = datetime(2026, 11, 7, 4, 10, tzinfo=UTC)
HOST = "example-host"


def overlay() -> ModelLandingCanaryOverlay:
    return ModelLandingCanaryOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )


class FakeClock:
    """A clock only the wait moves: a sleep advances it by exactly what was asked."""

    def __init__(self) -> None:
        self.now = RUN_AT
        self.sleeps: list[float] = []
        #: When set, every sleep takes this long whatever was asked (a host that slept through the wait).
        self.oversleep_seconds: float | None = None

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        took = seconds if self.oversleep_seconds is None else self.oversleep_seconds
        self.now += timedelta(seconds=took)


class World:
    """A test repository and a landing tick that merge the canary in five minutes, until a test breaks one by name."""

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.repo = InMemoryCanaryRepo(clock=self.clock, land_after_seconds=300)

    def break_landing_tick_disabled(self) -> None:
        """The planted stall: the landing tick never merges anything."""
        self.repo.landing_tick_enabled = False

    def break_landing_tick_slow(self) -> None:
        """The tick merges, but after the bar."""
        self.repo.land_after_seconds = overlay().target.landing_bound_seconds + 120

    def break_pull_request_closed(self) -> None:
        self.repo.closed_unmerged = True

    def break_open_fails(self) -> None:
        self.repo.open_error = "push-refused"

    def break_state_unreadable(self) -> None:
        self.repo.state_error = "pr-state-exit-1"

    def handler(
        self,
        ledger: InMemoryLedger | None,
        settings: ModelLandingCanaryOverlay | None = None,
    ) -> HandlerLandingCanary:
        return HandlerLandingCanary(
            overlay=settings or overlay(),
            host=HOST,
            repo=self.repo,
            appender=ledger,
            clock=self.clock,
            sleep=self.clock.sleep,
        )


__all__ = ["HOST", "RUN_AT", "FakeClock", "World", "overlay"]
