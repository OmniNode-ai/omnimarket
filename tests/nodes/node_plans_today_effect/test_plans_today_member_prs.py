# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""plans-today in a member's session: the member's pull request source.

A scoped ticket's pull requests are its Linear attachments plus the member's ``pr=`` cells, read with
one ``gh pr view`` each under the member's own login, at most 30, refusing on a failed read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.nodes.node_plans_today_effect.conftest import (
    URL,
    body,
    by_title,
    gh_for_member,
    gh_pr,
    invoke,
    issue,
    member_fixture,
    tasks_of,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- (b), (d) PR source


def test_scoped_prs_are_the_union_of_linear_attachments_and_the_members_pr_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = member_fixture(tmp_path, monkeypatch)
    fake = gh_for_member(monkeypatch)
    code, doc = invoke(fx, watcher=False)
    assert code == 0
    assert doc is not None
    read = sorted(c[3] for c in fake.pr_views)
    assert read == ["11", "12"], (
        "one gh pr view per scoped PR; the other member's #99 unread"
    )
    assert all(
        c[c.index("--repo") + 1] == "OmniNode-ai/omnimarket" for c in fake.pr_views
    )
    assert ["gh", "api", "user", "--jq", ".login"] in fake.calls
    assert not any("search" in c for c in fake.calls)
    assert doc["sources"]["pr_reads"] == 2
    assert doc["sources"]["pr_read_cap"] == 30
    assert doc["sources"]["watcher_last_tick"] == "not read (member mode)"
    tasks = tasks_of(doc, "Shared")
    assert tasks["T1"]["prs"] == ["omnimarket#11"]
    assert tasks["T3"]["prs"] == ["omnimarket#12"]


def test_scoped_ticket_whose_attachments_were_not_read_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fx = member_fixture(
        tmp_path,
        monkeypatch,
        sidecar_issues=[issue("OMN-2001", prs=None), issue("OMN-2003", prs=[])],
    )
    fake = gh_for_member(monkeypatch)
    code, doc = invoke(fx, watcher=False)
    assert code == 2
    assert doc is None
    assert "OMN-2001" in capsys.readouterr().err
    assert fake.calls == []


def test_more_than_thirty_prs_refuses_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    urls = [URL.format(repo="omnimarket", n=n) for n in range(100, 131)]
    fx = member_fixture(
        tmp_path,
        monkeypatch,
        sidecar_issues=[issue("OMN-2001", prs=urls), issue("OMN-2003", prs=[])],
        rows=[],
    )
    fake = gh_for_member(monkeypatch, prs={})
    code, doc = invoke(fx, watcher=False)
    assert code == 2
    assert doc is None
    assert "more than the 30 reads" in capsys.readouterr().err
    assert fake.calls == []


def test_thirty_prs_are_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = [URL.format(repo="omnimarket", n=n) for n in range(100, 130)]
    fx = member_fixture(
        tmp_path,
        monkeypatch,
        sidecar_issues=[issue("OMN-2001", prs=urls), issue("OMN-2003", prs=[])],
        rows=[],
    )
    fake = gh_for_member(
        monkeypatch,
        prs={f"omnimarket#{n}": gh_pr(state="MERGED") for n in range(100, 130)},
    )
    code, doc = invoke(fx, watcher=False)
    assert code == 0
    assert doc is not None
    assert len(fake.pr_views) == 30
    assert doc["sources"]["pr_reads"] == 30


def test_a_failed_read_refuses_with_the_pr_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fx = member_fixture(tmp_path, monkeypatch)
    gh_for_member(
        monkeypatch,
        prs={"omnimarket#11": gh_pr(), "omnimarket#12": gh_pr()},
        fail={"omnimarket#12"},
    )
    code, doc = invoke(fx, watcher=False)
    assert code == 2
    assert doc is None
    assert "omnimarket#12" in capsys.readouterr().err


def test_reads_run_only_under_the_members_own_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fx = member_fixture(tmp_path, monkeypatch)
    fake = gh_for_member(monkeypatch, login="fixture-member-b-gh")
    code, doc = invoke(fx, watcher=False)
    assert code == 2
    assert doc is None
    assert "another GitHub login" in capsys.readouterr().err
    assert fake.pr_views == []


# --------------------------------------------------------------------------- (e2)


def test_assigned_ticket_with_only_a_linear_attached_red_pr_is_in_progress_and_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plans = {"2026-09-28-e2.md": body("E2", [("mine", "OMN-2201")])}
    fx = member_fixture(
        tmp_path,
        monkeypatch,
        plans=plans,
        sidecar_issues=[
            issue(
                "OMN-2201",
                status="In Progress",
                prs=[URL.format(repo="omnibase_core", n=7)],
            )
        ],
        rows=[],
    )
    gh_for_member(monkeypatch, prs={"omnibase_core#7": gh_pr(red=True)})
    code, doc = invoke(fx, watcher=False)
    assert code == 0
    assert doc is not None
    plan = by_title(doc)["E2"]
    assert plan["tasks"][0]["state"] == "open-red"
    assert plan["tasks"][0]["prs"] == ["omnibase_core#7"]
    assert plan["verdict"] != "NOT-STARTED"
    assert plan["next_action_kind"] == "dispatch-fix"
