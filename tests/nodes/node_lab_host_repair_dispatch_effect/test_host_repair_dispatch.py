# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host repair dispatch (OMN-23389): one repair lane per host and alarm, deduplicated durably, retried to a budget."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lab_host_repair_dispatch_effect.handlers.adapters import (
    ArgvDispatcher,
    InMemoryDispatcher,
    InMemoryEscalation,
    InMemoryMeasurement,
    InMemoryStateStore,
    JsonFileStateStore,
    SshMeasurement,
)
from omnimarket.nodes.node_lab_host_repair_dispatch_effect.handlers.handler_host_repair_dispatch import (
    HandlerHostRepairDispatch,
    RepairRefusalError,
    load_overlay,
)
from omnimarket.nodes.node_lab_host_repair_dispatch_effect.models.model_host_repair import (
    EnumHostRepairAction,
    Level,
    ModelHostDiskAlarm,
    ModelHostMeasurement,
    ModelHostRepairOverlay,
    ModelHostRepairRequest,
    ModelHostRepairResult,
    ModelHostVolumeAlarm,
)
from omnimarket.nodes.node_lab_host_repair_dispatch_effect.node import (
    NodeLabHostRepairDispatchEffect,
)
from omnimarket.nodes.node_lab_host_repair_dispatch_effect.runtime_host_repair_dispatch import (
    main,
)

T0 = datetime(2026, 11, 9, 21, 0, 0, tzinfo=UTC)
DF = "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/nvme0n1p2 546000000 530000000 16000000 97% /"
FINDMNT = "/ /dev/nvme0n1p2 ext4 rw,relatime"
MEASURED = ModelHostMeasurement(df=DF, findmnt=FINDMNT)
OVERLAY = ModelHostRepairOverlay(
    alarm_name="disk-watermark-page",
    retry_budget=3,
    backoff_seconds=(600, 1200),
    alarm_to_repair_latency_seconds=300,
    state_file="$HOME/state/repair.json",
    brief_dir="$HOME/briefs",
    repair_ticket="OMN-1",
    parent_lane="parent-lane",
    lane_prefix="hostrepair",
    measure_separator="---FINDMNT---",
    measure_timeout_seconds=30,
    measure_argv=("measure", "{host}"),
    dispatch_argv=(
        "start",
        "{lane}",
        "{brief_file}",
        "{target}",
        "{ticket}",
        "{parent}",
    ),
    dispatch_timeout_seconds=30,
    escalate_argv=("escalate", "{row}"),
    brief_template="host {host} {used_percent} attempt {attempt}/{budget} {alarm} {read_at}\n{df}\n{findmnt}",
)


def stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def alarm(
    host: str = "pool-a", level: Level = "page", at: datetime = T0
) -> ModelHostDiskAlarm:
    return ModelHostDiskAlarm(
        host_name=host,
        read_at=stamp(at),
        level=level,
        volumes=(ModelHostVolumeAlarm(path="/", used_percent=97.0, page_percent=90),),
    )


class Rig:
    def __init__(
        self, hosts: tuple[str, ...] = ("pool-a",), refuse: bool = False
    ) -> None:
        self.store = InMemoryStateStore()
        self.dispatcher = InMemoryDispatcher(refuse=refuse)
        self.escalation = InMemoryEscalation()
        self.measurement = InMemoryMeasurement(dict.fromkeys(hosts, MEASURED))
        self.handler = HandlerHostRepairDispatch(
            store=self.store,
            measurement=self.measurement,
            dispatcher=self.dispatcher,
            escalation=self.escalation,
            overlay=OVERLAY,
        )

    def alarm(
        self,
        host: str = "pool-a",
        level: Level = "page",
        after: int = 0,
        read_after: int = 0,
    ) -> ModelHostRepairResult:
        return self.handler.handle(
            ModelHostRepairRequest(
                alarm=alarm(host, level, T0 + timedelta(seconds=read_after)),
                now=stamp(T0 + timedelta(seconds=after)),
            )
        )


def test_golden_chain_an_alarm_dispatches_one_lane_with_the_measured_reading() -> None:
    rig = Rig()
    result = rig.alarm(after=40)
    assert result.action is EnumHostRepairAction.DISPATCHED
    assert result.attempt == 1
    assert result.latency_seconds == 40
    assert result.latency_exceeded is False
    [spec] = rig.dispatcher.lanes
    assert spec.lane == result.lane
    assert spec.target == "pool-a"
    assert DF in spec.brief
    assert FINDMNT in spec.brief
    assert "attempt 1/3" in spec.brief
    assert rig.measurement.calls == ["pool-a"]


def test_two_alarms_for_one_host_dispatch_one_lane() -> None:
    """AC2 falsifier: a second alarm for the same host and alarm inside the backoff dispatches nothing."""
    rig = Rig()
    first = rig.alarm(after=0)
    second = rig.alarm(after=300, read_after=300)
    assert first.action is EnumHostRepairAction.DISPATCHED
    assert second.action is EnumHostRepairAction.DEDUPED
    assert len(rig.dispatcher.lanes) == 1
    assert second.lane == first.lane


def test_alarms_for_two_hosts_dispatch_two_lanes() -> None:
    rig = Rig(hosts=("pool-a", "pool-b"))
    rig.alarm("pool-a")
    rig.alarm("pool-b")
    assert sorted(s.target for s in rig.dispatcher.lanes) == ["pool-a", "pool-b"]


def test_dedupe_survives_a_restart_through_the_durable_store(tmp_path: Path) -> None:
    path = tmp_path / "state.json"

    def handler() -> tuple[HandlerHostRepairDispatch, InMemoryDispatcher]:
        dispatcher = InMemoryDispatcher()
        return (
            HandlerHostRepairDispatch(
                store=JsonFileStateStore(path),
                measurement=InMemoryMeasurement({"pool-a": MEASURED}),
                dispatcher=dispatcher,
                escalation=InMemoryEscalation(),
                overlay=OVERLAY,
            ),
            dispatcher,
        )

    request = ModelHostRepairRequest(alarm=alarm(), now=stamp(T0))
    before, first = handler()
    assert before.handle(request).action is EnumHostRepairAction.DISPATCHED
    after, second = handler()
    assert after.handle(request).action is EnumHostRepairAction.DEDUPED
    assert len(first.lanes) == 1
    assert second.lanes == []
    assert json.loads(path.read_text())["pool-a|disk-watermark-page"]["attempts"] == 1


def test_a_failed_repair_retries_to_its_budget_then_escalates_once() -> None:
    rig = Rig()
    actions = [
        rig.alarm(after=0).action,
        rig.alarm(after=601).action,
        rig.alarm(after=601 + 1201).action,
        rig.alarm(after=601 + 1201 + 1201).action,
        rig.alarm(after=601 + 1201 + 1201 + 1201).action,
    ]
    assert actions == [
        EnumHostRepairAction.DISPATCHED,
        EnumHostRepairAction.RETRIED,
        EnumHostRepairAction.RETRIED,
        EnumHostRepairAction.ESCALATED,
        EnumHostRepairAction.ESCALATED_ALREADY,
    ]
    assert len(rig.dispatcher.lanes) == 3
    assert len({s.lane for s in rig.dispatcher.lanes}) == 3
    assert len(rig.escalation.rows) == 1
    assert "spent its budget of 3" in rig.escalation.rows[0]


def test_a_reading_below_the_page_mark_ends_the_episode() -> None:
    rig = Rig()
    rig.alarm(after=0)
    assert rig.alarm(level="ok", after=100).action is EnumHostRepairAction.CLEARED
    again = rig.alarm(after=200, read_after=200)
    assert again.action is EnumHostRepairAction.DISPATCHED
    assert again.attempt == 1
    assert rig.alarm("pool-a", "warn", after=300).action is EnumHostRepairAction.CLEARED


def test_a_level_below_page_with_no_episode_is_ignored() -> None:
    assert Rig().alarm(level="warn").action is EnumHostRepairAction.IGNORED


def test_a_dispatch_past_the_latency_bound_is_reported_late() -> None:
    result = Rig().alarm(after=301)
    assert result.action is EnumHostRepairAction.DISPATCHED
    assert result.latency_exceeded is True


def test_error_chain_a_host_that_cannot_be_measured_gets_no_brief_that_guesses() -> (
    None
):
    rig = Rig(hosts=())
    result = rig.alarm()
    assert result.action is EnumHostRepairAction.MEASURE_FAILED
    assert rig.dispatcher.lanes == []
    state = rig.store.get("pool-a|disk-watermark-page")
    assert state is not None
    assert state.attempts == 1
    assert state.last_error.startswith("measure:")


def test_error_chain_a_refused_dispatch_spends_the_attempt_and_retries_after_the_backoff() -> (
    None
):
    rig = Rig(refuse=True)
    first = rig.alarm(after=0)
    assert first.action is EnumHostRepairAction.DISPATCH_FAILED
    assert rig.alarm(after=100).action is EnumHostRepairAction.DEDUPED
    rig.dispatcher.refuse = False
    retry = rig.alarm(after=601)
    assert retry.action is EnumHostRepairAction.RETRIED
    assert retry.attempt == 2
    assert len(rig.dispatcher.lanes) == 1


def test_a_replayed_attempt_has_the_same_lane_name() -> None:
    first = Rig().alarm(after=0)
    replay = Rig().alarm(after=0)
    assert first.lane == replay.lane == "hostrepair-pool-a-202611092100-a1"


class _Recorder:
    def __init__(self, stdout: str = "", code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.stdout, self.code = stdout, code

    def __call__(
        self, argv: Sequence[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), self.code, self.stdout, "")


def test_the_ssh_measurement_splits_df_from_findmnt_and_fills_the_host() -> None:
    runner = _Recorder(f"{DF}\n---FINDMNT---\n{FINDMNT}\n")
    reading = SshMeasurement(
        ("ssh", "{host}", "df"), "---FINDMNT---", 5, runner
    ).measure("pool-a")
    assert reading.df == DF
    assert reading.findmnt == FINDMNT
    assert runner.calls == [["ssh", "pool-a", "df"]]


def test_the_argv_dispatcher_writes_the_brief_and_fills_every_placeholder(
    tmp_path: Path,
) -> None:
    runner = _Recorder()
    rig = Rig()
    rig.handler = HandlerHostRepairDispatch(
        store=rig.store,
        measurement=rig.measurement,
        dispatcher=ArgvDispatcher(OVERLAY.dispatch_argv, 5, runner),
        escalation=rig.escalation,
        overlay=OVERLAY,
        home=tmp_path,
    )
    result = rig.alarm()
    assert result.action is EnumHostRepairAction.DISPATCHED
    assert Path(result.brief_file).read_text().count(DF) == 1
    assert runner.calls == [
        ["start", result.lane, result.brief_file, "pool-a", "OMN-1", "parent-lane"]
    ]


def test_node_entry_and_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    node = NodeLabHostRepairDispatchEffect()
    node.handler = HandlerHostRepairDispatch(
        store=InMemoryStateStore(),
        measurement=InMemoryMeasurement({"pool-a": MEASURED}),
        dispatcher=InMemoryDispatcher(),
        escalation=InMemoryEscalation(),
        overlay=OVERLAY,
    )
    assert (
        node.handle(ModelHostRepairRequest(alarm=alarm(), now=stamp(T0))).action
        is EnumHostRepairAction.DISPATCHED
    )
    # A reading file as the watermark node writes it, with the fields the alarm does not need.
    reading = tmp_path / "reading.json"
    reading.write_text(
        json.dumps(
            {
                "host_name": "pool-a",
                "read_at": stamp(T0),
                "level": "ok",
                "paged": False,
                "volumes": [
                    {
                        "path": "/",
                        "total_bytes": 1,
                        "free_bytes": 1,
                        "used_percent": 10.0,
                        "warn_percent": 80,
                        "page_percent": 90,
                        "level": "ok",
                    }
                ],
                "reading_file": str(reading),
                "written": True,
            }
        )
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump(OVERLAY.model_dump(mode="json")))
    assert main(["run", "--reading-file", str(reading), "--overlay", str(overlay)]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "ignored"


def test_error_chain_an_unreadable_reading_or_overlay_is_indeterminate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "--reading-file", str(tmp_path / "missing.json")]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err
    with pytest.raises(RepairRefusalError):
        load_overlay(str(tmp_path / "missing.yaml"))


def test_the_shipped_overlays_load() -> None:
    root = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_lab_host_repair_dispatch_effect"
    )
    for name in ("overlay.yaml", "overlay.example.yaml"):
        overlay = load_overlay(str(root / name))
        assert overlay.retry_budget >= 1
        assert "{df}" in overlay.brief_template
        assert "{findmnt}" in overlay.brief_template
