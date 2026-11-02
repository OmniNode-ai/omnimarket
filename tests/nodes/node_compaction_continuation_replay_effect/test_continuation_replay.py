# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CM.11 falsifiers: stubs isolate, refused calls are logged, ledger writes land in scratch only."""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_compaction_continuation_replay_effect.handlers.handler_continuation_replay import (
    HandlerContinuationReplay,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.handlers.stubs import (
    SandboxTools,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.models import (
    EnumRefusalReason,
    EnumReplayStatus,
    EnumToolOutcome,
    ModelRecordedCall,
    ModelReplayRequest,
    ModelToolCall,
)

pytestmark = pytest.mark.unit

ROW = "2026-11-02T10:00:00Z | STATUS | lane=x | ticket=OMN-1 | note"


def _real_ledger(tmp_path: Path) -> Path:
    real = tmp_path / "real" / "ROLLING_WORK_LEDGER.md"
    real.parent.mkdir()
    real.write_text("2026-11-01T00:00:00Z | CLAIM | lane=a | ticket=OMN-1 | seed\n")
    return real


def _request(
    tmp_path: Path,
    turns: list[list[ModelToolCall]],
    real: Path,
    frozen_files: dict[str, str] | None = None,
    scratch_worktree: str | None = None,
) -> ModelReplayRequest:
    return ModelReplayRequest(
        correlation_id="c1",
        boundary_id="b1",
        arm="stock",
        repeat_index=0,
        n_turns=5,
        scratch_dir=str(tmp_path / "scratch"),
        ledger_source=str(real),
        scripted_turns=turns,
        frozen_files=frozen_files or {},
        scratch_worktree=scratch_worktree,
    )


def test_refused_call_logged_gh_pr_merge(tmp_path: Path) -> None:
    real = _real_ledger(tmp_path)
    call = ModelToolCall(
        tool="bash", command="gh pr merge 12 --squash --repo OmniNode-ai/omnimarket"
    )
    result = HandlerContinuationReplay().handle(_request(tmp_path, [[call]], real))
    assert result.status == EnumReplayStatus.COMPLETED
    assert [c.call_class for c in result.refusal_log] == ["gh.pr.merge"]
    entry = result.refusal_log[0]
    assert entry.outcome == EnumToolOutcome.REFUSED
    assert entry.refusal_reason == EnumRefusalReason.REFUSED_BY_DESIGN
    assert (
        "gh.pr.merge|" in result.proposed_call_set
    )  # proposed calls are recorded, not dropped


def test_scratch_ledger_only(tmp_path: Path) -> None:
    real = _real_ledger(tmp_path)
    before = real.read_bytes()
    calls = [
        ModelToolCall(
            tool="ledger_append", row=ROW, path=str(real)
        ),  # names the real ledger
        ModelToolCall(tool="bash", command=f"onex-ledger {real} --append '{ROW}2'"),
    ]
    result = HandlerContinuationReplay().handle(_request(tmp_path, [calls], real))
    assert real.read_bytes() == before
    assert result.status == EnumReplayStatus.COMPLETED
    assert result.scratch_rows[-2:] == [ROW, f"{ROW}2"]
    assert result.scratch_ledger_path != str(real)
    assert Path(result.scratch_ledger_path).read_text().splitlines()[0].endswith("seed")
    assert "redirected from" in result.calls[0].detail


def test_refusals_are_typed(tmp_path: Path) -> None:
    real = _real_ledger(tmp_path)
    frozen = {"/pinned/a.txt": "pinned bytes"}
    calls = [
        ModelToolCall(tool="read_file", path="/pinned/a.txt"),
        ModelToolCall(tool="read_file", path="/etc/passwd"),
        ModelToolCall(tool="bash", command="curl https://example.invalid/x"),
        ModelToolCall(tool="bash", command="git push origin main"),
        ModelToolCall(
            tool="bash", command="onex scm pr-merge OmniNode-ai/omnimarket#1"
        ),
        ModelToolCall(tool="bash", command="echo 'unterminated"),
        ModelToolCall(tool="bash", command="rm -rf /"),
        ModelToolCall(tool="network", command="https://x.invalid"),
        ModelToolCall(tool="write_file", path="/tmp/x"),
    ]
    result = HandlerContinuationReplay().handle(
        _request(tmp_path, [calls], real, frozen_files=frozen)
    )
    assert [c.refusal_reason for c in result.calls] == [
        None,
        EnumRefusalReason.UNPINNED_READ,
        EnumRefusalReason.NETWORK,
        EnumRefusalReason.NETWORK,
        EnumRefusalReason.REFUSED_BY_DESIGN,
        EnumRefusalReason.MALFORMED,
        EnumRefusalReason.UNSUPPORTED_TOOL,
        EnumRefusalReason.NETWORK,
        EnumRefusalReason.UNSUPPORTED_TOOL,
    ]
    assert result.calls[0].detail == "pinned bytes"


def test_item_key_and_turn_cap(tmp_path: Path) -> None:
    real = _real_ledger(tmp_path)
    turns = [
        [ModelToolCall(tool="bash", command=f"gh issue view OMN-{i}")] for i in range(9)
    ]
    req = _request(tmp_path, turns, real).model_copy(update={"n_turns": 3})
    result = HandlerContinuationReplay().handle(req)
    assert result.turns_run == 3
    assert result.proposed_call_set == [
        "gh.issue.view|OMN-0",
        "gh.issue.view|OMN-1",
        "gh.issue.view|OMN-2",
    ]


# --- AC1: stubs_isolate. A mutant stub that lets one call through must be caught. ---


def test_stubs_isolate_clean_stubs_reach_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = _real_ledger(tmp_path)
    reached: list[str] = []

    def _net(*_a: object, **_k: object) -> None:
        reached.append("net")

    def _proc(*_a: object, **_k: object) -> None:
        reached.append("proc")

    monkeypatch.setattr(socket.socket, "connect", _net)
    monkeypatch.setattr(subprocess.Popen, "__init__", _proc)
    calls = [
        ModelToolCall(tool="bash", command="gh pr merge 1"),
        ModelToolCall(tool="bash", command="curl http://127.0.0.1:9/"),
        ModelToolCall(tool="bash", command="onex run-node x"),
    ]
    result = HandlerContinuationReplay().handle(_request(tmp_path, [calls], real))
    assert reached == []
    assert result.status == EnumReplayStatus.COMPLETED
    assert result.isolation_violations == []


class _NetworkMutant(SandboxTools):
    """Mutant: gh passes through to a real connection instead of refusing."""

    def dispatch(self, turn: int, call: ModelToolCall) -> ModelRecordedCall:
        if call.command.startswith("gh "):
            socket.create_connection(("127.0.0.1", 9), timeout=0.2)
        return super().dispatch(turn, call)


class _ProcessMutant(SandboxTools):
    def dispatch(self, turn: int, call: ModelToolCall) -> ModelRecordedCall:
        if call.command.startswith("gh "):
            subprocess.run(["true"], check=False)
        return super().dispatch(turn, call)


class _LedgerMutant(SandboxTools):
    """Mutant: the ledger write goes to the path the call names, the real ledger."""

    def dispatch(self, turn: int, call: ModelToolCall) -> ModelRecordedCall:
        if call.tool == "ledger_append" and call.path:
            with open(call.path, "a", encoding="utf-8") as fh:
                fh.write(call.row + "\n")
        return super().dispatch(turn, call)


@pytest.mark.parametrize("mutant", [_NetworkMutant, _ProcessMutant])
def test_stubs_isolate_catches_egress_mutant(
    tmp_path: Path, mutant: type[SandboxTools]
) -> None:
    real = _real_ledger(tmp_path)
    call = ModelToolCall(tool="bash", command="gh pr merge 1")
    result = HandlerContinuationReplay(stubs_factory=mutant).handle(
        _request(tmp_path, [[call]], real)
    )
    assert result.status == EnumReplayStatus.ISOLATION_BREACH
    assert result.isolation_violations
    assert result.calls[0].refusal_reason == EnumRefusalReason.EGRESS_BLOCKED


def test_stubs_isolate_catches_real_ledger_mutant(tmp_path: Path) -> None:
    real = _real_ledger(tmp_path)
    call = ModelToolCall(tool="ledger_append", row=ROW, path=str(real))
    result = HandlerContinuationReplay(stubs_factory=_LedgerMutant).handle(
        _request(tmp_path, [[call]], real)
    )
    assert result.status == EnumReplayStatus.ISOLATION_BREACH
    assert any(
        v.startswith("protected_file_changed") for v in result.isolation_violations
    )


def test_stubs_isolate_guard_restores_sockets(tmp_path: Path) -> None:
    real = _real_ledger(tmp_path)
    connect = socket.socket.connect
    popen_init = subprocess.Popen.__init__
    HandlerContinuationReplay(stubs_factory=_NetworkMutant).handle(
        _request(
            tmp_path, [[ModelToolCall(tool="bash", command="gh pr merge 1")]], real
        )
    )
    assert socket.socket.connect is connect
    assert subprocess.Popen.__init__ is popen_init


def test_scratch_dir_may_not_contain_the_real_ledger(tmp_path: Path) -> None:
    real = _real_ledger(tmp_path)
    with pytest.raises(ValueError, match="protected"):
        ModelReplayRequest(
            correlation_id="c",
            boundary_id="b",
            arm="a",
            repeat_index=0,
            scratch_dir=str(real.parent),
            ledger_source=str(real),
        )


def test_diff_collected_by_git_diff_head(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()

    def git(*a: str) -> None:
        subprocess.run(
            ["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t", *a],
            check=True,
            capture_output=True,
            env=scrub_git_location_env(os.environ),
        )

    git("init", "-q")
    (wt / "a.txt").write_text("one\n")
    git("add", "a.txt")
    git("commit", "-qm", "init")
    (wt / "a.txt").write_text("two\n")  # unstaged
    (wt / "b.txt").write_text("new\n")
    git("add", "b.txt")  # staged only: plain `git diff` would miss it
    real = _real_ledger(tmp_path)
    result = HandlerContinuationReplay().handle(
        _request(tmp_path, [], real, scratch_worktree=str(wt))
    )
    assert result.diff is not None
    assert "+two" in result.diff
    assert "b.txt" in result.diff
