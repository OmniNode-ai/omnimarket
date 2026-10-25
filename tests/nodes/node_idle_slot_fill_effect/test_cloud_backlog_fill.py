"""Free cloud slots take small-code backlog tickets (OMN-17427; RULING 2026-10-21T21:14:58Z lane=cloud-backlog-2115).

Operator, verbatim: "OK yeah I agree start sending backlog tickets to the cloud I mean they can use mocks right CI will
catch issues." ``cloud_backlog.candidates`` picks the pillar backlog's tickets, then the sprint's not-started ones, and
skips what a live CLAIM, an open PR, a merge, a closed state or an earlier send holds, every excluded repository and
every ticket that needs the lab to write the code. ``brief`` tells the session to mock the lab and push only green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_idle_slot_fill_effect import cloud_backlog as cb
from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import ENV, write_overlay

NOW = "2026-10-21T21:30:00Z"
DECL = cb.load_declaration(ENV)


def ticket(
    tid: str,
    repo: str = "omnimarket",
    title: str = "delegation events carry the terminal cause",
) -> dict:
    return {
        "kind": "ticket",
        "key": tid,
        "repo": repo,
        "title": title,
        "fact": f"{tid} Backlog: {title}",
    }


def pick(backlog=(), sprint=(), **over):
    args = {
        "operator": "op-1",
        "claimed": set(),
        "open_pr": set(),
        "merged": set(),
        "closed": set(),
        "sent": {},
        "decl": DECL,
        "now": NOW,
        **over,
    }
    return cb.candidates(list(backlog), list(sprint), **args)


def test_backlog_tickets_are_taken_in_order_and_other_kinds_are_not() -> None:
    items = [
        ticket("OMN-1"),
        {"kind": "m4-board-red", "key": "x", "repo": "omnimarket", "title": "t"},
        ticket("OMN-2", "omnibase_core"),
        {"kind": "experiment-design", "key": "exp:y", "repo": "omnimarket"},
    ]
    got, _ = pick(items)
    assert [(i["ticket"], i["repo"], i["key"], i["source"]) for i in got] == [
        ("OMN-1", "omnimarket", "ticket:OMN-1", "pillar-backlog"),
        ("OMN-2", "omnibase_core", "ticket:OMN-2", "pillar-backlog"),
    ]


@pytest.mark.parametrize(
    ("item", "over", "reason"),
    [
        (ticket("OMN-3", "repo-deploy"), {}, "excluded-repo"),
        (ticket("OMN-3", "repo-knowledge-base"), {}, "excluded-repo"),
        (
            ticket("OMN-3", "omnimarket", "move the repo-deploy board probe"),
            {},
            "excluded-repo",
        ),
        (
            ticket(
                "OMN-3", "omnibase_infra", "Lab host 192.0.2.7 is swapping continuously"
            ),
            {},
            "needs-lab",
        ),
        (
            ticket(
                "OMN-3",
                "omnibase_infra",
                "The dev-lane cloud migration one-shot is fatal",
            ),
            {},
            "needs-lab",
        ),
        (
            ticket("OMN-3", "omnimarket", "deploy the runtime to the dev environment"),
            {},
            "needs-lab",
        ),
        (
            ticket(
                "OMN-3",
                "repo-plugin-source",
                "validate-plugin.yml never sets ONEX_REGISTRY_ROOT",
            ),
            {},
            "needs-lab",
        ),
        (ticket("OMN-3", "omnimarket"), {"claimed": {"OMN-3"}}, "claimed"),
        (ticket("OMN-3", "omnimarket"), {"open_pr": {"OMN-3"}}, "open-pr"),
        (ticket("OMN-3", "omnimarket"), {"merged": {"OMN-3"}}, "merged"),
        (ticket("OMN-3", "omnimarket"), {"closed": {"OMN-3"}}, "closed"),
        (ticket("OMN-3", "omnimarket"), {"other_owner": {"OMN-3"}}, "other-owner"),
        (
            ticket("OMN-3", "omnimarket"),
            {
                "sent": {
                    "ticket:OMN-3": {
                        "lane": "cloud-omn-3-2100",
                        "at": "2026-10-21T21:00:00Z",
                    }
                }
            },
            "sent",
        ),
        (
            ticket("OMN-3", "omnimarket"),
            {"out_of_scope": {"omnimarket"}},
            "excluded-repo",
        ),
        (ticket("OMN-3", "repo-web-a"), {}, "repo-not-declared"),
        (
            {"kind": "ticket", "key": "no id here", "repo": "omnimarket", "title": "t"},
            {},
            "no-ticket",
        ),
    ],
)
def test_a_held_excluded_or_lab_bound_ticket_is_skipped(
    item: dict, over: dict, reason: str
) -> None:
    got, skipped = pick([item], **over)
    assert got == [], got
    assert skipped == {reason: 1}, skipped
    # positive control: the same ticket with nothing holding it is taken
    control = ticket("OMN-4", "omnibase_core")
    got, _ = pick([item, control], **over)
    assert [i["ticket"] for i in got] == ["OMN-4"]


def test_a_dispatch_brief_caveat_does_not_exclude_the_ticket() -> None:
    item = {
        **ticket("OMN-20"),
        "brief": "if it is repo-deploy/cloud or dashboard work or needs the lab host, change nothing",
    }
    got, skipped = pick([item])
    assert [i["ticket"] for i in got] == ["OMN-20"], got
    assert skipped == {}, skipped


def test_the_linear_text_decides_the_repository_and_is_judged_too() -> None:
    items = [
        ticket("OMN-30", "omnimarket", "finish stale In Progress ticket OMN-30"),
        ticket("OMN-31", "omnimarket", "finish stale In Progress ticket OMN-31"),
        ticket("OMN-32", "omnimarket", "finish stale In Progress ticket OMN-32"),
        ticket("OMN-33", "omnimarket", "finish stale In Progress ticket OMN-33"),
        ticket("OMN-34", "omnimarket", "finish stale In Progress ticket OMN-34"),
        ticket("OMN-35", "omnibase_core", "finish stale In Progress ticket OMN-35"),
        ticket("OMN-36", "omnimarket", "finish stale In Progress ticket OMN-36"),
    ]
    linear = {
        "OMN-30": {
            "title": "omnibase_core literal corpus keeps pristine hooks red",
            "description": "",
        },
        "OMN-31": {
            "title": "a fix with no repository named",
            "description": "somewhere",
        },
        "OMN-32": {
            "title": "repo-notes-a: eliminate a claim race",
            "description": "in omnimarket too",
        },
        "OMN-33": {
            "title": "omnimarket node: redeploy it with kubectl",
            "description": "context: the 192.0.2.7 lab host",
        },
        "OMN-35": {
            "title": "a gate in omnibase_core misreads omnibase_infra pins",
            "description": "",
        },
        "OMN-36": {
            "title": "a gate in omnibase_core misreads omnibase_infra pins",
            "description": "",
        },
    }
    got, skipped = pick(items, linear=linear)
    assert [(i["ticket"], i["repo"]) for i in got] == [
        ("OMN-30", "omnibase_core"),
        ("OMN-31", "omnimarket"),
        ("OMN-34", "omnimarket"),
        ("OMN-35", "omnibase_core"),
    ], got
    assert skipped == {"no-repo": 1, "excluded-repo": 1, "needs-lab": 1}, skipped
    # a description that mentions a lab host as context does not make the ticket need the lab
    got, _ = pick(
        [ticket("OMN-37", "omnimarket", "finish stale In Progress ticket OMN-37")],
        linear={
            "OMN-37": {
                "title": "a parser bug",
                "description": "seen first on the 192.0.2.7 lab host",
            }
        },
    )
    assert [i["ticket"] for i in got] == ["OMN-37"]


def test_a_send_outside_the_window_frees_the_ticket_again() -> None:
    got, _ = pick(
        [ticket("OMN-5")],
        sent={"ticket:OMN-5": {"lane": "x", "at": "2026-10-19T21:00:00Z"}},
    )
    assert [i["ticket"] for i in got] == ["OMN-5"]
    got, skipped = pick(
        [ticket("OMN-5")], sent={"ticket:OMN-5": {"lane": "x", "at": "not a stamp"}}
    )
    assert got == []
    assert skipped == {"sent": 1}


def sprint_issue(tid: str, **over) -> dict:
    return {
        "identifier": tid,
        "title": f"omnibase_core: a small enum gains a member ({tid})",
        "description": "",
        "state_type": "unstarted",
        "assignee": None,
        "estimate": 1,
        **over,
    }


def test_sprint_tickets_follow_the_backlog_and_only_unstarted_unowned_small_ones_count() -> (
    None
):
    sprint = [
        sprint_issue("OMN-10"),
        sprint_issue("OMN-11", assignee="op-1"),
        sprint_issue("OMN-12", assignee="someone-else"),
        sprint_issue("OMN-13", state_type="started"),
        sprint_issue("OMN-14", estimate=5),
        sprint_issue("OMN-15", title="no repository named here"),
        sprint_issue("OMN-16", state_type="backlog"),
        sprint_issue("OMN-1"),
    ]
    got, skipped = pick([ticket("OMN-1")], sprint)
    assert [(i["ticket"], i["source"]) for i in got] == [
        ("OMN-1", "pillar-backlog"),
        ("OMN-10", "sprint"),
        ("OMN-11", "sprint"),
        ("OMN-16", "sprint"),
    ]
    assert skipped == {
        "other-owner": 1,
        "started-or-closed": 1,
        "too-large": 1,
        "no-repo": 1,
    }, skipped
    assert got[1]["repo"] == "omnibase_core"


def test_the_longest_repository_name_wins() -> None:
    assert (
        cb.repo_of("fix repo-plugin-source hooks", DECL["repos"])
        == "repo-plugin-source"
    )
    assert cb.repo_of("fix omniclaude hooks", DECL["repos"]) == "omniclaude"


def test_the_rules_come_from_the_declaration_not_the_code(tmp_path: Path) -> None:
    code = Path(cb.__file__).read_text(encoding="utf-8")
    for token in ('"repo-deploy"', '"repo-knowledge-base"', '"redpanda"', '"kubectl"'):
        assert token not in code, (
            f"cloud_backlog.py names {token}: the rules belong in the overlay"
        )
    assert {"repo-deploy", "repo-knowledge-base"} <= set(DECL["excluded"])
    narrow = write_overlay(
        tmp_path,
        idle_slot_fill={
            "cloud_backlog": {
                "repos": ["omnimarket"],
                "excluded_repos": [],
                "lab_markers": ["widget"],
                "sprint_state_types": ["unstarted"],
            }
        },
    )
    decl = cb.load_declaration(narrow)
    got, skipped = pick(
        [
            ticket("OMN-6", title="a widget fix"),
            ticket("OMN-7", "omnibase_core"),
            ticket("OMN-8"),
        ],
        decl=decl,
    )
    assert [i["ticket"] for i in got] == ["OMN-8"]
    assert skipped == {
        "needs-lab": 1,
        "repo-not-declared": 1,
    }
    with pytest.raises(NodeOverlayError, match=r"idle_slot_fill\.cloud_backlog"):
        cb.load_declaration({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "empty")})


def test_the_brief_mocks_the_lab_pushes_green_and_writes_no_host_line() -> None:
    item = {
        **ticket("OMN-9"),
        "ticket": "OMN-9",
        "source": "pillar-backlog",
        "brief": "keep it small",
    }
    lane = cb.lane_name(item, NOW)
    assert lane == "cloud-omn-9-2130"
    text = cb.brief(item, lane, "cloud-backlog-2115")
    assert text.startswith(
        f"# Lane {lane}: cloud backlog ticket OMN-9 (omnimarket)\nROUTE: model=sonnet effort=high\n"
    )
    for needed in (
        "Mock or fake every lab dependency",
        "Push only when they pass",
        "The title carries OMN-9",
        "RULING 2026-10-21T21:14:58Z",
        "they can use mocks right CI will catch issues",
        "Write no line about a host you did not run on",
        "Never merge, arm",
        "keep it small",
    ):
        assert needed in text, needed
