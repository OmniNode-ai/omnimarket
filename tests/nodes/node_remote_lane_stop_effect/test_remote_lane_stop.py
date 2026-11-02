# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The remote-lane stop node: a running lane is stopped by name and closed once.

Names carry the falsifiers of the ticket: remote_lane_stop_running,
remote_lane_stop_ended_or_unknown, remote_lane_stop_scope.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from omnimarket.nodes.node_remote_lane_stop_effect.handlers.handler_remote_lane_stop import (
    HandlerRemoteLaneStop,
)
from omnimarket.nodes.node_remote_lane_stop_effect.models.model_remote_lane_stop import (
    ModelRemoteLaneStopRequest,
    ModelStopHost,
)
from omnimarket.nodes.node_remote_lane_stop_effect.protocols.local_remote_lane_stop_adapters import (
    LocalStopLedger,
    LocalStopTransport,
)
from tests.nodes.node_remote_lane_stop_effect.conftest import (
    CLAIM_TS,
    LANE,
    RUN_ID,
    write_receipt,
)


def _request(
    receipts: Path, ledger: Path, tool: list[str], **fields: object
) -> ModelRemoteLaneStopRequest:
    base: dict[str, object] = {
        "lane": LANE,
        "reason": "operator asked for a stop",
        "stopping_lane": "orchestrator-1",
        "receipt_dir": str(receipts),
        "ledger_path": str(ledger),
        "ledger_command": tool,
        "actor": "claude:test",
        "model": "sonnet",
        "polite_wait_s": 2.0,
        "kill_wait_s": 2.0,
        "poll_interval_s": 0.05,
        "hosts": [ModelStopHost(name="local", local=True)],
    }
    base.update(fields)
    return ModelRemoteLaneStopRequest.model_validate(base)


def _handler(
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> HandlerRemoteLaneStop:
    return HandlerRemoteLaneStop(
        transport=LocalStopTransport(run=run), ledger=LocalStopLedger(run=run)
    )


def _terminals(ledger: Path) -> list[str]:
    return [
        r
        for r in ledger.read_text(encoding="utf-8").splitlines()
        if "| TERMINAL |" in r
    ]


def _sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
    )


def test_remote_lane_stop_running(
    receipts: Path, ledger: Path, ledger_tool: list[str]
) -> None:
    child = _sleeper()
    try:
        write_receipt(receipts, host="local", pid=child.pid)
        result = _handler().handle(_request(receipts, ledger, ledger_tool))
        assert result.outcome == "stopped", result.message
        assert child.wait(timeout=5) is not None
        rows = _terminals(ledger)
        assert len(rows) == 1
        assert "outcome=stopped" in rows[0]
        assert f"closes-CLAIM={CLAIM_TS}" in rows[0]
        assert "stopped-by=orchestrator-1" in rows[0]
        assert "operator asked for a stop" in rows[0]
        assert "worktree=kept:omni_worktrees/OMN-1/repo" in rows[0]
        assert result.run_id == RUN_ID
        assert result.terminal_ts == "2026-11-02T10:30:00Z"
    finally:
        child.kill()
        child.wait()


def test_remote_lane_stop_ended_or_unknown_final_receipt(
    receipts: Path, ledger: Path, ledger_tool: list[str]
) -> None:
    child = _sleeper()
    try:
        write_receipt(receipts, host="local", pid=child.pid, status="done", final=True)
        result = _handler().handle(_request(receipts, ledger, ledger_tool))
        assert result.outcome == "already-ended"
        assert "done" in result.message
        assert child.poll() is None, "no signal reaches an ended lane's pid"
        assert _terminals(ledger) == []
    finally:
        child.kill()
        child.wait()


def test_remote_lane_stop_ended_or_unknown_terminal_on_ledger(
    receipts: Path, ledger: Path, ledger_tool: list[str]
) -> None:
    child = _sleeper()
    try:
        write_receipt(receipts, host="local", pid=child.pid)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(
                f"2026-11-02T10:20:00Z | TERMINAL | lane={LANE} | ticket=OMN-1 | outcome=done | "
                f"closes-CLAIM={CLAIM_TS} | text\n"
            )
        result = _handler().handle(_request(receipts, ledger, ledger_tool))
        assert result.outcome == "already-ended"
        assert "2026-11-02T10:20:00Z" in result.message
        assert child.poll() is None
        assert len(_terminals(ledger)) == 1
    finally:
        child.kill()
        child.wait()


def test_remote_lane_stop_ended_or_unknown_lane(
    receipts: Path, ledger: Path, ledger_tool: list[str]
) -> None:
    calls: list[Sequence[str]] = []

    def run(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    result = _handler(run).handle(
        _request(receipts, ledger, ledger_tool, lane="no-such-lane")
    )
    assert result.outcome == "unknown-lane"
    assert "no-such-lane" in result.message
    assert calls == []
    assert _terminals(ledger) == []


class FakeLab:
    """A lab host: ssh runs the node's scripts against a process tree that dies on a rule."""

    def __init__(self, dies_on: str | None, events: list[str]) -> None:
        self.dies_on = dies_on  # "TERM", "KILL" or None (never)
        self.alive = True
        self.events = events
        self.argvs: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.argvs.append(list(argv))
        script = str(kwargs.get("input") or "")
        out = ""
        if "STOP-SIGNAL" in script:
            sig = "KILL" if "kill -KILL" in script else "TERM"
            self.events.append(f"signal-{sig}")
            if self.dies_on == sig:
                self.alive = False
            out = "STOP-SIGNALLED\n"
        elif "TREE-ALIVE" in script:
            self.events.append("poll")
            out = "TREE-ALIVE 4242\n" if self.alive else "TREE-GONE\n"
        elif "REAP-UNIT" in script:
            self.events.append("reap-unit")
            if self.dies_on == "KILL":
                self.alive = False
            out = "REAPED\n"
        elif argv and "ledger" in " ".join(argv):
            self.events.append("ledger")
            return subprocess.run(
                list(argv), capture_output=True, text=True, check=False
            )
        return subprocess.CompletedProcess(list(argv), 0, out, "")


def _lab_request(
    receipts: Path, ledger: Path, tool: list[str], **fields: object
) -> ModelRemoteLaneStopRequest:
    host = ModelStopHost(
        name="h9",
        ssh_argv=["ssh", "-o", "BatchMode=yes", "lab@h9"],
        work_root="/lab",
        reap_script="echo REAP-UNIT {unit}",
    )
    return _request(receipts, ledger, tool, hosts=[host], **fields)


def _lab_ledger(
    tool: list[str], events: list[str]
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run_ledger(
        argv: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        events.append("ledger")
        return subprocess.run(list(argv), capture_output=True, text=True, check=False)

    return run_ledger


def _lab_handler(
    lab: FakeLab, tool: list[str], events: list[str]
) -> HandlerRemoteLaneStop:
    clock = {"now": 0.0}

    def sleep(seconds: float) -> None:
        clock["now"] += seconds

    return HandlerRemoteLaneStop(
        transport=LocalStopTransport(run=lab),
        ledger=LocalStopLedger(run=_lab_ledger(tool, events)),
        clock=lambda: clock["now"],
        sleep=sleep,
    )


def test_remote_lane_stop_scope(
    receipts: Path, ledger: Path, ledger_tool: list[str]
) -> None:
    events: list[str] = []
    lab = FakeLab("TERM", events)
    write_receipt(
        receipts, host="h9", pid=1, guard={"unit": "onexlane-fake-lane-1-aaa111"}
    )
    result = _lab_handler(lab, ledger_tool, events).handle(
        _lab_request(receipts, ledger, ledger_tool)
    )
    assert result.outcome == "stopped", result.message
    commands = {argv[0] for argv in lab.argvs}
    assert commands <= {"ssh"}, "the node runs ssh and the ledger tool only"
    assert not any("git" in a or a == "gh" for argv in lab.argvs for a in argv)
    assert events.index("ledger") > max(
        i for i, e in enumerate(events) if e.startswith("signal-")
    )
    last_poll = max(i for i, e in enumerate(events) if e == "poll")
    assert events.index("ledger") > last_poll, (
        "the TERMINAL follows the last poll, which found the tree gone"
    )
    assert len(_terminals(ledger)) == 1


def test_remote_lane_stop_scope_survivor_outlives_the_polite_signal(
    receipts: Path, ledger: Path, ledger_tool: list[str]
) -> None:
    events: list[str] = []
    lab = FakeLab("KILL", events)
    write_receipt(
        receipts, host="h9", pid=1, guard={"unit": "onexlane-fake-lane-1-aaa111"}
    )
    result = _lab_handler(lab, ledger_tool, events).handle(
        _lab_request(receipts, ledger, ledger_tool)
    )
    assert result.outcome == "stopped", result.message
    assert result.signals == ["TERM", "KILL"]
    assert events.index("signal-TERM") < events.index("signal-KILL")
    polls_between = events[
        events.index("signal-TERM") : events.index("signal-KILL")
    ].count("poll")
    assert polls_between >= 2, (
        "the polite signal is given a bounded wait before the forceful one"
    )
    assert "reap-unit" in events
    assert events.index("ledger") > events.index("signal-KILL")


def test_remote_lane_stop_scope_survivor_refused_writes_no_row(
    receipts: Path, ledger: Path, ledger_tool: list[str]
) -> None:
    events: list[str] = []
    lab = FakeLab(None, events)
    write_receipt(
        receipts, host="h9", pid=1, guard={"unit": "onexlane-fake-lane-1-aaa111"}
    )
    result = _lab_handler(lab, ledger_tool, events).handle(
        _lab_request(receipts, ledger, ledger_tool)
    )
    assert result.outcome == "refused"
    assert "4242" in result.message, "the survivor is named"
    assert result.survivor is not None
    assert "ledger" not in events
    assert _terminals(ledger) == []


def test_remote_lane_stop_scope_loser_of_the_single_closer_race_is_a_noop(
    receipts: Path, ledger: Path, ledger_tool: list[str]
) -> None:
    events: list[str] = []

    class RunnerWins(FakeLab):
        def __call__(
            self, argv: Sequence[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            done = super().__call__(argv, **kwargs)
            if "TREE-ALIVE" in str(kwargs.get("input") or "") and not self.alive:
                with ledger.open(
                    "a", encoding="utf-8"
                ) as fh:  # the runner's closeout lands first
                    fh.write(
                        f"2026-11-02T10:29:00Z | TERMINAL | lane={LANE} | ticket=OMN-1 | outcome=blocked | "
                        f"closes-CLAIM={CLAIM_TS} | text\n"
                    )
            return done

    lab = RunnerWins("TERM", events)
    write_receipt(receipts, host="h9", pid=1)
    result = _lab_handler(lab, ledger_tool, events).handle(
        _lab_request(receipts, ledger, ledger_tool)
    )
    assert result.outcome == "already-ended"
    assert "2026-11-02T10:29:00Z" in result.message
    assert len(_terminals(ledger)) == 1


class FakeCloud:
    def __init__(self) -> None:
        self.archived: list[tuple[str, str]] = []

    def archive(self, session_id: str, account: str) -> None:
        self.archived.append((session_id, account))

    def session_ended(self, session_id: str, account: str) -> bool:
        return bool(self.archived)


def test_remote_lane_stop_cloud_session_is_archived_then_closed(
    receipts: Path, ledger: Path, ledger_tool: list[str]
) -> None:
    cloud = FakeCloud()
    write_receipt(
        receipts,
        host="cloud:acct1",
        placement="cloud",
        result={"session_id": "session_01"},
        cloud={"session_id": "session_01", "account": "acct1"},
    )
    handler = HandlerRemoteLaneStop(
        transport=LocalStopTransport(), ledger=LocalStopLedger(), cloud=cloud
    )
    result = handler.handle(_request(receipts, ledger, ledger_tool))
    assert result.outcome == "stopped", result.message
    assert cloud.archived == [("session_01", "acct1")]
    assert len(_terminals(ledger)) == 1


def test_remote_lane_stop_scope_slot_marker_is_cleaned(
    receipts: Path, ledger: Path, ledger_tool: list[str], tmp_path: Path
) -> None:
    placement = tmp_path / "placement"
    placement.mkdir()
    (placement / "h9.1793600000.4321").write_text("4321")
    (placement / "h9.1793600001.9999").write_text("9999")
    events: list[str] = []
    lab = FakeLab("TERM", events)
    write_receipt(receipts, host="h9", pid=4321)
    result = _lab_handler(lab, ledger_tool, events).handle(
        _lab_request(receipts, ledger, ledger_tool, placement_dir=str(placement))
    )
    assert result.outcome == "stopped", result.message
    assert sorted(p.name for p in placement.iterdir()) == ["h9.1793600001.9999"]


def test_remote_lane_stop_scope_local_pid_gone_before_terminal(
    receipts: Path, ledger: Path, ledger_tool: list[str]
) -> None:
    child = _sleeper()
    try:
        write_receipt(receipts, host="local", pid=child.pid)
        started = time.monotonic()
        result = _handler().handle(_request(receipts, ledger, ledger_tool))
        assert result.outcome == "stopped", result.message
        assert child.poll() is not None
        assert time.monotonic() - started < 10
    finally:
        child.kill()
        child.wait()


def test_remote_lane_stop_lab_command(
    receipts: Path, ledger: Path, ledger_tool: list[str], tmp_path: Path
) -> None:
    """Exercise the command and shell transport against a real process group.

    The context and receipt use the runner's format. This portable regression
    uses a local transport and a scratch ledger; a live runner proof is separate.
    """
    context = tmp_path / "runs" / f"{RUN_ID}.ctx"
    context.mkdir(parents=True)
    child = _sleeper()
    # Reap the child before the shell's signal-0 poll, as the runner's guard does.
    reaper = threading.Thread(target=child.wait)
    reaper.start()
    placement = tmp_path / "placement"
    placement.mkdir()
    marker = placement / f"h9.1793600000.{child.pid}"
    marker.write_text(str(child.pid))
    peer_marker = placement / "h9.1793600001.9999"
    peer_marker.write_text("9999")
    (context / "run.pid").write_text(str(child.pid))
    write_receipt(receipts, host="h9", pid=child.pid)
    request = _request(
        receipts,
        ledger,
        ledger_tool,
        hosts=[ModelStopHost(name="h9", ssh_argv=["env"], work_root=str(tmp_path))],
        placement_dir=str(placement),
    )
    try:
        # Use the same module command documented for dispatch hosts, with the
        # real adapter's shell scripts; only its SSH prefix is local here.
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(sys.path)
        stopped = subprocess.run(
            [sys.executable, "-m", "omnimarket.nodes.node_remote_lane_stop_effect"],
            input=request.model_dump_json(),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=env,
        )
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        result = json.loads(stopped.stdout)
        assert result["outcome"] == "stopped"
        assert result["lane"] == LANE
        assert result["run_id"] == RUN_ID
        assert result["signals"] == ["TERM"]
        reaper.join(timeout=5)
        assert not reaper.is_alive()
        assert child.returncode is not None
        assert not marker.exists()
        assert peer_marker.exists(), "a peer lane's slot is retained"
        rows = _terminals(ledger)
        assert len(rows) == 1
        assert f"lane={LANE}" in rows[0]
        assert f"run={RUN_ID}" in rows[0]
        assert "outcome=stopped" in rows[0]
        assert "stopped-by=orchestrator-1" in rows[0]
        assert "operator asked for a stop" in rows[0]
        assert f"closes-CLAIM={CLAIM_TS}" in rows[0]
        assert result["terminal_ts"] in rows[0]
    finally:
        if child.poll() is None:
            child.kill()
        reaper.join(timeout=5)
