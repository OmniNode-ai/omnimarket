# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A declared reading exit is a verdict reading, not a failure (OMN-24609)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from omnimarket.handlers.lab_sli_pack_gate import LAB_SLI_PACK_OVERLAY
from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.nodes.node_lab_sli_collect_effect.handlers.handler_lab_sli_collect import (
    HandlerLabSliCollect,
    _launchd_failures,
    _launchd_readings,
    load_collect_config,
)
from omnimarket.nodes.node_lab_sli_collect_effect.models import (
    ModelLabSliCollectRequest,
)
from omnimarket.nodes.node_lab_sli_collect_effect.protocols import ProbeError

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 15, 3, 0, tzinfo=UTC)

TICK_LISTING = (
    "PID\tStatus\tLabel\n"
    "-\t1\tai.omninode.throughput-tick\n"
    "-\t1\tai.omninode.queue-status-tick\n"
)


@pytest.fixture(autouse=True)
def _enable_lab_sli_pack(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the overlay roots at a pack overlay that enables the lab SLI pack."""
    pack_dir = tmp_path / LAB_SLI_PACK_OVERLAY
    pack_dir.mkdir()
    (pack_dir / "overlay.yaml").write_text("status: enabled\n", encoding="utf-8")
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path))


@dataclass
class FakeProbe:
    os_name: str = "Darwin"
    commands: dict[tuple[str, ...], str] = field(default_factory=dict)

    def system(self) -> str:
        return self.os_name

    def run(self, argv: tuple[str, ...]) -> str:
        if argv not in self.commands:
            raise ProbeError(f"{argv[0]}: not found")
        return self.commands[argv]


def test_declared_reading_exit_is_not_a_failure() -> None:
    config = load_collect_config()
    listing = (
        "PID\tStatus\tLabel\n"
        "-\t1\tai.omninode.throughput-tick\n"
        "-\t1\tai.omninode.queue-status-tick\n"
        "-\t78\tai.omninode.host-reconcile\n"
    )
    failures = _launchd_failures(
        listing, config.launchd_label_domains, config.launchd_reading_exits
    )
    readings = _launchd_readings(
        listing, config.launchd_label_domains, config.launchd_reading_exits
    )
    assert failures == ["ai.omninode.host-reconcile=78"]
    assert readings == [
        "ai.omninode.queue-status-tick=1",
        "ai.omninode.throughput-tick=1",
    ]


def test_undeclared_label_with_the_same_exit_is_still_a_failure() -> None:
    config = load_collect_config()
    listing = "PID\tStatus\tLabel\n-\t1\tai.omninode.host-drift\n"
    failures = _launchd_failures(
        listing, config.launchd_label_domains, config.launchd_reading_exits
    )
    assert failures == ["ai.omninode.host-drift=1"]


def test_declared_label_with_another_exit_is_still_a_failure() -> None:
    config = load_collect_config()
    listing = (
        "PID\tStatus\tLabel\n"
        "-\t3\tai.omninode.queue-status-tick\n"
        "-\t127\tai.omninode.throughput-tick\n"
    )
    failures = _launchd_failures(
        listing, config.launchd_label_domains, config.launchd_reading_exits
    )
    assert failures == [
        "ai.omninode.queue-status-tick=3",
        "ai.omninode.throughput-tick=127",
    ]


def test_collect_reading_detail_and_value() -> None:
    probe = FakeProbe(commands={("launchctl", "list"): TICK_LISTING})
    batch = HandlerLabSliCollect(probe=probe).handle(
        ModelLabSliCollectRequest(interval_start=NOW, now=NOW, collector_host="h101")
    )
    jobs = {r.sli_id: r for r in batch.readings}["scheduled_job_failures"]
    assert jobs.subjects[0].value == 0.0
    assert jobs.detail.startswith("every unit exited 0")
    assert (
        "declared reading exits, not counted: "
        "ai.omninode.queue-status-tick=1, ai.omninode.throughput-tick=1"
    ) in jobs.detail


def test_contract_declares_both_ticks() -> None:
    assert load_collect_config().launchd_reading_exits == {
        "ai.omninode.throughput-tick": (1,),
        "ai.omninode.queue-status-tick": (1,),
    }


def test_without_declared_reading_exits_the_two_ticks_are_failures() -> None:
    config = load_collect_config()
    assert _launchd_failures(TICK_LISTING, config.launchd_label_domains) == [
        "ai.omninode.queue-status-tick=1",
        "ai.omninode.throughput-tick=1",
    ]
