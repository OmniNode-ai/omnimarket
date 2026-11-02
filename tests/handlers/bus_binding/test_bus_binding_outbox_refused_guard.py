# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ledger guard's refusal of a replayed append is read, settled in code or left open once (OMN-21959).

The six recorded cases are queue rows of class ``bus-binding-outbox-refused`` whose refusal came from a ledger
guard (the friction guard, OMN-18274, or the worktree cell guard, OMN-20148), not from a request-id clash. The
replay scenario puts them in a real queue and runs the actor over it twice: against the ledger as read on
2026-11-02, and against a ledger that has since made each cause good. The refused request bodies the second run
reads are built for the scenario from the evidence; they are not recorded bodies.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.handlers.bus_binding.guard_refusal import (
    GuardPorts,
    read_guard_refusal,
    settle_guard_case,
    settle_guard_refusals,
)
from omnimarket.handlers.bus_binding.recovery import REFUSED_CLASS, SOURCE
from omnimarket.models.bus_binding.model_guard_refusal import EnumGuardRefusal
from omnimarket.nodes.node_exception_queue_effect.container.container_exception_queue_effect import (
    create_handler_exception_emit,
    create_handler_exception_queue,
)
from omnimarket.nodes.node_exception_queue_effect.models import (
    ModelExceptionEmitRequest,
    ModelExceptionQueueRequest,
    ModelExceptionRow,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "outbox_refused_guard_rows.json"
NOW = datetime(2026, 11, 2, 4, 30, tzinfo=UTC)
CASES: list[dict[str, str]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
CLAIMS: dict[str, str] = json.loads(FIXTURE.read_text(encoding="utf-8"))[
    "ledger_claims_read_2026-11-02T04:18Z"
]
KEPT_WORKTREES: dict[str, str] = json.loads(FIXTURE.read_text(encoding="utf-8"))[
    "kept_mismatch_worktrees"
]


def _case(suffix: str) -> dict[str, str]:
    return next(c for c in CASES if c["id"].endswith(suffix))


def _ports(
    *,
    ledger: dict[str, list[str]],
    bodies: dict[str, str] | None = None,
    gone: frozenset[str] = frozenset(),
    appended: list[str] | None = None,
    accepts: bool = True,
) -> GuardPorts:
    sink = appended if appended is not None else []

    def append(rows: str) -> bool:
        sink.append(rows)
        return accepts

    return GuardPorts(
        lane_rows=lambda lane: ledger.get(lane, []),
        refused_rows=lambda path: (bodies or {}).get(path),
        path_exists=lambda path: path not in gone,
        append=append,
    )


def _queue_with(state_dir: Path, cases: list[dict[str, str]]) -> None:
    for case in cases:
        create_handler_exception_emit().handle(
            ModelExceptionEmitRequest(
                state_dir=state_dir / "exception-queue",
                row=ModelExceptionRow(
                    cls=REFUSED_CLASS,
                    key=case["key"],
                    source=case["source"],
                    evidence=case["evidence"],
                    tried=case["tried"],
                    proposed_fix=case["proposed_fix"],
                ),
                now="2026-11-01T20:47:49Z",
            )
        )


def _open_keys(state_dir: Path) -> list[str]:
    result = create_handler_exception_queue().handle(
        ModelExceptionQueueRequest(state_dir / "exception-queue", "orchestrator")
    )
    return [row["key"] for row in result.rows if row["cls"] == REFUSED_CLASS]


def test_bus_binding_outbox_refused_guard_reads_each_recorded_refusal() -> None:
    expected = {
        "1e63c013": (
            EnumGuardRefusal.FRICTION_GUARD,
            "codex-integration-pass-20261028T052803Z",
            "",
        ),
        "5b687c90": (
            EnumGuardRefusal.FRICTION_GUARD,
            "fallback-open-prs-0200",
            "2026-10-27T02:09:23Z",
        ),
        "90283905": (
            EnumGuardRefusal.WORKTREE_REMOVED_EXISTS,
            "push-broker-memory-5396-0420",
            "2026-10-27T04:21:00Z",
        ),
        "b8ffe77d": (
            EnumGuardRefusal.FRICTION_GUARD,
            "codex-integration-pass-61",
            "2026-10-27T05:34:45Z",
        ),
        "17a09c33": (
            EnumGuardRefusal.WORKTREE_KEPT_MISMATCH,
            "controller-rebase-install-1150",
            "2026-10-27T11:51:50Z",
        ),
        "c4300b3a": (
            EnumGuardRefusal.FRICTION_GUARD,
            "lean-skills-mac-1810",
            "2026-10-27T16:19:33Z",
        ),
        "e8b4fe65": (
            EnumGuardRefusal.FRICTION_GUARD,
            "codex-integration-pass-20261028T052803Z",
            "2026-10-28T05:29:08Z",
        ),
    }
    for suffix, (guard, lane, stamp) in expected.items():
        refusal = read_guard_refusal(_case(suffix))
        assert refusal is not None, suffix
        assert (refusal.guard, refusal.lane, refusal.lost_stamp) == (guard, lane, stamp)


def test_bus_binding_outbox_refused_guard_ignores_a_request_id_clash() -> None:
    clash = {
        "key": "lane=dev request=cf16d896-d11f-4cef-9601-2cb5cda111fb",
        "evidence": "ledger host h200 refused the queued append (exit_code=65): request id cf16d896 already belongs to different rows",
    }
    assert read_guard_refusal(clash) is None


def test_bus_binding_outbox_refused_guard_replay_as_recorded_leaves_all_open(
    tmp_path: Path,
) -> None:
    """The refused files sit on the writer's machine and every lane holds a CLAIM and no TERMINAL: nothing is
    resolvable, so each case stays open once and nothing is appended."""
    _queue_with(tmp_path, CASES)
    appended: list[str] = []
    ledger = {lane: [claim] for lane, claim in CLAIMS.items()}
    results = settle_guard_refusals(
        tmp_path, _ports(ledger=ledger, appended=appended), now=NOW
    )
    assert [r.resolved for r in results] == [False] * 7
    assert appended == []
    assert sorted(_open_keys(tmp_path)) == sorted(c["key"] for c in CASES)
    assert all(r.emitted.startswith("repeat:") for r in results)


def _bodies() -> dict[str, str]:
    def path_of(suffix: str) -> str:
        return _case(suffix)["tried"].split("refused request at ", 1)[1]

    return {
        path_of(
            "5b687c90"
        ): "2026-10-27T02:09:23Z | TERMINAL | lane=fallback-open-prs-0200 | ticket=OMN-20275 | friction=OMN-20275 | closes-CLAIM=2026-10-27T02:08:32Z | outcome=handed-off",
        path_of(
            "90283905"
        ): "2026-10-27T04:21:00Z | TERMINAL | lane=push-broker-memory-5396-0420 | ticket=OMN-20275 | friction=none | worktree=removed:omni_worktrees/OMN-20275-push5396/omnibase_infra | closes-CLAIM=2026-10-27T04:20:14Z | outcome=done",
        path_of(
            "b8ffe77d"
        ): "2026-10-27T05:34:45Z | TERMINAL | lane=codex-integration-pass-61 | ticket=OMN-17427 | friction=ledger-broker-dns-unavailable | closes-CLAIM=2026-10-27T05:33:10Z | outcome=done",
        path_of(
            "17a09c33"
        ): f"2026-10-27T11:51:50Z | TERMINAL | lane=controller-rebase-install-1150 | ticket=OMN-19998 | friction=none | worktree=kept:{KEPT_WORKTREES['refused']}:pushed | closes-CLAIM=2026-10-27T11:46:43Z | outcome=done",
        path_of(
            "c4300b3a"
        ): "2026-10-27T16:19:33Z | TERMINAL | lane=lean-skills-mac-1810 | ticket=OMN-21433 | friction=omniclaude has no prose-only skill registration | closes-CLAIM=2026-10-27T16:11:10Z | outcome=done",
    }


def test_bus_binding_outbox_refused_guard_replay_after_the_causes_are_made_good(
    tmp_path: Path,
) -> None:
    _queue_with(tmp_path, CASES)
    appended: list[str] = []
    ledger = {lane: [claim] for lane, claim in CLAIMS.items()}
    # fallback-open-prs-0200: a FRICTION row for the lane has since been written
    ledger["fallback-open-prs-0200"].append(
        "2026-10-28T09:00:00Z | FRICTION | lane=fallback-open-prs-0200 | existing=OMN-20275 | cost=~10 lane-minutes | dns"
    )
    # codex-integration-pass-61: the lane's TERMINAL was written again by hand
    ledger["codex-integration-pass-61"].append(
        "2026-10-27T05:40:00Z | TERMINAL | lane=codex-integration-pass-61 | closes-CLAIM=2026-10-27T05:33:10Z | friction=none"
    )
    results = settle_guard_refusals(
        tmp_path,
        _ports(
            ledger=ledger,
            bodies=_bodies(),
            gone=frozenset({"omni_worktrees/OMN-20275-push5396/omnibase_infra"}),
            appended=appended,
        ),
        now=NOW,
    )
    by_lane = {r.lane: r for r in results}
    assert by_lane["fallback-open-prs-0200"].resolved
    assert by_lane["push-broker-memory-5396-0420"].resolved
    assert by_lane["codex-integration-pass-61"].resolved
    assert "TERMINAL" in by_lane["codex-integration-pass-61"].how
    # re-appended unchanged: the friction case and the removed worktree case; the superseded one appends nothing
    assert len(appended) == 2
    assert "friction=OMN-20275" in appended[0]
    assert (
        "worktree=removed:omni_worktrees/OMN-20275-push5396/omnibase_infra"
        in appended[1]
    )
    # what code cannot decide stays open, with what is missing named
    kept = by_lane["controller-rebase-install-1150"]
    assert not kept.resolved
    assert KEPT_WORKTREES["claimed"] in kept.how
    assert KEPT_WORKTREES["refused"] in kept.how
    assert "cost" in by_lane["lean-skills-mac-1810"].how
    assert "no CLAIM" in by_lane["codex-integration-pass-20261028T052803Z"].how
    remaining = sorted(_open_keys(tmp_path))
    assert remaining == sorted(
        c["key"]
        for c in CASES
        if any(
            lane in c["key"]
            for lane in (
                "controller-rebase-install-1150",
                "lean-skills-mac-1810",
                "codex-integration-pass-20261028T052803Z",
                "lane=dev",
            )
        )
    )


def test_bus_binding_outbox_refused_guard_a_refused_reappend_stays_open(
    tmp_path: Path,
) -> None:
    case = _case("90283905")
    _queue_with(tmp_path, [case])
    ledger = {"push-broker-memory-5396-0420": [CLAIMS["push-broker-memory-5396-0420"]]}
    results = settle_guard_refusals(
        tmp_path,
        _ports(
            ledger=ledger,
            bodies=_bodies(),
            gone=frozenset({"omni_worktrees/OMN-20275-push5396/omnibase_infra"}),
            accepts=False,
        ),
        now=NOW,
    )
    assert [r.resolved for r in results] == [False]
    assert "refused" in results[0].how
    assert _open_keys(tmp_path) == [case["key"]]


def test_bus_binding_outbox_refused_guard_never_resolves_a_decision_row(
    tmp_path: Path,
) -> None:
    case = _case("5b687c90")
    create_handler_exception_emit().handle(
        ModelExceptionEmitRequest(
            state_dir=tmp_path / "exception-queue",
            row=ModelExceptionRow(
                cls=REFUSED_CLASS,
                key=case["key"],
                source=SOURCE,
                evidence=case["evidence"],
                tried=case["tried"],
                proposed_fix=case["proposed_fix"],
                decision=True,
            ),
            now="2026-11-01T20:47:49Z",
        )
    )
    ledger = {
        "fallback-open-prs-0200": [
            CLAIMS["fallback-open-prs-0200"],
            "2026-10-28T09:00:00Z | FRICTION | lane=fallback-open-prs-0200 | existing=OMN-20275 | cost=~10 lane-minutes | dns",
        ]
    }
    appended: list[str] = []
    ports = _ports(ledger=ledger, bodies=_bodies(), appended=appended)
    # the orchestrator's pass never sees a decision row; handed one directly, the actor leaves it to the operator
    assert settle_guard_refusals(tmp_path, ports, now=NOW) == []
    operator_rows = (
        create_handler_exception_queue()
        .handle(ModelExceptionQueueRequest(tmp_path / "exception-queue", "operator"))
        .rows
    )
    assert [r["key"] for r in operator_rows] == [case["key"]]
    direct = settle_guard_case(tmp_path, operator_rows[0], ports, now=NOW)
    assert direct is not None
    assert not direct.resolved
    assert "decision" in direct.how
    assert appended == []
    still_open = (
        create_handler_exception_queue()
        .handle(ModelExceptionQueueRequest(tmp_path / "exception-queue", "operator"))
        .rows
    )
    assert [r["key"] for r in still_open] == [case["key"]]
