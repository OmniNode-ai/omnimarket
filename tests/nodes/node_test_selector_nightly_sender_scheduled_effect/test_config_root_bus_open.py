# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev runtime mounts the lane declaration but exports no workspace config root: the sender's bus open reads the overlay's."""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.models.scm import ModelScmBusReceipt, ModelScmTerminal, ModelScmTopics
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.handlers import (
    adapter_bus_command_sender as adapter,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.models.model_nightly_sender import (
    EnumCollectStatus,
    ModelCollectCommand,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.models.model_nightly_sender_schedule_overlay import (
    ModelNightlySenderScheduleOverlay,
)
from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.protocols.protocol_nightly_sender import (
    NightlyCommandError,
)
from tests.nodes.node_test_selector_nightly_sender_scheduled_effect.support import (
    OVERLAY,
)

pytestmark = pytest.mark.unit

CONFIG_ROOT_ENV = "ONEX_WORKSPACE_CONFIG_ROOT"
MOUNTED_ROOT = "/etc/onex/workspace-config"
COMMAND = ModelCollectCommand(
    slot_date=date(2026, 11, 22), now=datetime(2026, 11, 22, 4, 0, tzinfo=UTC)
)


class FakeLaneBus:
    """Opens only when the lane declaration's root is named, as the real resolver does, and records what it saw."""

    def __init__(self) -> None:
        self.seen_roots: list[str | None] = []

    @contextlib.asynccontextmanager
    async def open(
        self,
        *,
        bus: str,
        lane: str | None,
        kafka_bootstrap: str | None,
        workspace_root: Path | None,
    ) -> AsyncIterator[object]:
        root = os.environ.get(CONFIG_ROOT_ENV)
        self.seen_roots.append(root)
        if not root:
            raise LabRunBusError(
                f"bus lane {lane!r}: the lane declaration config/lab/ci_bus_lanes.yaml does not exist"
            )
        yield object()


def install_fakes(monkeypatch: pytest.MonkeyPatch, bus: FakeLaneBus) -> None:
    monkeypatch.setattr(
        "omnimarket.delegated_test_loop.lane_bus.open_lab_run_bus", bus.open
    )

    class FakeCaller:
        def __init__(self, _bus: object) -> None: ...

        async def call_topics(
            self, *, topics: ModelScmTopics, **_: Any
        ) -> ModelScmTerminal:
            return ModelScmTerminal(
                receipt=ModelScmBusReceipt(
                    correlation_id=uuid4(),
                    command_topic=topics.command,
                    command_envelope_id=uuid4(),
                    terminal_topic=topics.success,
                ),
                failed=False,
                payload={"status": "collected", "slot_date": "2026-11-22"},
            )

    monkeypatch.setattr(
        "omnimarket.handlers.scm.handler_scm_dispatch.ScmBusCaller", FakeCaller
    )


def sender(root: str | None) -> adapter.BusNightlyCommandSender:
    return adapter.BusNightlyCommandSender(
        bus_lane="dev", timeout_s=5, workspace_config_root=root
    )


def test_config_root_the_overlay_root_is_named_while_the_bus_opens_and_the_environment_is_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CONFIG_ROOT_ENV, raising=False)
    bus = FakeLaneBus()
    install_fakes(monkeypatch, bus)
    answer = sender(MOUNTED_ROOT).collect_nightly(COMMAND)
    assert answer.status is EnumCollectStatus.COLLECTED
    assert bus.seen_roots == [MOUNTED_ROOT]
    assert CONFIG_ROOT_ENV not in os.environ


def test_config_root_a_root_the_environment_already_names_wins_and_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CONFIG_ROOT_ENV, "/srv/operator-root")
    bus = FakeLaneBus()
    install_fakes(monkeypatch, bus)
    sender(MOUNTED_ROOT).collect_nightly(COMMAND)
    assert bus.seen_roots == ["/srv/operator-root"]
    assert os.environ[CONFIG_ROOT_ENV] == "/srv/operator-root"


def test_error_no_overlay_root_and_no_environment_root_is_a_command_error_naming_the_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CONFIG_ROOT_ENV, raising=False)
    bus = FakeLaneBus()
    install_fakes(monkeypatch, bus)
    with pytest.raises(NightlyCommandError, match="bus lane 'dev'"):
        sender(None).collect_nightly(COMMAND)
    assert bus.seen_roots == [None]
    assert CONFIG_ROOT_ENV not in os.environ


def test_config_root_the_variable_is_removed_when_the_bus_open_fails_with_a_root_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CONFIG_ROOT_ENV, raising=False)

    @contextlib.asynccontextmanager
    async def refusing(
        *,
        bus: str,
        lane: str | None,
        kafka_bootstrap: str | None,
        workspace_root: Path | None,
    ) -> AsyncIterator[object]:
        assert os.environ[CONFIG_ROOT_ENV] == MOUNTED_ROOT
        raise LabRunBusError("broker refused")
        yield object()

    monkeypatch.setattr(
        "omnimarket.delegated_test_loop.lane_bus.open_lab_run_bus", refusing
    )
    with pytest.raises(NightlyCommandError, match="broker refused"):
        sender(MOUNTED_ROOT).collect_nightly(COMMAND)
    assert CONFIG_ROOT_ENV not in os.environ


def test_overlay_the_config_root_is_optional_absolute_and_in_the_example_overlay(
    tmp_path: Path,
) -> None:
    without = ModelNightlySenderScheduleOverlay.model_validate(dict(OVERLAY))
    assert without.workspace_config_root is None
    declared = ModelNightlySenderScheduleOverlay.model_validate(
        {**OVERLAY, "workspace_config_root": MOUNTED_ROOT}
    )
    assert declared.workspace_config_root == MOUNTED_ROOT
    with pytest.raises(ValueError, match="workspace_config_root"):
        ModelNightlySenderScheduleOverlay.model_validate(
            {**OVERLAY, "workspace_config_root": "relative/root"}
        )
    node_dir = Path(adapter.__file__).resolve().parents[1]
    example = yaml.safe_load(
        (node_dir / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert example["workspace_config_root"] == "/etc/example/workspace-config"


def test_config_root_the_handler_hands_the_overlay_root_to_the_bus_sender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_test_selector_nightly_sender_scheduled_effect.handlers import (
        handler_nightly_sender_scheduled as handler_module,
    )
    from tests.nodes.node_test_selector_nightly_sender_scheduled_effect.support import (
        AFTER_NIGHTLY,
        FakeRuns,
        FakeSender,
        FakeSettings,
        environ,
        run_view,
        tick,
    )

    built: list[dict[str, object]] = []

    class RecordingSender(FakeSender):
        def __init__(self, **kwargs: object) -> None:
            super().__init__()
            built.append(kwargs)

    monkeypatch.setattr(handler_module, "BusNightlyCommandSender", RecordingSender)
    env = environ(tmp_path)
    overlay_file = tmp_path / "overlays" / handler_module.NODE_NAME / "overlay.yaml"
    document = yaml.safe_load(overlay_file.read_text(encoding="utf-8"))
    overlay_file.write_text(
        yaml.safe_dump({**document, "workspace_config_root": MOUNTED_ROOT}),
        encoding="utf-8",
    )
    event = handler_module.HandlerNightlySenderScheduled(
        environ=env, runs=FakeRuns((run_view(),)), settings=FakeSettings()
    ).handle(tick(AFTER_NIGHTLY))
    assert event is not None
    assert event.outcome == "completed"
    assert built == [
        {
            "bus_lane": "fixture-lane",
            "timeout_s": 60,
            "workspace_config_root": MOUNTED_ROOT,
        }
    ]
