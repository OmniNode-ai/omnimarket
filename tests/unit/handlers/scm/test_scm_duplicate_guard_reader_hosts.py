# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex scm pr-create``'s duplicate guard reads its claims through the ledger reader node, on every lab host (OMN-26387).

On 2026-11-21 the reader answered ``pr-duplicate-guard-claims`` with exit 3 (``reader-not-in-moved-readers``) on h051 and exit 1
(``NodeOverlayNotFoundError``, no reader overlay root) on h105, so ``onex scm pr-create`` printed ``PR-DUPLICATE-GUARD
unchecked`` and no lab lane could open a pull request. The fixture holds the reader's own recorded answers on both hosts, before
the overlay named the reader and the h105 overlays clone moved under the registry root, and after.

The guard runs here through its production adapter (``SubprocessLedgerRowsReader``) with the subprocess replayed from those
records; every file the guard opens while it runs is recorded, and none may be a ledger file.
"""

from __future__ import annotations

import builtins
import io
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.adapter_work_ledger_rows_reader import (
    INTERPRETER_ENV,
    READER_MODULE,
    SubprocessLedgerRowsReader,
)
from omnimarket.handlers.scm import scm_cli
from omnimarket.nodes.node_pr_duplicate_snapshot_effect.handlers import (
    handler_pr_duplicate_snapshot as snapshot_module,
)
from omnimarket.nodes.node_pr_duplicate_snapshot_effect.handlers.ledger_claims_read import (
    load_ledger_read_config,
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


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    built = World(tmp_path)
    monkeypatch.setenv("ONEX_PR_WATCHER_STATE", str(built.state))
    monkeypatch.setenv("ONEX_SCM_CLONE_ROOT", str(built.root))
    return built


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []

    def fake(*args: object, **kwargs: object) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr(scm_cli, "_call_bus", fake)
    return calls


FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "work_ledger_reader"
    / "pr_duplicate_guard_claims_2026_11_21.json"
)
RECORDS: list[dict[str, Any]] = json.loads(FIXTURE.read_text())["recorded"]
LAB_HOSTS = ("h051", "h105")
INTERPRETER = "/lab/venv/bin/python"


def record(host: str, state: str) -> dict[str, Any]:
    return next(r for r in RECORDS if r["host"] == host and r["state"] == state)


class ReplayedReader:
    """``subprocess.run`` for the reader module: answers with a record's exit and stderr, and keeps every argv."""

    def __init__(self, recorded: dict[str, Any], stdout: Sequence[str] = ()) -> None:
        self.recorded = recorded
        self.stdout = "\n".join(stdout)
        self.argvs: list[list[str]] = []
        self.timeouts: list[object] = []

    def __call__(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.argvs.append(argv)
        self.timeouts.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(
            argv, self.recorded["exit"], self.stdout, self.recorded["stderr"]
        )


@pytest.fixture
def opened_files(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every path the guard opens through ``open`` or ``io.open`` (pathlib's reads) while a test runs."""
    seen: list[str] = []
    real_open = builtins.open

    def recording_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", recording_open)
    monkeypatch.setattr(io, "open", recording_open)
    return seen


def assert_no_ledger_file_opened(opened: Sequence[str]) -> None:
    assert opened, "the recorder saw no file open at all"
    assert [path for path in opened if "ledger" in path.lower()] == []


def use_reader(
    monkeypatch: pytest.MonkeyPatch, replay: ReplayedReader
) -> ReplayedReader:
    """The guard's production adapter, with only the subprocess replayed."""
    monkeypatch.setenv(INTERPRETER_ENV, f"{INTERPRETER} -m some.module append")
    monkeypatch.setattr(
        snapshot_module,
        "SubprocessLedgerRowsReader",
        lambda environ, **kwargs: SubprocessLedgerRowsReader(
            environ, run=replay, **kwargs
        ),
    )
    return replay


def claim_rows() -> list[str]:
    """What the reader prints after the fix: another lane's live claim on the same cause, and the lane's own claim."""
    return [
        _row(5, "CLAIM", "cause-core-0010", CAUSE, "pr=omnibase_core#2159", "fix"),
        _row(5, "CLAIM", LANE, CAUSE, "mine"),
    ]


@pytest.mark.parametrize("host", LAB_HOSTS)
def test_the_guard_reads_its_claims_through_the_reader_node_and_never_the_ledger_file(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    opened_files: list[str],
    host: str,
) -> None:
    """The refusal names the lane the reader's rows hold, and no ledger file is opened."""
    world.lane_branch("src/metrics.py", 15)
    replay = use_reader(
        monkeypatch, ReplayedReader(record(host, "after-fix"), claim_rows())
    )

    result = invoke(world.check_args())

    assert result.exit_code == scm_cli.EXIT_DUPLICATE, result.output
    answer = json.loads(result.stdout)
    assert answer["adopt_lane"] == "cause-core-0010"
    assert_no_ledger_file_opened(opened_files)
    declared = load_ledger_read_config()
    head = [INTERPRETER, "-m", READER_MODULE, "--reader", snapshot_module.LEDGER_READER]
    claim_kinds = [word for kind in declared.claim_kinds for word in ("--kind", kind)]
    assert sorted(replay.argvs) == sorted(
        [
            [
                *head,
                "--query",
                "rows",
                "--window-hours",
                str(snapshot_module.LEDGER_WINDOW_HOURS),
                *claim_kinds,
            ],
            [
                *head,
                "--query",
                "rows",
                "--window-hours",
                str(declared.heartbeat_window_hours),
            ],
        ]
    )
    assert snapshot_module.LEDGER_READER == "pr-duplicate-guard-claims"


@pytest.mark.parametrize("host", LAB_HOSTS)
def test_a_reader_that_answers_with_no_other_claim_lets_pr_create_go_on(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    opened_files: list[str],
    sent: list[object],
    capsys: pytest.CaptureFixture[str],
    host: str,
) -> None:
    """After the fix the guard is clear and ``pr-create`` reaches the bus call, on both lab hosts."""
    world.lane_branch("src/metrics.py", 15)
    use_reader(
        monkeypatch,
        ReplayedReader(record(host, "after-fix"), [_row(5, "CLAIM", LANE, "mine")]),
    )
    monkeypatch.chdir(world.lane)

    with pytest.raises(SystemExit) as stopped:
        scm_cli.scm_group.main(CREATE_ARGS, standalone_mode=False)

    err = capsys.readouterr().err
    assert stopped.value.code == 0
    assert "PR-DUPLICATE-GUARD clear" in err
    assert "unchecked" not in err
    assert len(sent) == 1


@pytest.mark.parametrize("host", LAB_HOSTS)
def test_the_recorded_before_fix_answer_is_the_state_the_lab_hosts_must_not_be_in(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    opened_files: list[str],
    sent: list[object],
    capsys: pytest.CaptureFixture[str],
    host: str,
) -> None:
    """Regression: each host's newest recorded answer is a fresh one, and ``pr-create`` goes on through it.

    On the first recorded answers (exit 3 on h051, exit 1 on h105) ``pr-create`` opened nothing and no ledger file was opened.
    """
    world.lane_branch("src/metrics.py", 15)
    newest = [r for r in RECORDS if r["host"] == host][-1]
    use_reader(monkeypatch, ReplayedReader(newest, [_row(5, "CLAIM", LANE, "mine")]))
    monkeypatch.chdir(world.lane)

    assert newest["exit"] == 0, f"{host}: the newest recorded reader answer is {newest}"
    with pytest.raises(SystemExit) as stopped:
        scm_cli.scm_group.main(CREATE_ARGS, standalone_mode=False)

    err = capsys.readouterr().err
    assert stopped.value.code == 0, err
    assert len(sent) == 1
    assert_no_ledger_file_opened(opened_files)


@pytest.mark.parametrize(
    ("host", "tell"),
    [
        ("h051", "reader-not-in-moved-readers"),
        ("h105", "ONEX_LEDGER_READER_OVERLAY"),
    ],
)
def test_the_answers_of_2026_11_21_leave_the_guard_unchecked_and_the_file_unread(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    opened_files: list[str],
    sent: list[object],
    host: str,
    tell: str,
) -> None:
    """Exit 3 (not moved) and exit 1 (no overlay) are refusals with the reader's own words, never a read of the ledger file."""
    world.lane_branch("src/metrics.py", 15)
    use_reader(monkeypatch, ReplayedReader(record(host, "before-fix"), []))
    monkeypatch.chdir(world.lane)

    result = invoke(CREATE_ARGS)

    assert result.exit_code == scm_cli.EXIT_LOCAL_UNANSWERED, result.output
    assert "PR-DUPLICATE-GUARD unchecked, no pull request opened" in result.stderr
    assert tell in result.stderr
    assert_no_ledger_file_opened(opened_files)
    assert sent == []


def test_the_recorder_sees_a_ledger_file_when_one_is_opened(
    tmp_path: Path, opened_files: list[str]
) -> None:
    """Positive control: the check fails when a ledger file is read, so a pass above means none was."""
    path = tmp_path / "work_ledger_rows.md"
    path.write_text("row\n")

    path.read_text()

    with pytest.raises(AssertionError):
        assert_no_ledger_file_opened(opened_files)


def test_the_guard_waits_for_the_reader_as_long_as_a_loaded_host_needed(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    opened_files: list[str],
) -> None:
    """The 168 hour whole-window read took 253 seconds on h051 under load: the fallback's declared wait is longer, the primary's is not (OMN-26433)."""
    world.lane_branch("src/metrics.py", 15)
    loaded = record("h051", "after-fix-loaded")
    replay = use_reader(
        monkeypatch, ReplayedReader(loaded, [_row(5, "CLAIM", LANE, "mine")])
    )

    invoke(world.check_args())

    declared = load_ledger_read_config()
    assert loaded["elapsed_s"] > 120
    assert declared.fallback_timeout_s > loaded["elapsed_s"]
    assert replay.timeouts == [declared.primary_timeout_s] * 2
