# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for the delegation completion-rate effect tests: the overlay fixture, an in-memory handler and a fake ssh runner."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_delegation_completion_rate import (
    ModelDelegationEventFact,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.adapters.events_source import (
    InMemoryEventSource,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.handlers.handler_delegation_completion_rate_run import (
    HandlerDelegationCompletionRateRun,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.models.model_delegation_rate_run import (
    ModelDelegationRateCommand,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.overlay import (
    ModelDelegationRateEffectOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_delegation_completion_rate_compute.completion_rate_support import (
    NOW,
    above_bound_hour,
)
from tests.nodes.node_delegation_completion_rate_compute.completion_rate_support import (
    overlay as rate_overlay,
)

OVERLAY_FILE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "overlays"
    / "node_delegation_completion_rate_effect"
    / "overlay.yaml"
)


def effect_overlay() -> ModelDelegationRateEffectOverlay:
    return ModelDelegationRateEffectOverlay.model_validate(
        yaml.safe_load(OVERLAY_FILE.read_text(encoding="utf-8"))
    )


def handler(
    *,
    events: Sequence[ModelDelegationEventFact] | None = None,
    error: str | None = None,
    ledger: InMemoryLedger | None = None,
    write: bool = True,
    now: datetime = NOW,
) -> tuple[HandlerDelegationCompletionRateRun, InMemoryLedger]:
    target = ledger if ledger is not None else InMemoryLedger()
    run = HandlerDelegationCompletionRateRun(
        rate_overlay=rate_overlay(),
        source=InMemoryEventSource(
            now=None if error else now,
            events=above_bound_hour() if events is None else events,
            error=error,
        ),
        appender=target if write else None,
        clock=lambda: now,
    )
    return run, target


def command(
    write: bool = True, correlation_id: str = "rate-run-1"
) -> ModelDelegationRateCommand:
    return ModelDelegationRateCommand(correlation_id=correlation_id, write=write)


def answer(
    rows: Sequence[dict[str, Any]], now: str = "2026-11-08T07:00:00.000000+00:00"
) -> str:
    lines = [json.dumps({"now": now}), *(json.dumps(r) for r in rows)]
    return "\n".join(lines) + "\n"


def db_row(
    t: str = "2026-11-08T06:30:00.000000+00:00",
    *,
    src: str = "real",
    shadow: bool = False,
    ok: bool | None = True,
    gate: bool | None = True,
    backend: str | None = "local-heavy-reasoning",
) -> dict[str, Any]:
    return {
        "t": t,
        "src": src,
        "shadow": shadow,
        "ok": ok,
        "gate": gate,
        "backend": backend,
    }


class FakeRunner:
    """A runner that records the command and the script it was given and returns a canned answer."""

    def __init__(
        self, stdout: str = "", returncode: int = 0, raises: Exception | None = None
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.raises = raises
        self.calls: list[tuple[list[str], str, int]] = []

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(argv), (stdin or b"").decode(), timeout))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(
            list(argv), self.returncode, self.stdout.encode(), b""
        )
