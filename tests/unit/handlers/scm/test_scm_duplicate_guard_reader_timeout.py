# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex scm pr-create``'s duplicate guard answers from a bounded claims read when the dev broker's replies are late (OMN-26433).

On 2026-11-22 lab lanes on h053 and h201 were refused ``PR-DUPLICATE-GUARD unchecked``: the ledger reader answered the guard's
168 hour, every-row-type read with exit 69 ``bus-session-failed-timeouterror``, and a pr-create waited 200 to 300 seconds with no
output. The seam-deadline probe on h053 measured the work-ledger query's replies at 55 to 137 seconds (deadline 30 seconds), or
none, for each page. A read that needs many pages cannot answer inside any bound; a read that needs one or two can.

The tests replay the reader subprocess (``subprocess.run``) over a broker whose page latency is the recorded one: a read takes
pages x latency, and is a timeout when that is more than the wait the guard gave it. The ledger it serves holds 84,000 filler
rows in the 168 hour window, as the 2026-11-21 records did, so the guard's whole-window read needs many pages and its CLAIM,
TERMINAL and RELEASE read and its one hour heartbeat read need one each.

The guard is never weakened: the narrowed reads must give the same live claims as the whole-window read (a lane with an old CLAIM
and a recent row of any type still holds it), and when no read answers the guard refuses ``unchecked`` and opens nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.adapter_work_ledger_rows_reader import (
    INTERPRETER_ENV,
    SubprocessLedgerRowsReader,
)
from omnimarket.handlers.scm import scm_cli
from omnimarket.handlers.scm.scm_duplicate_guard import EXIT_CLEAR, EXIT_UNCHECKED
from omnimarket.nodes.node_pr_duplicate_snapshot_effect.handlers import (
    handler_pr_duplicate_snapshot as snapshot_module,
)
from tests.unit.handlers.scm.test_scm_duplicate_guard import (
    CAUSE,
    CREATE_ARGS,
    LANE,
    World,
    _row,
    invoke,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "work_ledger_reader"
    / "pr_duplicate_guard_claims_2026_11_22.json"
)
RECORDED: dict[str, Any] = json.loads(FIXTURE.read_text())
RECORDS: list[dict[str, Any]] = RECORDED["recorded"]
CONTRACT = (
    Path(snapshot_module.__file__).resolve().parents[1] / "contract.yaml"
)  # the guard's declared bounds
INTERPRETER = "/lab/venv/bin/python"
#: Rows one reply page carries, and the filler rows (STATUS, spread evenly) of the 168 hour window.
PAGE_ROWS = 5000
WINDOW_FILLER_ROWS = 84_000
#: The recorded reply latency of one page, the seam probe's middle answer in seconds.
SEAM_REPLIES_S = sorted(
    ms / 1000 for ms in RECORDED["seam_probe"]["replies_ms"] if ms is not None
)
PAGE_LATENCY_S = SEAM_REPLIES_S[len(SEAM_REPLIES_S) // 2]
HOSTS = ("h053", "h201", "h202")


def record(host: str) -> dict[str, Any]:
    return next(r for r in RECORDS if r["host"] == host)


def stamp_of(row: str) -> datetime:
    return datetime.fromisoformat(row.split(" | ")[0].replace("Z", "+00:00"))


class Broker:
    """``subprocess.run`` for the reader module over the dev broker with the recorded page latency.

    A read of N matching rows takes ``ceil(N / PAGE_ROWS)`` pages of ``PAGE_LATENCY_S`` each. When that is more than the wait the
    caller passed, the caller's own timeout fires (``subprocess.TimeoutExpired``); otherwise the reader answers with its rows.
    ``refuse_all`` answers every read with the host's recorded exit 69 instead, ``refuse_kinds`` only a read that names row types.
    """

    def __init__(
        self,
        rows: Sequence[str],
        host: str,
        *,
        refuse_all: bool = False,
        refuse_kinds: bool = False,
        filler: int = WINDOW_FILLER_ROWS,
    ) -> None:
        self.rows = list(rows)
        self.recorded = record(host)
        self.refuse_all = refuse_all
        self.refuse_kinds = refuse_kinds
        self.filler = filler
        self.argvs: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.argvs.append(argv)
        timeout = float(kwargs["timeout"])
        self.timeouts.append(timeout)
        hours = int(argv[argv.index("--window-hours") + 1])
        kinds = [argv[i + 1] for i, word in enumerate(argv) if word == "--kind"]
        if self.refuse_all or (self.refuse_kinds and kinds):
            return subprocess.CompletedProcess(
                argv, self.recorded["exit"], "", self.recorded["stderr"]
            )
        now = datetime.now(UTC)
        kept = [
            row
            for row in self.rows
            if (now - stamp_of(row)).total_seconds() <= hours * 3600
            and (not kinds or row.split(" | ")[1] in kinds)
        ]
        matched = len(kept) + (
            int(self.filler * hours / 168) if not kinds or "STATUS" in kinds else 0
        )
        pages = max(1, -(-matched // PAGE_ROWS))
        if pages * PAGE_LATENCY_S > timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        return subprocess.CompletedProcess(
            argv, 0, "\n".join(kept), "READER status=ok source=node"
        )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    built = World(tmp_path)
    monkeypatch.setenv("ONEX_PR_WATCHER_STATE", str(built.state))
    monkeypatch.setenv("ONEX_SCM_CLONE_ROOT", str(built.root))
    built.lane_branch("src/metrics.py", 15)
    monkeypatch.chdir(built.lane)
    return built


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []

    def fake(*args: object, **kwargs: object) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr(scm_cli, "_call_bus", fake)
    return calls


def use_broker(monkeypatch: pytest.MonkeyPatch, broker: Broker) -> Broker:
    """The guard's production adapter, with only the subprocess replayed."""
    monkeypatch.setenv(INTERPRETER_ENV, f"{INTERPRETER} -m some.module append")
    monkeypatch.setattr(
        snapshot_module,
        "SubprocessLedgerRowsReader",
        lambda environ, **kwargs: SubprocessLedgerRowsReader(
            environ, run=broker, **kwargs
        ),
    )
    return broker


def other_lane_claim_rows() -> list[str]:
    """Another lane's CLAIM on our cause 100 hours ago with a STATUS heartbeat 10 minutes ago, and our own CLAIM."""
    return [
        _row(100 * 60, "CLAIM", "cause-core-0010", CAUSE, "pr=omnibase_core#2159"),
        _row(10, "STATUS", "cause-core-0010", "working"),
        _row(5, "CLAIM", LANE, CAUSE, "mine"),
    ]


def ledger_read_contract() -> dict[str, Any] | None:
    config = yaml.safe_load(CONTRACT.read_text(encoding="utf-8")).get("config", {})
    declared = config.get("ledger_read")
    return declared if isinstance(declared, dict) else None


@pytest.mark.parametrize("host", HOSTS)
def test_recorded_2026_11_22_the_whole_window_read_that_timed_out_no_longer_refuses_pr_create(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    sent: list[object],
    host: str,
) -> None:
    """The recorded state: the 168 hour every-row read cannot finish, so the guard must read the claims another way."""
    recorded = record(host)
    assert recorded["exit"] == 69
    assert "bus-session-failed-timeouterror" in recorded["stderr"]
    broker = use_broker(monkeypatch, Broker(other_lane_claim_rows(), host))

    result = invoke(world.check_args())

    assert result.exit_code == scm_cli.EXIT_DUPLICATE, (
        f"{host}: exit {result.exit_code}; {result.stderr}; reads {broker.argvs}"
    )
    answer = json.loads(result.stdout)
    assert answer["adopt_lane"] == "cause-core-0010"
    assert sent == []


@pytest.mark.parametrize("host", HOSTS)
def test_recorded_2026_11_22_pr_create_goes_on_when_the_bounded_read_finds_no_other_claim(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    sent: list[object],
    capsys: pytest.CaptureFixture[str],
    host: str,
) -> None:
    """Printed ``clear`` follows rows that were read: our own CLAIM is in them, and ``pr-create`` reaches the bus call."""
    broker = use_broker(
        monkeypatch, Broker([_row(5, "CLAIM", LANE, CAUSE, "mine")], host)
    )

    with pytest.raises(SystemExit) as stopped:
        scm_cli.scm_group.main(CREATE_ARGS, standalone_mode=False)

    err = capsys.readouterr().err
    assert stopped.value.code == 0, err
    assert "PR-DUPLICATE-GUARD clear" in err
    assert "unchecked" not in err
    assert len(sent) == 1
    assert broker.argvs, "clear was printed with no claims read"


def test_the_narrowed_read_holds_the_same_live_claims_as_the_whole_window(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: no weakening. An old CLAIM of a lane quiet for three hours is not live, a lane with a recent row of any type is."""
    quiet = [
        _row(100 * 60, "CLAIM", "cause-core-0010", CAUSE, "pr=omnibase_core#2159"),
        _row(180, "STATUS", "cause-core-0010", "last word"),
        _row(5, "CLAIM", LANE, CAUSE, "mine"),
    ]
    use_broker(monkeypatch, Broker(quiet, "h053", filler=0))

    result = invoke(world.check_args())

    assert result.exit_code == EXIT_CLEAR, result.output


def test_the_whole_window_read_still_finds_the_claim_when_it_is_the_only_one_that_answers(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback shape: a node that refuses a read naming row types is read whole, and the claim is found."""
    broker = use_broker(
        monkeypatch,
        Broker(other_lane_claim_rows(), "h201", refuse_kinds=True, filler=2000),
    )

    result = invoke(world.check_args())

    assert result.exit_code == scm_cli.EXIT_DUPLICATE, (
        f"{result.stderr}; reads {broker.argvs}"
    )
    assert json.loads(result.stdout)["adopt_lane"] == "cause-core-0010"


@pytest.mark.parametrize("host", HOSTS)
def test_never_clear_unread_when_every_read_exits_69(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    sent: list[object],
    capsys: pytest.CaptureFixture[str],
    host: str,
) -> None:
    """With the primary and the fallback both unreadable the guard refuses ``unchecked``, after trying, and opens nothing."""
    broker = use_broker(monkeypatch, Broker([], host, refuse_all=True))

    with pytest.raises(SystemExit) as stopped:
        scm_cli.scm_group.main(CREATE_ARGS, standalone_mode=False)

    err = capsys.readouterr().err
    assert stopped.value.code == scm_cli.EXIT_LOCAL_UNANSWERED, err
    assert "PR-DUPLICATE-GUARD unchecked, no pull request opened" in err
    assert "PR-DUPLICATE-GUARD clear" not in err
    assert broker.argvs, "the guard refused without trying to read the claims"
    assert sent == []


def test_never_clear_unread_when_no_read_answers_inside_its_bound(
    world: World, monkeypatch: pytest.MonkeyPatch, sent: list[object]
) -> None:
    """A broker too slow for even one page: every read hits its own bound and the guard refuses, never clear."""
    use_broker(monkeypatch, Broker([_row(5, "CLAIM", LANE, CAUSE, "mine")], "h053"))
    # One page takes longer than any declared bound.
    monkeypatch.setattr(sys.modules[__name__], "PAGE_LATENCY_S", 10_000.0)

    result = invoke(world.check_args())

    assert result.exit_code == EXIT_UNCHECKED, result.output
    assert json.loads(result.stdout)["state"] == "unchecked"
    assert "timed out" in result.stdout
    assert sent == []


def test_the_bounds_of_the_claims_read_are_declared_in_the_node_contract_and_used(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: the wait of every read the guard makes is a value of the node contract, and the two tiers fit its total."""
    declared = ledger_read_contract()
    assert declared is not None, (
        "the guard node contract declares no config.ledger_read"
    )
    broker = use_broker(monkeypatch, Broker(other_lane_claim_rows(), "h053"))
    invoke(world.check_args())
    assert broker.timeouts
    primary = declared["primary_timeout_s"]
    assert set(broker.timeouts) <= {primary, declared["fallback_timeout_s"]}
    assert primary + declared["fallback_timeout_s"] <= declared["total_budget_s"]
    assert declared["heartbeat_window_hours"] * 3600 >= 0.75 * 3600, (
        "the heartbeat read must cover a CLAIM's live hours"
    )


def test_the_declared_claim_row_types_hold_every_row_that_opens_or_closes_a_claim() -> (
    None
):
    declared = ledger_read_contract()
    assert declared is not None, (
        "the guard node contract declares no config.ledger_read"
    )
    assert {"CLAIM", "TERMINAL", "RELEASE"} <= set(declared["claim_kinds"])
    assert declared["reader"] == "pr-duplicate-guard-claims"
    assert declared["window_hours"] == 168
