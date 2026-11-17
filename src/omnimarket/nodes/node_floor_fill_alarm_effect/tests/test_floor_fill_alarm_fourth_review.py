# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression controls for runtime invocation, restarted lanes and revision proofs."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_floor_fill_alarm_compute.models import ModelFloorFillSentLane
from omnimarket.nodes.node_floor_fill_alarm_effect.handlers.handler_floor_fill_alarm_check import (
    HandlerFloorFillAlarmCheck,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.runtime_floor_fill_proof import (
    ProofMissingError,
    lab_head,
    no_handoff,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.runtime_floor_fill_proof import (
    main as proof_main,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.tests.support_alarm_section import (
    alarm_section,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.tests.support_ledger_grammar import (
    AUTHENTICATED_PRINCIPAL,
    real_writer_ledger,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.tests.test_floor_fill_alarm_review_findings import (
    _Lanes,
    _Ledger,
    _Pool,
    _request,
    _Runs,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
    EnumFloorFillStatus,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.store import (
    InMemoryFloorStateStore,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import harness, overlay
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support_live_tick import (
    live_section,
    recorded_floor,
    write_snapshot,
)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 11, 16, 17, tzinfo=UTC)


@pytest.mark.parametrize("late", [False, True])
def test_floor_fill_alarm_restart_writes_a_second_death_or_late_claim(
    late: bool,
) -> None:
    ledger = _Ledger()

    def run(start: datetime) -> Any:
        lane = ModelFloorFillSentLane(
            lane="same-lane",
            key="floor:a:b",
            sent_at=start,
            ended=not late,
            claim_at=start + timedelta(minutes=11) if late else None,
        )
        return HandlerFloorFillAlarmCheck(
            runs=_Runs(), lanes=_Lanes((lane,)), pool=_Pool(), ledger=ledger
        ).handle(_request(start + timedelta(minutes=12)))

    assert len(run(NOW).rows_written) == 1
    assert run(NOW).rows_written == ()
    assert len(run(NOW + timedelta(hours=1)).rows_written) == 1
    assert len(ledger.held) == 2


def test_floor_fill_alarm_old_ended_lanes_do_not_alarm_at_first_check() -> None:
    from omnimarket.nodes.node_floor_fill_alarm_effect.handlers.adapter_floor_fill_alarm_sources import (
        StoredStateSentLanes,
    )

    state = json.dumps(
        {
            "seq": 1,
            "entries": {
                "floor:a:b": {
                    "key": "floor:a:b",
                    "state": "SENT",
                    "lane": "old",
                    "attempt": 1,
                    "ticket": "T-1",
                    "ended": True,
                    "sent_at": (NOW - timedelta(days=30)).isoformat(),
                }
            },
        }
    )
    source = StoredStateSentLanes(
        lambda _: state, "scope", lambda: (), clock=lambda: NOW
    )
    assert source.read_sent_lanes() == ()


def test_floor_fill_alarm_main_live_tick_invokes_the_real_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omnimarket.nodes.node_floor_fill_orchestrator import (
        runtime_floor_fill_orchestrator as runtime,
    )
    from omnimarket.nodes.node_floor_fill_orchestrator.container import container_live
    from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
        ScriptedTickets,
    )

    store = InMemoryFloorStateStore()
    now = datetime.now(UTC)
    write_snapshot(tmp_path / "pool.json", free=2, read_at=now)
    (tmp_path / "ledger.md").write_text("")
    config = overlay(
        plans_root=str(tmp_path),
        runner_command=["true"],
        process_working_directory=str(tmp_path),
        stopped_reports_root=str(tmp_path),
        stopped_brief_directory=str(tmp_path / "briefs"),
    )
    floor, plans = recorded_floor(now)
    for name in plans:
        (tmp_path / f"{name}.md").write_text(f"# {name}\n")
    original = container_live.build_live_handler_factory
    monkeypatch.setattr(
        container_live, "build_live_store", lambda *_args, **_kwargs: store
    )
    monkeypatch.setattr(
        container_live,
        "build_live_handler_factory",
        lambda settings, **kw: original(
            settings,
            **kw,
            fetch_text=lambda *_args: floor,
            tickets=ScriptedTickets(),
            brake=lambda: False,
            stop=lambda _: None,
        ),
    )
    monkeypatch.setenv("EXAMPLE_FLOOR_FILL_DSN", "unused")
    section = alarm_section(tmp_path)
    payload = {
        "floor_fill": config,
        "floor_fill_live": live_section(tmp_path),
        "floor_fill_alarm": section,
        "lab_hosts": [{"name": config["host_id"], "available": True}],
    }
    code = runtime.main(
        [
            "--overlay-json",
            json.dumps(payload),
            "--this-host",
            config["host_id"],
            "--profile",
            "live",
            "tick",
        ]
    )
    output = capsys.readouterr().out
    assert code == 0, output
    assert "NOTE alarm-check" in output
    assert store.load(config["scope"] + ":answer").payload is not None
    log = [
        json.loads(line)
        for line in Path(section["check_log_path"]).read_text().splitlines()
    ]
    assert log[-1]["outcome"] == "checked"
    assert log[-1]["runs_read"] == 1


def test_floor_fill_alarm_runtime_store_constructor_never_connects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.handlers import floor_fill_state_store_postgres as postgres
    from omnimarket.nodes.node_floor_fill_orchestrator.container.container_live import (
        build_live_store,
    )
    from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
        load_floor_fill_live_settings,
    )

    def forbidden(*a: Any, **kw: Any) -> Any:
        raise AssertionError("constructor connected")

    monkeypatch.setattr(postgres.psycopg2, "connect", forbidden)
    assert (
        build_live_store(
            load_floor_fill_live_settings(live_section(tmp_path)), dsn="unused"
        )
        is not None
    )


def test_floor_fill_alarm_both_store_writes_fail_keep_failed_answer() -> None:
    h = harness(config=overlay(max_fill_lanes_per_line=1))
    inner = h.store
    failures: list[str] = []

    class Broken:
        def load(self, key: str) -> Any:
            return inner.load(key)

        def compare_and_set(
            self, key: str, expected_version: int, new_version: int, payload: str
        ) -> bool:
            if h.starter.starts:
                failures.append(key)
                raise FloorFillUnreadableError("store-down")
            return inner.compare_and_set(key, expected_version, new_version, payload)

    h.store = Broken()
    answer = h.orchestrator().run(EnumFloorFillCommand.TICK)
    assert answer.status is EnumFloorFillStatus.FAILED
    assert len(failures) == 2
    assert failures[-1].endswith(":answer")
    assert any(item.kind == "started" for item in answer.key_results)
    assert "store-down" in " ".join(answer.lines)


def test_floor_fill_alarm_unbound_handler_is_not_advertised_on_bus() -> None:
    contract = yaml.safe_load((Path(__file__).parents[1] / "contract.yaml").read_text())
    assert not contract.get("handler_routing")
    assert not contract.get("runtime_dispatch")
    assert not contract.get("event_bus", {}).get("subscribe_topics")
    assert not contract.get("broker_acl_grants")


def test_floor_fill_proof_sending_runs_ignore_preinstall_revision(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.md"
    head = "a" * 40
    ledger.write_text(
        f"{NOW.isoformat()} | STATUS | lane=l | ticket=OMN-24873 | installed=h051:{head[:12]} | observed=installed\n"
    )
    runs = tmp_path / "runs.jsonl"

    def write(at: datetime) -> None:
        runs.write_text(
            "\n".join(
                json.dumps(
                    {
                        "seq": seq,
                        "run": {"lanes_sent": 1, "observed_at": at.isoformat()},
                    }
                )
                for seq in (1, 2)
            )
        )

    args = [
        "sending-runs",
        "--ledger",
        str(ledger),
        "--ticket",
        "OMN-24873",
        "--host",
        "h051",
        "--head",
        head,
        "--runs",
        str(runs),
    ]
    write(NOW - timedelta(seconds=1))
    assert proof_main(args) == 1
    write(NOW)
    assert proof_main(args) == 0


def test_floor_fill_proof_handoff_reads_real_grammar_with_pr_list(
    tmp_path: Path,
) -> None:
    ledger, path = real_writer_ledger(tmp_path)
    # The MSG fields were read from the real handoff ledger row. The writer
    # supplies authenticated principal/receipt cells for this regression row.
    ledger.append(
        f"2026-11-16T19:43:05Z | MSG | from=alarm-control | principal={AUTHENTICATED_PRINCIPAL} | "
        "to=landing-controller | id=2026-11-16T19:43:05Z-alarm-control | ticket=OMN-24873 | "
        "repo=omnimarket | pr=omnimarket#1,omnimarket#6333 | "
        "head=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa | needs=land | regression handoff row"
    )
    assert "req=" in path.read_text()
    with pytest.raises(ProofMissingError, match="handoff"):
        no_handoff(path, "omnimarket#6333")
    no_handoff(path, "omnimarket#633")


def test_floor_fill_proof_lab_head_excludes_base_and_parses_first_body_line(
    tmp_path: Path,
) -> None:
    env = {
        **os.environ,
        "GIT_DIR": str(tmp_path / ".git"),
        "GIT_WORK_TREE": str(tmp_path),
    }

    def git(*args: str) -> str:
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.invalid",
                *args,
            ],
            env=env,
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/dev", base)
    git("commit", "-q", "--allow-empty", "-m", "change")
    head = git("rev-parse", "HEAD")
    body = tmp_path / "body.json"
    body.write_text(
        json.dumps({"facts": {"body": f"Lab: head={head} command=test exit=0"}})
    )
    lab_head(body, tmp_path)
    body.write_text(json.dumps({"facts": {"body": f"Lab: head={base}"}}))
    with pytest.raises(ProofMissingError):
        lab_head(body, tmp_path)


def test_floor_fill_alarm_real_writer_claim_has_whole_lane_and_key_cells(
    tmp_path: Path,
) -> None:
    ledger, path = real_writer_ledger(tmp_path)
    row = f"2026-11-16T17:00:00Z | CLAIM | lane=writer-control | principal={AUTHENTICATED_PRINCIPAL} | actor=script:test | model=none | ticket=OMN-24873 | key=floor:a:b | worktree=none | est ~1 lane-hours; displaces regression work; (OMN-24873)"
    ledger.append(row)
    written = path.read_text()
    cells = written.strip().split(" | ")
    assert "lane=writer-control" in cells
    assert "key=floor:a:b" in cells
    assert "req=" in written
    assert f"principal={AUTHENTICATED_PRINCIPAL}" in cells


def test_floor_fill_alarm_schedule_has_a_timer_independent_of_tick(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_floor_fill_alarm_effect.tests.test_floor_fill_alarm_review_findings import (
        _install,
    )

    section = alarm_section(tmp_path)
    section["ledger_append_command"] = ["/srv/example/bin/ledger-append", "{row}"]
    code, output, error = _install(
        tmp_path,
        {"floor_fill_live": live_section(tmp_path), "floor_fill_alarm": section},
    )
    assert code == 0, error
    commands = [line for line in output.splitlines() if line.startswith("ExecStart=")]
    assert len(commands) == 2
    assert any(command.endswith(" alarm-check") for command in commands)
    assert "Unit=onex-floor-fill-tick-alarm.service" in output
    assert output.count("[Timer]") == 2


def test_floor_fill_alarm_own_timer_runs_when_tick_is_dead_or_timed_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omnimarket.nodes.node_floor_fill_orchestrator import (
        runtime_floor_fill_orchestrator as runtime,
    )
    from omnimarket.nodes.node_floor_fill_orchestrator.container import container_live

    section = alarm_section(tmp_path)
    write_snapshot(tmp_path / "pool.json", free=25, read_at=datetime.now(UTC))
    store = InMemoryFloorStateStore()
    monkeypatch.setattr(
        container_live, "build_live_store", lambda *_args, **_kwargs: store
    )

    def dead_tick(*a: Any, **kw: Any) -> Any:
        raise AssertionError("an independent alarm must not run a tick")

    monkeypatch.setattr(runtime, "run_entry", dead_tick)
    monkeypatch.setenv("EXAMPLE_FLOOR_FILL_DSN", "unused")
    config = overlay()
    payload = {
        "floor_fill": config,
        "floor_fill_live": live_section(tmp_path),
        "floor_fill_alarm": section,
        "lab_hosts": [{"name": config["host_id"], "available": True}],
    }
    code = runtime.main(
        [
            "--overlay-json",
            json.dumps(payload),
            "--this-host",
            config["host_id"],
            "--profile",
            "live",
            "alarm-check",
        ]
    )
    assert code == 0, capsys.readouterr().out
    assert "alarm=floor-fill-not-running" in Path(section["ledger_path"]).read_text()


def test_floor_fill_alarm_helper_executes_real_append_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import runpy
    import sys

    from omnimarket.nodes.node_floor_fill_alarm_effect.tests.support_ledger_grammar import (
        REAL_WRITER_APPENDER,
    )
    from omnimarket.nodes.node_work_ledger_append_effect import (
        HandlerWorkLedgerAppendEffect,
    )

    ledger, path = real_writer_ledger(tmp_path)
    calls: list[str] = []
    real_handle = HandlerWorkLedgerAppendEffect.handle

    def traced(self: Any, request: Any, **kwargs: Any) -> Any:
        calls.append(request.rows)
        return real_handle(self, request, **kwargs)

    monkeypatch.setattr(HandlerWorkLedgerAppendEffect, "handle", traced)
    row = f"2026-11-16T17:00:00Z | STATUS | lane=writer-control | principal={AUTHENTICATED_PRINCIPAL} | actor=script:test | model=none | ticket=OMN-24873 | control=real-writer"
    script = tmp_path / "probe_writer.py"
    script.write_text(REAL_WRITER_APPENDER)
    monkeypatch.setattr(
        sys, "argv", [str(script), str(path), AUTHENTICATED_PRINCIPAL, row]
    )
    runpy.run_path(str(script), run_name="__main__")
    assert len(calls) == 1
    assert "control=real-writer" in calls[0]
    assert "control=real-writer" in path.read_text()
    assert ledger.rows()


def test_floor_fill_alarm_independent_and_after_tick_checks_are_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from omnimarket.nodes.node_floor_fill_alarm_effect.container.container_live import (
        run_alarm_check,
    )
    from omnimarket.nodes.node_floor_fill_alarm_effect.models import (
        load_floor_fill_alarm_settings,
    )

    settings = load_floor_fill_alarm_settings(alarm_section(tmp_path))
    write_snapshot(tmp_path / "pool.json", free=25, read_at=datetime.now(UTC))
    entered = threading.Event()
    release = threading.Event()
    overlapped = threading.Event()
    original = HandlerFloorFillAlarmCheck.handle
    active = 0
    guard = threading.Lock()

    def traced(self: Any, request: Any) -> Any:
        nonlocal active
        with guard:
            active += 1
            if active > 1:
                overlapped.set()
        entered.set()
        release.wait(30)
        try:
            return original(self, request)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(HandlerFloorFillAlarmCheck, "handle", traced)

    def check() -> Any:
        return run_alarm_check(settings, load_payload=lambda _: None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(check)
        assert entered.wait(15)
        second = executor.submit(check)
        try:
            assert not overlapped.wait(0.3), (
                "both timers entered the ledger dedupe/write section"
            )
        finally:
            release.set()
        results = [first.result(timeout=45), second.result(timeout=45)]
    assert sum(len(result.rows_written) for result in results) == 1
