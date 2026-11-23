# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fakes and builders shared by the nightly sender tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.handlers.handler_nightly_sender_scheduled import (
    HandlerNightlySenderScheduled,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.models.model_nightly_sender import (
    EnumCollectStatus,
    EnumPublishStatus,
    ModelCollectAnswer,
    ModelCollectCommand,
    ModelNightlyRunView,
    ModelPublishAnswer,
    ModelPublishCommand,
    ModelRecordSettings,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.protocols.protocol_nightly_sender import (
    NightlyCommandError,
    NightlyRunReadError,
)

NODE = "node_test_selector_nightly_sender_scheduled_effect"
REPO = Path(__file__).resolve().parents[3]
SLOT = date(2026, 11, 22)
SLOT_START = datetime(2026, 11, 22, 2, 30, tzinfo=UTC)
AFTER_NIGHTLY = datetime(2026, 11, 22, 4, 0, tzinfo=UTC)
STATE_ENV = "TEST_SENDER_STATE_DIR"

OVERLAY: dict[str, object] = {
    "job_id": "test_selector_nightly_sender",
    "run_interval_seconds": 900,
    "grace_seconds": 300,
    "repository": "example-org/example-repo",
    "workflow_file": "nightly-full-suite.yml",
    "nightly_slot_time_utc": "02:30",
    "earliest_check_time_utc": "03:15",
    "settle_margin_seconds": 300,
    "collector_max_lag_hours": 6,
    "state_dir_env": STATE_ENV,
    "bus_lane": "fixture-lane",
    "command_timeout_seconds": 60,
}


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


def write_overlay(root: Path, document: dict[str, object] | None = None) -> None:
    (root / NODE).mkdir(parents=True, exist_ok=True)
    (root / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(OVERLAY if document is None else document), encoding="utf-8"
    )


def environ(tmp_path: Path, *, state: bool = True) -> dict[str, str]:
    overlays = tmp_path / "overlays"
    write_overlay(overlays)
    env = {"ONEX_SKILL_OVERLAY_ROOTS": str(overlays)}
    if state:
        env[STATE_ENV] = str(tmp_path / "state")
    return env


def run_view(
    *,
    status: str | None = "completed",
    created: datetime = SLOT_START + timedelta(minutes=1),
    updated: datetime = SLOT_START + timedelta(minutes=40),
    run_id: int = 4242,
) -> ModelNightlyRunView:
    return ModelNightlyRunView(
        run_id=run_id, status=status, created_at=created, updated_at=updated
    )


@dataclass
class FakeRuns:
    runs: tuple[ModelNightlyRunView, ...] = ()
    fail: bool = False
    reads: int = 0

    def list_runs(
        self, *, repository: str, workflow_file: str
    ) -> tuple[ModelNightlyRunView, ...]:
        self.reads += 1
        assert (repository, workflow_file) == (
            "example-org/example-repo",
            "nightly-full-suite.yml",
        )
        if self.fail:
            raise NightlyRunReadError("gateway refused")
        return self.runs


@dataclass
class FakeSender:
    """Records every command in order; answers from the status it is given."""

    collect_status: EnumCollectStatus = EnumCollectStatus.COLLECTED
    publish_status: EnumPublishStatus = EnumPublishStatus.PUBLISHED
    collect_error: bool = False
    publish_error: bool = False
    sent: list[tuple[str, date]] = field(default_factory=list)
    publish_commands: list[ModelPublishCommand] = field(default_factory=list)

    def collect_nightly(self, command: ModelCollectCommand) -> ModelCollectAnswer:
        self.sent.append(("collect_nightly", command.slot_date))
        if self.collect_error:
            raise NightlyCommandError("no terminal event")
        return ModelCollectAnswer(
            status=self.collect_status,
            slot_date=command.slot_date,
            raw={
                "status": self.collect_status.value,
                "slot_date": str(command.slot_date),
            },
        )

    def publish_attribution_record(
        self, command: ModelPublishCommand
    ) -> ModelPublishAnswer:
        self.sent.append(("publish_attribution_record", command.slot_date))
        self.publish_commands.append(command)
        if self.publish_error:
            raise NightlyCommandError("no terminal event")
        return ModelPublishAnswer(
            status=self.publish_status, slot_date=command.slot_date
        )


class FakeSettings:
    def read(self) -> ModelRecordSettings:
        return ModelRecordSettings(
            selector_version="1.0.0",
            policy_version="abc123def456",
            stage_entry_counted=1,
        )


def handler(
    tmp_path: Path,
    runs: FakeRuns,
    sender: FakeSender,
    *,
    state: bool = True,
) -> HandlerNightlySenderScheduled:
    return HandlerNightlySenderScheduled(
        environ=environ(tmp_path, state=state),
        runs=runs,
        sender=sender,
        settings=FakeSettings(),
    )
