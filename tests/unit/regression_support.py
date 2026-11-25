# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders shared by the delegation check regression tests: settings, runs and a handler over in-memory ports."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_delegation_check_regression_effect.adapters.inmemory_adapters import (
    InMemoryChangeSource,
    InMemoryClaims,
    InMemoryRunSource,
    InMemorySeenStore,
    RecordingPublisher,
)
from omnimarket.nodes.node_delegation_check_regression_effect.handlers.handler_delegation_check_regression import (
    HandlerDelegationCheckRegression,
    load_topics,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    EnumRunVerdict,
    ModelChange,
    ModelProbeRun,
    ModelRegressionSettings,
)
from omnimarket.nodes.node_delegation_check_regression_effect.protocols import (
    ProtocolChangeSource,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "delegation_check_regression"
    / "recorded_runs_2026_11_23.json"
)
T0 = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
TOPICS = load_topics()


def settings(**over: object) -> ModelRegressionSettings:
    base: dict[str, object] = {
        "alarm_lane": "example-orchestrator-lane",
        "owner": "example-owner",
        "ledger_read": {
            "route": "file",
            "path_env": "EXAMPLE_LEDGER_FILE",
            "timeout_seconds": 60,
            "row_limit": 100,
        },
        "checks": [
            {
                "check_id": "C14",
                "repository": "example-org/example-customer",
                "workflow": "c14.yml",
                "interval_seconds": 3600,
            },
            {
                "check_id": "C13",
                "repository": "example-org/example-customer",
                "workflow": "c13.yml",
                "interval_seconds": 3600,
            },
        ],
    }
    base.update(over)
    return ModelRegressionSettings.model_validate(base)


def run(n: int, verdict: str, *, at: datetime | None = None) -> ModelProbeRun:
    return ModelProbeRun(
        run_id=f"run-{n}",
        at=at or T0 + timedelta(hours=n),
        verdict=EnumRunVerdict(verdict),
    )


def runs(verdicts: str, *, start: datetime = T0) -> list[ModelProbeRun]:
    """One run an hour; 'g' is a pass and 'r' a fail."""
    return [
        ModelProbeRun(
            run_id=f"run-{i}",
            at=start + timedelta(hours=i),
            verdict=EnumRunVerdict.PASS if ch == "g" else EnumRunVerdict.FAIL,
        )
        for i, ch in enumerate(verdicts)
    ]


class Rig:
    def __init__(
        self,
        by_check: Mapping[str, Sequence[ModelProbeRun]],
        *,
        cfg: ModelRegressionSettings | None = None,
        changes: Sequence[ModelChange] = (),
        change_source: ProtocolChangeSource | None = None,
        held: Mapping[str, str] | None = None,
        until: datetime | None = None,
        unreadable: Mapping[str, str] | None = None,
    ) -> None:
        self.settings = cfg or settings()
        self.publisher = RecordingPublisher()
        self.seen = InMemorySeenStore()
        self.handler = HandlerDelegationCheckRegression(
            settings=self.settings,
            runs=InMemoryRunSource(by_check, until=until, unreadable=unreadable),
            changes=change_source or InMemoryChangeSource(changes),
            claims=InMemoryClaims(held),
            seen=self.seen,
            publisher=self.publisher,
            topics=TOPICS,
        )


def recorded_runs() -> dict[str, list[ModelProbeRun]]:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {
        check: [
            ModelProbeRun(
                run_id=r["run_id"], at=r["at"], verdict=EnumRunVerdict(r["verdict"])
            )
            for r in recorded
        ]
        for check, recorded in document["runs"].items()
    }
