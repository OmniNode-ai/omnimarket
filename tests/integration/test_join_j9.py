# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J9 (piece 9 to piece 10): a merged fix is installed on every target host within an hour and read back equal.

Rebuild plan 4.1, J9. Piece 9 (landing) merges a head; piece 10 (install and sync) installs it. The rig runs the
node's own tick, ``HandlerAutomaticFixInstall``, against a scratch git repository whose released ref is the merged
head and against two declared targets whose installer, read-back and observation commands are injected (nothing here
touches a real host, the ledger or GitHub). The J9 join handler then reads the receipts the tick wrote against the
merged head and the one-hour deadline of the node's contract, and answers piece 10 on the floor's emission contract.

The join is judged the way the floor judges it: piece 9's chain and piece 10's chain share one correlation id and the
floor's own receipt fold reads the pair.

Run the two cases with ``-k golden`` and ``-k error``.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.handlers.emission_sink import ChainEmitter, InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceDef,
)
from omnimarket.nodes.node_automatic_fix_install_effect import (
    HandlerAutomaticFixInstall,
    ModelFixInstallOverlay,
    ModelFixInstallRequest,
    ModelFixInstallTarget,
)
from omnimarket.nodes.node_automatic_fix_install_effect.handlers.handler_fix_install_join_j9 import (
    HandlerFixInstallJoinJ9,
    load_join_j9_policy,
)
from omnimarket.nodes.node_automatic_fix_install_effect.models import (
    EnumJoinJ9Outcome,
    ModelFixInstallResult,
    ModelJoinJ9Reading,
    ModelJoinJ9Request,
    ModelJoinJ9Target,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)

pytestmark = pytest.mark.integration

MERGED_AT = datetime(2026, 11, 8, 9, 0, 0, tzinfo=UTC)
POLICY = load_join_j9_policy()
HOUR = timedelta(seconds=POLICY.deadline_seconds)
HOSTS = ("lab-a", "lab-b")
CHAIN_DEADLINE_S = 900


class _Clock:
    """A clock that moves one second per read, so a chain's events keep their order."""

    def __init__(self, start: datetime) -> None:
        self._t = start

    def __call__(self) -> datetime:
        self._t += timedelta(seconds=1)
        return self._t


class _Rig:
    """A scratch repository whose released ref is the merged head, and two lab targets that still run its parent."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "dev")
        self.parent = self._commit("base.txt", "base\n")
        self.merged = self._commit("fix.txt", "the merged fix\n")
        self._git("update-ref", "refs/remotes/origin/dev", self.merged)
        self.live = dict.fromkeys(HOSTS, self.parent)
        self.refusing: set[str] = set()
        self.watcher = root / "watcher.json"
        self.proofs = root / "proofs.json"
        self.sink = InMemoryEmissionSink()

    def _git(self, *argv: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(self.repo), *argv],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
            env={
                **scrub_git_location_env(os.environ),
                "GIT_AUTHOR_NAME": "fixture",
                "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                "GIT_COMMITTER_NAME": "fixture",
                "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            },
        )
        return done.stdout.strip()

    def _commit(self, name: str, text: str) -> str:
        (self.repo / name).write_text(text)
        self._git("add", name)
        self._git("-c", "commit.gpgsign=false", "commit", "-qm", f"fixture {name}")
        return self._git("rev-parse", "HEAD")

    def _run(
        self, argv: tuple[str, ...], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] == "read-installed":
            return subprocess.CompletedProcess(argv, 0, self.live[argv[1]], "")
        if argv[0] == "existing-installer":
            if argv[1] in self.refusing:
                return subprocess.CompletedProcess(argv, 1, "", "host unreachable")
            self.live[argv[1]] = argv[2]
            return subprocess.CompletedProcess(argv, 0, "installed", "")
        if argv[0] == "observe":
            return subprocess.CompletedProcess(argv, 0, "next call ran", "")
        raise AssertionError(argv)

    def tick(self, at: datetime) -> ModelFixInstallResult:
        """One tick of the node's own handler at ``at``, with no open fix to compose."""
        self.watcher.write_text(f'{{"last_tick": "{at.isoformat()}", "prs": {{}}}}')
        self.proofs.write_text('{"passes": {}, "fails": {}}')
        overlay = ModelFixInstallOverlay(
            state_dir=self.root / "state",
            watcher_state=self.watcher,
            proof_state=self.proofs,
            targets=tuple(
                ModelFixInstallTarget(
                    key=host,
                    host=host,
                    surface="plugin",
                    repository="fixture",
                    clone=self.repo,
                    released_ref="origin/dev",
                    installed_command=("read-installed", host),
                    install_command=("existing-installer", host, "{revision}"),
                    verify_command=("observe", host, "{revision}"),
                )
                for host in HOSTS
            ),
        )
        return HandlerAutomaticFixInstall(
            overlay=overlay, run=self._run, clock=lambda: at
        ).handle(ModelFixInstallRequest())

    def piece9_merged(self, cid: str) -> None:
        """Piece 9 as the landing node answers: a start, then a success carrying the merge."""
        chain = ChainEmitter(
            self.sink,
            node="node_pr_landing_decision_compute",
            piece=POLICY.from_piece,
            clock=_Clock(MERGED_AT),
            correlation_id=cid,
            backend="in_memory",
        )
        chain.start()
        chain.success({"merges_decided": 1})

    def join(self, cid: str, at: datetime) -> HandlerFixInstallJoinJ9:
        return HandlerFixInstallJoinJ9(
            sink=self.sink, policy=POLICY, clock=_Clock(at), backend="in_memory"
        )

    def request(
        self,
        cid: str,
        judged_at: datetime,
        readings: list[ModelJoinJ9Reading],
        *,
        merged_head: str | None = None,
    ) -> ModelJoinJ9Request:
        return ModelJoinJ9Request(
            correlation_id=cid,
            merged_head=merged_head or self.merged,
            merged_at=MERGED_AT,
            judged_at=judged_at,
            targets=tuple(ModelJoinJ9Target(key=h, host=h) for h in HOSTS),
            readings=tuple(readings),
        )


def _readings(result: ModelFixInstallResult) -> list[ModelJoinJ9Reading]:
    return [ModelJoinJ9Reading(at=result.at, receipt=r) for r in result.targets]


def _fold(emissions: list[ModelNodeEmission], now: datetime) -> tuple[str, list[str]]:
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=now,
            emissions=tuple(emissions),
            pieces=tuple(
                ModelPieceDef(piece=p, name=f"piece {p}", reading_max_age_s=86400)
                for p in (POLICY.from_piece, POLICY.to_piece)
            ),
            joins=(
                ModelJoinDef(
                    id=POLICY.join_id,
                    from_piece=POLICY.from_piece,
                    to_piece=POLICY.to_piece,
                    label="landing to install and sync",
                ),
            ),
            freshness_max_age_s=86400,
            error_max_age_s=86400,
            chain_deadline_s=CHAIN_DEADLINE_S,
        )
    )
    join = next(j for j in receipts.joins if j.join == POLICY.join_id)
    return join.state, [f.code for f in receipts.findings]


def test_policy_reads_the_hour_from_the_contract() -> None:
    assert POLICY.join_id == "J9"
    assert (POLICY.from_piece, POLICY.to_piece) == (9, 10)
    assert POLICY.deadline_seconds == 3600


# ---------------------------------------------------------------------------------------------- golden


def test_golden_merged_fix_is_installed_on_every_target_within_the_hour(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    cid = "j9-golden"
    rig.piece9_merged(cid)
    at = MERGED_AT + timedelta(minutes=10)
    result = rig.tick(at)
    assert result.status == "completed"
    assert rig.live == dict.fromkeys(HOSTS, rig.merged), (
        "each host reads back the merged head"
    )

    joined = rig.join(cid, at).handle(
        rig.request(cid, at + timedelta(minutes=1), _readings(result))
    )
    assert joined.outcome is EnumJoinJ9Outcome.INSTALLED
    assert joined.installed == HOSTS
    assert joined.failures == ()
    assert joined.failure_code is None
    piece10 = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in piece10] == ["start", "success"]
    state, findings = _fold(rig.sink.events, at + timedelta(minutes=5))
    assert state == "golden_only", findings
    assert findings == []


def test_golden_a_target_already_at_the_merged_head_reads_current_and_counts(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.live["lab-a"] = rig.merged
    at = MERGED_AT + timedelta(minutes=3)
    result = rig.tick(at)
    assert {r.host: r.status for r in result.targets} == {
        "lab-a": "current",
        "lab-b": "installed",
    }
    joined = rig.join("j9-current", at).handle(
        rig.request("j9-current", at, _readings(result))
    )
    assert joined.outcome is EnumJoinJ9Outcome.INSTALLED


def test_golden_a_target_not_yet_installed_inside_the_hour_is_pending_not_failed(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    rig.refusing.add("lab-b")
    at = MERGED_AT + timedelta(minutes=10)
    result = rig.tick(at)
    joined = rig.join("j9-pending", at).handle(
        rig.request("j9-pending", at + timedelta(minutes=20), _readings(result))
    )
    assert joined.outcome is EnumJoinJ9Outcome.PENDING
    assert joined.pending == ("lab-b",)
    assert joined.installed == ("lab-a",)
    assert rig.sink.events == [], "an unjudged join emits nothing"


# ---------------------------------------------------------------------------------------------- error


def test_error_a_host_that_does_not_install_within_the_hour_is_named_and_the_join_reads_failed(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    cid = "j9-error"
    rig.piece9_merged(cid)
    rig.refusing.add("lab-b")
    at = MERGED_AT + timedelta(minutes=10)
    result = rig.tick(at)
    assert result.status == "partial"
    assert rig.live == {"lab-a": rig.merged, "lab-b": rig.parent}

    judged_at = MERGED_AT + HOUR + timedelta(seconds=1)
    joined = rig.join(cid, judged_at).handle(
        rig.request(cid, judged_at, _readings(result))
    )
    assert joined.outcome is EnumJoinJ9Outcome.FAILED
    assert joined.installed == ("lab-a",)
    assert [(f.host, f.code) for f in joined.failures] == [
        ("lab-b", POLICY.refused_failure_code)
    ]
    assert "installer-exit-1" in joined.failures[0].detail
    assert joined.failure_code == POLICY.refused_failure_code
    piece10 = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in piece10] == ["start", "failure"]
    assert piece10[-1].failure_code == POLICY.refused_failure_code
    assert "lab-b" in (piece10[-1].failure_detail or "")
    # The floor reads a join's error side from a typed upstream refusal (piece 9 failed, piece 10 never started).
    # Here piece 9 succeeded and piece 10 started and failed: the floor does not read it golden and names no
    # broken-chain finding. The failed reading is the join handler's typed result and piece 10's failure emission.
    state, findings = _fold(rig.sink.events, judged_at + timedelta(minutes=5))
    assert state == "not_run"
    assert findings == []


def test_error_a_host_with_no_receipt_after_the_merge_is_named_late(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    at = MERGED_AT + timedelta(minutes=10)
    result = rig.tick(at)
    only_a = [r for r in _readings(result) if r.receipt.host == "lab-a"]
    judged_at = MERGED_AT + HOUR + timedelta(minutes=1)
    joined = rig.join("j9-silent", judged_at).handle(
        rig.request("j9-silent", judged_at, only_a)
    )
    assert joined.outcome is EnumJoinJ9Outcome.FAILED
    assert [(f.host, f.code) for f in joined.failures] == [
        ("lab-b", POLICY.late_failure_code)
    ]


def test_error_an_install_after_the_hour_is_late_even_though_it_reads_back_equal(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    at = MERGED_AT + HOUR + timedelta(minutes=5)
    result = rig.tick(at)
    assert all(r.installed == rig.merged for r in result.targets)
    joined = rig.join("j9-late", at).handle(
        rig.request("j9-late", at, _readings(result))
    )
    assert joined.outcome is EnumJoinJ9Outcome.FAILED
    assert {f.code for f in joined.failures} == {POLICY.late_failure_code}
    assert {f.host for f in joined.failures} == set(HOSTS)


def test_error_a_readback_that_differs_from_the_merged_head_is_named_a_mismatch(
    tmp_path: Path,
) -> None:
    rig = _Rig(tmp_path)
    at = MERGED_AT + timedelta(minutes=10)
    result = rig.tick(at)
    other_head = rig.parent
    judged_at = MERGED_AT + HOUR + timedelta(seconds=1)
    joined = rig.join("j9-mismatch", judged_at).handle(
        rig.request("j9-mismatch", judged_at, _readings(result), merged_head=other_head)
    )
    assert joined.outcome is EnumJoinJ9Outcome.FAILED
    assert {f.code for f in joined.failures} == {POLICY.mismatch_failure_code}


def test_error_a_receipt_from_before_the_merge_does_not_count(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    at = MERGED_AT + timedelta(minutes=10)
    result = rig.tick(at)
    stale = [
        ModelJoinJ9Reading(at=MERGED_AT - timedelta(minutes=1), receipt=r.receipt)
        for r in _readings(result)
    ]
    judged_at = MERGED_AT + HOUR + timedelta(seconds=1)
    joined = rig.join("j9-stale", judged_at).handle(
        rig.request("j9-stale", judged_at, stale)
    )
    assert joined.outcome is EnumJoinJ9Outcome.FAILED
    assert {f.code for f in joined.failures} == {POLICY.late_failure_code}
