# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The window re-drive republishes autobind for members a batch window stranded (OMN-17427).

The case is the one observed on 2026-10-01. omnimarket#3154, #3155 and #3156
were marked ready between 02:23Z and 03:01Z, and their ``OCC Autobind Publisher``
runs succeeded. The emitter skipped each one with ``skip:WINDOW_IN_FLIGHT``
because window OCC#12036 (created 02:47Z, merged 03:19Z) was in flight, and
``OCC Autobind Mint Verify`` reported each one unbound. Nothing published for
them again after OCC#12036 merged, so lanes hand-authored OCC#12040 and #12039
for #3155 and #3156. Those then went red on Pre-commit.

Falsifiers:

* AC1: while the repository's window is open, no member is published.
* AC2: once no window is open, every ready, unbound, ticketed member whose head
  is past the grace period is published once, oldest PR first, in window mode
  (no ``--batch-mode`` flag). A draft, a bound member, an unticketed member and
  a fresh head are not.
* AC3: dry-run publishes nothing, and an unreadable member fails the pass.
* AC4: the live client parses the paginated PR listing and asks the
  publisher for the window path, not the per-PR path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.ci.occ_conflicted_companion_remint import (  # noqa: E402
    WINDOW_REDRIVE_GRACE,
    EnumRedriveOutcome,
    GhCli,
    MemberFacts,
    Target,
    decide_window_redrive,
    run_window_redrive_pass,
    window_companion_branch_for,
)

pytestmark = pytest.mark.unit

REPO = "OmniNode-ai/omnimarket"
OCC = "OmniNode-ai/onex_change_control"
NOW = datetime(2026, 10, 1, 3, 25, tzinfo=UTC)
OLD = NOW - timedelta(minutes=60)


def _member(
    number: int,
    *,
    title: str = "fix(OMN-20201): something",
    body: str = "",
    draft: bool = False,
    committed: datetime | None = OLD,
) -> MemberFacts:
    return MemberFacts(
        number=number,
        title=title,
        body=body,
        draft=draft,
        head_sha=f"{number:040d}",
        head_committed_at=committed,
    )


class FakeGh:
    def __init__(
        self,
        members: tuple[MemberFacts, ...],
        *,
        open_window: int | None,
        unreadable: frozenset[int] = frozenset(),
    ) -> None:
        self._members = members
        self._open_window = open_window
        self._unreadable = unreadable
        self.published: list[Target] = []
        self.window_branches: list[str] = []

    def open_members(self, *, repo: str) -> tuple[MemberFacts, ...]:
        assert repo == REPO
        # The live listing carries no commit date; the pass reads it.
        return tuple(
            MemberFacts(m.number, m.title, m.body, m.draft, m.head_sha)
            for m in self._members
        )

    def head_committed_at(self, *, repo: str, sha: str) -> datetime:
        member = next(m for m in self._members if m.head_sha == sha)
        if member.number in self._unreadable:
            raise RuntimeError("HTTP 502")
        assert member.head_committed_at is not None
        return member.head_committed_at

    def open_window_number(self, *, occ_repo: str, branch: str) -> int | None:
        assert occ_repo == OCC
        self.window_branches.append(branch)
        return self._open_window

    def publish_window_autobind(
        self, *, target: Target, member: MemberFacts, lane: str
    ) -> str:
        assert lane == "dev"
        assert member.number == target.pr_number
        self.published.append(target)
        return f"Published onex.cmd.omnimarket.occ-autobind.v1 event_id=e{target.pr_number}"


STRANDED = (
    _member(
        3156, title="fix(contracts): migrate canonical handler routing (OMN-20234)"
    ),
    _member(3154, title="feat(OMN-20233): a tool_use rubric class"),
    _member(3155, title="fix(OMN-20201): drop two dead mypy overrides"),
    _member(3153, body="Evidence-Source: OCC#12036\n", title="fix(OMN-20154): x"),
    _member(3152, draft=True, title="fix(OMN-20154): y"),
    _member(3151, title="chore: no ticket in this title"),
    _member(3160, title="fix(OMN-1): fresh", committed=NOW - timedelta(minutes=2)),
)


def _run(gh: FakeGh, *, dry_run: bool = False):  # type: ignore[no-untyped-def]
    return run_window_redrive_pass(
        gh, occ_repo=OCC, this_repo=REPO, lane="dev", dry_run=dry_run, now=NOW
    )


def test_ac1_an_open_window_holds_every_member() -> None:
    gh = FakeGh(STRANDED, open_window=12036)
    report = _run(gh)
    assert gh.published == []
    assert gh.window_branches == [window_companion_branch_for(REPO)]
    outcomes = dict(report.decisions)
    for number in (3154, 3155, 3156):
        assert outcomes[number].outcome is EnumRedriveOutcome.WINDOW_OPEN
    assert report.errors == ()


def test_ac2_after_the_window_merges_the_stranded_members_are_published() -> None:
    gh = FakeGh(STRANDED, open_window=None)
    report = _run(gh)
    assert [t.pr_number for t in gh.published] == [3154, 3155, 3156]
    assert [t.ticket for t in gh.published] == ["OMN-20233", "OMN-20201", "OMN-20234"]
    outcomes = {n: d.outcome for n, d in report.decisions}
    assert outcomes[3153] is EnumRedriveOutcome.BOUND
    assert outcomes[3152] is EnumRedriveOutcome.DRAFT
    assert outcomes[3151] is EnumRedriveOutcome.NO_TICKET
    assert outcomes[3160] is EnumRedriveOutcome.TOO_FRESH
    assert report.errors == ()


def test_ac2_the_grace_period_is_measured_from_the_head_commit() -> None:
    edge = _member(1, committed=NOW - WINDOW_REDRIVE_GRACE)
    young = _member(2, committed=NOW - WINDOW_REDRIVE_GRACE + timedelta(seconds=1))
    assert (
        decide_window_redrive(edge, repo=REPO, open_window=None, now=NOW).outcome
        is EnumRedriveOutcome.REDRIVE
    )
    assert (
        decide_window_redrive(young, repo=REPO, open_window=None, now=NOW).outcome
        is EnumRedriveOutcome.TOO_FRESH
    )


def test_ac3_dry_run_publishes_nothing() -> None:
    gh = FakeGh(STRANDED, open_window=None)
    report = _run(gh, dry_run=True)
    assert gh.published == []
    assert [
        n for n, d in report.decisions if d.outcome is EnumRedriveOutcome.REDRIVE
    ] == [3154, 3155, 3156]


def test_ac3_an_unreadable_member_fails_the_pass_and_spares_the_rest() -> None:
    gh = FakeGh(STRANDED, open_window=None, unreadable=frozenset({3155}))
    report = _run(gh)
    assert [t.pr_number for t in gh.published] == [3154, 3156]
    assert len(report.errors) == 1
    assert "#3155" in report.errors[0]


class ListingGh(GhCli):
    """The live client with ``gh`` and the publisher answered from fixed output."""

    def __init__(self, stdout: str) -> None:
        super().__init__(Path("publisher.py"))
        self._stdout = stdout
        self.argv: list[list[str]] = []

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.argv.append(args)
        return subprocess.CompletedProcess(args, 0, self._stdout, "")


def test_ac4_live_listing_is_parsed_from_one_json_object_per_line() -> None:
    rows = [
        {"number": 3154, "title": "t1", "body": None, "draft": False, "sha": "a" * 40},
        {"number": 3152, "title": "t2", "body": "b", "draft": True, "sha": "b" * 40},
    ]
    gh = ListingGh("\n".join(json.dumps(r) for r in rows) + "\n")
    members = gh.open_members(repo=REPO)
    assert members == (
        MemberFacts(3154, "t1", "", False, "a" * 40),
        MemberFacts(3152, "t2", "b", True, "b" * 40),
    )
    assert "--paginate" in gh.argv[0]


def test_ac4_window_publish_asks_for_the_default_window_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "Published x event_id=1\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("OCC_COMPANION_BATCH_MODE", "ticket")
    gh = GhCli(Path("publisher.py"))
    member = _member(3154, title="feat(OMN-20233): t")
    out = gh.publish_window_autobind(
        target=Target(REPO, 3154, "OMN-20233"), member=member, lane="dev"
    )
    assert out == "Published x event_id=1"
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert "--batch-mode" not in argv
    assert argv[-2:] == ["--lane", "dev"]
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["PR_NUMBER"] == "3154"
    assert env["PR_TICKET"] == "OMN-20233"
    assert env["PR_HEAD_SHA"] == member.head_sha
    assert "OCC_COMPANION_BATCH_MODE" not in env
