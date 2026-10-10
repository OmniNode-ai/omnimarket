# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host observer: launchd, cron completion evidence, receipts and the journal."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessEvent,
    ModelAutomationLivenessOverlay,
    automation_liveness_topics,
)
from omnimarket.nodes.node_automation_run_observer_effect.handlers import (
    HandlerAutomationRunObserver,
)
from omnimarket.nodes.node_automation_run_observer_effect.handlers.undeclared_census import (
    undeclared_process_id,
)
from omnimarket.nodes.node_automation_run_observer_effect.models import (
    ModelAutomationRunObserverRequest,
    ModelUndeclaredCensusScope,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.host_command_runner import (
    ModelHostCommandOutcome,
)

pytestmark = pytest.mark.unit

HOST = "host-a"
T0 = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
TOPICS = automation_liveness_topics()
RUN_TOPIC = TOPICS[EnumAutomationLivenessEvent.RUN_OBSERVED]
VERDICT_TOPIC = TOPICS[EnumAutomationLivenessEvent.LIVENESS_VERDICT]
HEARTBEAT_TOPIC = TOPICS[EnumAutomationLivenessEvent.HEARTBEAT]


def entry(process_id: str, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "process_id": process_id,
        "owner_repo": "example-owner/example-repo",
        "host": HOST,
        "trigger": {
            "kind": "launchd-interval",
            "native_id": f"com.example.{process_id}",
        },
        "expected_interval_seconds": 300,
        "alarm_deadline_seconds": 1800,
        "max_runtime_seconds": 240,
        "emitter": "observer",
        "run_record": "per-run",
        "real_work": "placed",
        "demand": "none",
        "positive_control": {"kind": "class_drill", "ref": "stop the job"},
    }
    base.update(overrides)
    return base


def write_overlay(tmp_path: Path, entries: Sequence[dict[str, object]]) -> str:
    overlay = ModelAutomationLivenessOverlay.model_validate(
        {
            "schema_version": "automation-liveness-overlay/v1",
            "processes": list(entries),
        }
    )
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(overlay.model_dump(mode="json")), encoding="utf-8")
    return str(path)


class FakeSink:
    def __init__(self) -> None:
        self.down = False
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(
        self, topic: str, payload: dict[str, object], *, key: str, event_id: str
    ) -> None:
        if self.down:
            raise ConnectionError("broker unreachable")
        self.published.append((topic, payload))

    def of(self, topic: str) -> list[dict[str, object]]:
        return [payload for t, payload in self.published if t == topic]


class FakeRunner:
    """Answers launchctl print and list from tables the test edits."""

    def __init__(self) -> None:
        self.runs: dict[str, tuple[int, int]] = {}
        self.listed: list[str] = []

    def run(self, argv: Sequence[str]) -> ModelHostCommandOutcome:
        if argv[:2] == ["launchctl", "list"]:
            rows = "".join(f"-\t0\t{label}\n" for label in self.listed)
            return ModelHostCommandOutcome(
                returncode=0, stdout="PID\tStatus\tLabel\n" + rows
            )
        label = argv[-1].rsplit("/", 1)[1]
        if label not in self.runs:
            return ModelHostCommandOutcome(
                returncode=113, stdout="", stderr="no such service"
            )
        count, code = self.runs[label]
        return ModelHostCommandOutcome(
            returncode=0,
            stdout=f"{label} = {{\n\truns = {count}\n\tlast exit code = {code}\n}}\n",
        )


def poll(
    handler: HandlerAutomationRunObserver,
    tmp_path: Path,
    overlay_path: str,
    now: datetime,
    census: ModelUndeclaredCensusScope | None = None,
):
    return handler.handle(
        ModelAutomationRunObserverRequest(
            host=HOST,
            overlay_path=overlay_path,
            state_dir=str(tmp_path / "state"),
            now=now,
            launchd_domain="gui/501",
            census=census or ModelUndeclaredCensusScope(),
        )
    )


def receipt(run: int, work: int = 1, exit_code: int = 0) -> str:
    start = T0 + timedelta(minutes=5 * run)
    return (
        json.dumps(
            {
                "started_at": start.isoformat(),
                "finished_at": (start + timedelta(seconds=30)).isoformat(),
                "exit_code": exit_code,
                "placed": work,
            }
        )
        + "\n"
    )


def receipts_overlay(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "fire-receipts.jsonl"
    overlay = write_overlay(
        tmp_path,
        [
            entry(
                "host-a/interval-job",
                evidence={
                    "source": "receipts_file",
                    "locator": str(log),
                    "completion_record": "outcome and exit_code",
                },
            )
        ],
    )
    return overlay, log


def test_observer_journal_every_run_idempotent(tmp_path: Path) -> None:
    overlay, log = receipts_overlay(tmp_path)
    sink = FakeSink()
    handler = HandlerAutomationRunObserver(sink=sink)
    log.write_text(receipt(0), encoding="utf-8")
    poll(handler, tmp_path, overlay, T0 + timedelta(minutes=1))

    # three runs land between two polls
    log.write_text(
        log.read_text() + receipt(1) + receipt(2, work=0) + receipt(3), encoding="utf-8"
    )
    second = poll(handler, tmp_path, overlay, T0 + timedelta(minutes=20))
    assert second.runs_emitted == 3
    runs = sink.of(RUN_TOPIC)
    assert len(runs) == 4
    assert [r["did_work_count"] for r in runs] == [1, 1, 0, 1]
    assert len({r["run_id"] for r in runs}) == 4

    # a plain re-read emits nothing
    assert (
        poll(handler, tmp_path, overlay, T0 + timedelta(minutes=21)).runs_emitted == 0
    )

    # and so does a re-read from the start of the file after the cursor is lost
    state = tmp_path / "state" / "observer-state.json"
    saved = json.loads(state.read_text())
    saved["cursors"]["host-a/interval-job"]["offset"] = 0
    state.write_text(json.dumps(saved))
    assert (
        poll(handler, tmp_path, overlay, T0 + timedelta(minutes=22)).runs_emitted == 0
    )
    assert len(sink.of(RUN_TOPIC)) == 4


def test_observer_journal_does_not_consume_a_half_written_record(
    tmp_path: Path,
) -> None:
    overlay, log = receipts_overlay(tmp_path)
    sink = FakeSink()
    handler = HandlerAutomationRunObserver(sink=sink)
    whole = receipt(0)
    log.write_text(whole[:40], encoding="utf-8")
    assert poll(handler, tmp_path, overlay, T0).runs_emitted == 0
    log.write_text(whole, encoding="utf-8")
    assert poll(handler, tmp_path, overlay, T0 + timedelta(minutes=1)).runs_emitted == 1


def latest_only_overlay(tmp_path: Path) -> str:
    return write_overlay(
        tmp_path,
        [
            entry(
                "host-a/nightly",
                trigger={
                    "kind": "launchd-calendar",
                    "native_id": "com.example.nightly",
                },
                expected_interval_seconds=600,
                alarm_deadline_seconds=3600,
                max_runtime_seconds=120,
                run_record="latest-only",
                evidence={"source": "launchd_state", "locator": "com.example.nightly"},
            )
        ],
    )


def test_observer_latest_only_unseen_runs(tmp_path: Path) -> None:
    overlay = latest_only_overlay(tmp_path)
    runner = FakeRunner()
    sink = FakeSink()
    handler = HandlerAutomationRunObserver(sink=sink, runner=runner)

    runner.runs["com.example.nightly"] = (4, 0)
    first = poll(handler, tmp_path, overlay, T0)
    assert first.unseen_runs == 0
    assert [r["unseen_runs"] for r in sink.of(RUN_TOPIC)] == [0]

    runner.runs["com.example.nightly"] = (7, 1)  # three runs since the last poll
    second = poll(handler, tmp_path, overlay, T0 + timedelta(minutes=10))
    assert second.unseen_runs == 2
    latest = sink.of(RUN_TOPIC)[-1]
    assert latest["unseen_runs"] == 2
    assert latest["outcome"] == "failed"
    assert latest["exit_code"] == 1

    # unchanged counter: no run, nothing unseen
    third = poll(handler, tmp_path, overlay, T0 + timedelta(minutes=20))
    assert (third.runs_emitted, third.unseen_runs) == (0, 0)
    assert len(sink.of(RUN_TOPIC)) == 2


def test_observer_journal_flush_order(tmp_path: Path) -> None:
    overlay, log = receipts_overlay(tmp_path)
    sink = FakeSink()
    sink.down = True
    handler = HandlerAutomationRunObserver(sink=sink)

    log.write_text(receipt(0) + receipt(1), encoding="utf-8")
    first = poll(handler, tmp_path, overlay, T0 + timedelta(minutes=10))
    assert sink.published == []
    assert first.events_published == 0
    assert first.journal_backlog == 3  # two runs and the heartbeat
    assert first.flush_error is not None
    assert "broker unreachable" in first.flush_error
    journal = (tmp_path / "state" / "event-journal.jsonl").read_text().splitlines()
    assert len(journal) == 3

    log.write_text(log.read_text() + receipt(2), encoding="utf-8")
    sink.down = False
    second = poll(handler, tmp_path, overlay, T0 + timedelta(minutes=15))
    assert second.journal_backlog == 0
    assert second.events_published == 5
    order = [
        payload.get("run_id") or f"beat-{payload['progress_counter']}"
        for _, payload in sink.published
    ]
    starts = [(T0 + timedelta(minutes=5 * n)).isoformat() for n in (0, 1, 2)]
    assert order == [
        f"host-a/interval-job:{starts[0]}",
        f"host-a/interval-job:{starts[1]}",
        "beat-1",
        f"host-a/interval-job:{starts[2]}",
        "beat-2",
    ]
    assert (tmp_path / "state" / "event-journal.jsonl").read_text() == ""


def test_observer_reports_undeclared(tmp_path: Path) -> None:
    overlay = latest_only_overlay(tmp_path)
    runner = FakeRunner()
    runner.runs["com.example.nightly"] = (1, 0)
    runner.listed = ["com.example.nightly", "com.example.stray", "com.other.vendor"]
    sink = FakeSink()
    handler = HandlerAutomationRunObserver(sink=sink, runner=runner)
    scope = ModelUndeclaredCensusScope(label_prefixes=("com.example.",))

    # positive control: an empty scope names nothing as ours
    quiet = poll(handler, tmp_path, overlay, T0)
    assert quiet.findings == ()

    result = poll(handler, tmp_path, overlay, T0 + timedelta(minutes=1), scope)
    assert [(f.process_id, f.verdict.value) for f in result.findings] == [
        ("host-a/undeclared/com.example.stray", "undeclared")
    ]
    verdicts = sink.of(VERDICT_TOPIC)
    assert len(verdicts) == 1
    assert verdicts[0]["verdict"] == "undeclared"
    assert verdicts[0]["reason"] == "process_without_entry"
    assert verdicts[0]["contract_digest"] is None

    # reported once while it keeps running, again if it goes and comes back
    assert (
        poll(handler, tmp_path, overlay, T0 + timedelta(minutes=2), scope).findings
        == ()
    )
    runner.listed = ["com.example.nightly"]
    poll(handler, tmp_path, overlay, T0 + timedelta(minutes=3), scope)
    runner.listed.append("com.example.stray")
    again = poll(handler, tmp_path, overlay, T0 + timedelta(minutes=4), scope)
    assert len(again.findings) == 1


def test_observer_reports_undeclared_cron_line_and_plist_path(tmp_path: Path) -> None:
    overlay = latest_only_overlay(tmp_path)
    cron = tmp_path / "crontab"
    cron.write_text(
        "# comment\nMAILTO=nobody\n*/5 * * * * /srv/ours/bin/tick\n0 * * * * /usr/bin/foreign\n",
        encoding="utf-8",
    )
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "x.plist").write_bytes(
        b'<?xml version="1.0"?><plist version="1.0"><dict><key>Label</key>'
        b"<string>vendor.thing</string><key>ProgramArguments</key><array>"
        b"<string>/srv/ours/bin/run</string></array></dict></plist>"
    )
    runner = FakeRunner()
    runner.runs["com.example.nightly"] = (1, 0)
    runner.listed = ["com.example.nightly", "vendor.thing", "vendor.other"]
    handler = HandlerAutomationRunObserver(sink=FakeSink(), runner=runner)
    scope = ModelUndeclaredCensusScope(
        path_roots=("/srv/ours/",),
        launchd_agent_dirs=(str(agents),),
        cron_files=(str(cron),),
    )
    result = poll(handler, tmp_path, overlay, T0, scope)
    assert sorted(f.process_id for f in result.findings) == sorted(
        [
            "host-a/undeclared/vendor.thing",
            undeclared_process_id(HOST, f"{cron}:3"),
        ]
    )


def cron_overlay(tmp_path: Path, log: Path) -> str:
    return write_overlay(
        tmp_path,
        [
            entry(
                "host-a/cron-job",
                trigger={"kind": "cron", "native_id": "example-cron-file:3"},
                expected_interval_seconds=3600,
                alarm_deadline_seconds=7800,
                max_runtime_seconds=600,
                evidence={
                    "source": "log_line",
                    "locator": str(log),
                    "completion_record": "run finished exit=",
                },
            )
        ],
    )


def stamp(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def test_observer_cron_completion_and_consumer_backlog_cron_overrun(
    tmp_path: Path,
) -> None:
    log = tmp_path / "run.log"
    overlay = cron_overlay(tmp_path, log)
    sink = FakeSink()
    handler = HandlerAutomationRunObserver(sink=sink)

    # a start record alone proves only a start
    log.write_text(f"{stamp(0)} run started\n", encoding="utf-8")
    inside = poll(handler, tmp_path, overlay, T0 + timedelta(minutes=9))
    assert inside.findings == ()
    assert [r["phase"] for r in sink.of(RUN_TOPIC)] == ["started"]

    past = poll(handler, tmp_path, overlay, T0 + timedelta(minutes=11))
    assert [(f.verdict.value, f.reason.value) for f in past.findings] == [
        ("overrun", "run_open_past_max_runtime")
    ]
    overrun = sink.of(VERDICT_TOPIC)[0]
    assert overrun["verdict"] == "overrun"
    assert overrun["verdict_since"].startswith(stamp(10)[:16])

    # reported once
    assert poll(handler, tmp_path, overlay, T0 + timedelta(minutes=12)).findings == ()

    # the completion arrives late: the run finishes, with its exit
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp(13)} run finished exit=3\n")
    poll(handler, tmp_path, overlay, T0 + timedelta(minutes=14))
    finished = sink.of(RUN_TOPIC)[-1]
    assert finished["phase"] == "finished"
    assert finished["exit_code"] == 3
    assert finished["outcome"] == "failed"
    assert finished["started_at"].startswith(stamp(0)[:16])

    # a run that completes inside its bound is never OVERRUN
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp(60)} run started\n{stamp(61)} run finished exit=0\n")
    clean = poll(handler, tmp_path, overlay, T0 + timedelta(minutes=90))
    assert clean.findings == ()
    last = sink.of(RUN_TOPIC)[-1]
    assert (last["phase"], last["outcome"]) == ("finished", "ok")
    assert len(sink.of(VERDICT_TOPIC)) == 1


def test_observer_unreadable_evidence_is_reported_once(tmp_path: Path) -> None:
    overlay, log = receipts_overlay(tmp_path)
    sink = FakeSink()
    handler = HandlerAutomationRunObserver(sink=sink)
    first = poll(handler, tmp_path, overlay, T0)
    assert [(f.verdict.value, f.reason.value) for f in first.findings] == [
        ("unobservable", "evidence_unreadable")
    ]
    assert poll(handler, tmp_path, overlay, T0 + timedelta(minutes=1)).findings == ()
    log.write_text(receipt(0), encoding="utf-8")
    assert poll(handler, tmp_path, overlay, T0 + timedelta(minutes=2)).runs_emitted == 1


def test_observer_entry_with_no_reader_is_unobservable_not_skipped(
    tmp_path: Path,
) -> None:
    overlay = write_overlay(
        tmp_path,
        [
            entry(
                "host-a/daemon",
                trigger={"kind": "systemd-service", "native_id": "example.service"},
                max_runtime_seconds=None,
                evidence={"source": "systemd_journal", "locator": "example.service"},
            )
        ],
    )
    result = poll(HandlerAutomationRunObserver(sink=FakeSink()), tmp_path, overlay, T0)
    assert [f.verdict.value for f in result.findings] == ["unobservable"]
    assert "no reader" in result.findings[0].detail


def test_observer_ignores_other_hosts_and_self_emitting_entries(tmp_path: Path) -> None:
    overlay = write_overlay(
        tmp_path,
        [
            entry(
                "host-b/elsewhere",
                host="host-b",
                evidence={"source": "receipts_file", "locator": "x"},
            ),
            entry("host-a/emits", emitter="self"),
        ],
    )
    sink = FakeSink()
    result = poll(HandlerAutomationRunObserver(sink=sink), tmp_path, overlay, T0)
    assert result.entries_read == 0
    assert [t for t, _ in sink.published] == [HEARTBEAT_TOPIC]
