"""The lab is never idle: the throughput tick fills free slots from the pillar backlog itself (OMN-17427).

2026-10-18 ~19:45Z to 21:25Z: every throughput tick printed MISSING lab-headroom with 13 to 29 free slots, the merge
sweep planned 0 lanes (reds=40, planned=0), and the orchestrator left the slots idle. RULING 2026-10-14T21:46:25Z
already said to fill them. ``HandlerIdleSlotFill`` decides what the tick sends; ``HandlerIdleTurnGuard`` refuses an
orchestrator turn that read free headroom or an UNOWNED red and dispatched nothing.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from omnimarket.nodes.node_idle_slot_fill_effect import models
from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
    handler_idle_slot_fill as handler,
)
from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import (
    ENV,
    EXPERIMENT_DESIGNS,
)

NOW = "2026-10-18T21:30:00Z"
PF_LINE = (
    "MISSING process-fix:repo-private-data#812 | handed off 2026-10-18T19:00:00Z by fix-lane-1; red (pytest), "
    "unmerged 150m after hand-off at head abcdef123456 | FIX: driver lane pf-drive-repo-private-data-812: dispatch "
    "it with --priority-class process-fix to carry repo-private-data#812 to merged"
)
RED_LINE = (
    "MISSING repo-status:omnimarket | shared_red_unowned: check pytest red on 4 PRs, no cause lane | FIX: dispatch "
    "ONE cause lane for check pytest in omnimarket"
)
OWNED_PF = "NOTE process-fix:repo-plugin-source#900 | owned by lease fix-900 | FIX: "
HOST_TICK = (
    "MISSING host-tick:minutes-watch | declared tick minutes-watch: unit is not "
    "loaded | FIX: reinstall it"
)


def host(
    name: str, claude: int, codex: int, *, error: str | None = None, local: bool = False
) -> dict:
    return {
        "name": name,
        "local": local,
        "admission": {
            "claude_slots": claude,
            "codex_slots": codex,
            "probe_error": error,
        },
    }


def headroom(*names: str) -> list[str]:
    return [
        f"MISSING lab-headroom:{n} | free Claude slots 3 | FIX: dispatch" for n in names
    ]


STANDING = dict.fromkeys(
    (
        "lab-repair",
        "dev-red",
        "process-fix",
        "cause-red",
        "m4-board-red",
        "experiment-design",
    ),
    "OMN-9101",
)


def fill(**changes) -> models.ModelIdleSlotFillResult:
    request = {
        "standing_tickets": STANDING,
        "now": NOW,
        "hosts": [host("host-a", 2, 2)],
        "lines": [*headroom("host-a"), PF_LINE, RED_LINE],
        **changes,
    }
    return handler.HandlerIdleSlotFill().handle(
        models.ModelIdleSlotFillRequest(**request)
    )


def test_free_slots_are_filled_from_the_tick_lines_in_pillar_order():
    result = fill()
    assert [(s["kind"], s["key"], s["model"]) for s in result.sends] == [
        ("process-fix", "repo-private-data#812", "codex"),
        ("cause-red", "repo-status:omnimarket", "codex"),
    ]
    first, second = result.sends
    assert first["lane"] == "fill-pf-drive-repo-private-data-812-20261018T2130Z"
    assert first["repo"] == "repo-private-data"
    assert first["priority_class"] == "process-fix"
    assert PF_LINE in first["brief"]
    assert "RULING 2026-10-14T21:46:25Z" in first["brief"]
    assert second["repo"] == "omnimarket"
    assert second["priority_class"] == "mvp-landing"
    assert RED_LINE in second["brief"]
    assert result.claude_free == 2
    assert result.idle_after == 0


def test_a_broken_lab_piece_gets_a_repair_lane_first():
    drift = "MISSING running-vs-merged:host-a-dispatch-venv | differs from main for 3h | FIX: reinstall the venv"
    result = fill(lines=[*headroom("host-a"), PF_LINE, drift])
    assert [(s["kind"], s["key"]) for s in result.sends] == [
        ("lab-repair", "repair:running-vs-merged:host-a-dispatch-venv"),
        ("process-fix", "repo-private-data#812"),
    ]
    repair = result.sends[0]
    assert (
        repair["lane"]
        == "fill-repair-running-vs-merged-host-a-dispatch-venv-20261018T2130Z"
    )
    assert repair["priority_class"] == "process-fix"
    assert drift in repair["brief"]
    assert repair["repo"] == ""
    assert "host" not in repair
    assert "Required host:" not in repair["brief"]


def test_a_host_tick_repair_goes_to_the_launching_host_only():
    # 2026-10-20: the repair for this line was placed on host-c, which cannot reach the launching host's launchd (OMN-17427)
    result = fill(
        hosts=[host("host-b", 1, 0, local=True), host("host-c", 2, 0)],
        lines=[*headroom("host-b", "host-c"), HOST_TICK, PF_LINE, RED_LINE],
    )
    repair, *other = result.sends
    assert repair["key"] == "repair:host-tick:minutes-watch"
    assert repair["host"] == "host-b"
    assert repair["model"] == "sonnet"
    lines = repair["brief"].splitlines()
    parent = next(i for i, line in enumerate(lines) if line.startswith("Parent: "))
    assert lines[parent + 1] == "Required host: host-b"
    assert lines[parent + 2] == (
        "The tick that printed the line below runs on host-b; this repair reads and changes that "
        "host's own files, so it runs there."
    )
    assert [s["key"] for s in other] == [
        "repo-private-data#812",
        "repo-status:omnimarket",
    ]
    assert all("host" not in s and s["model"] == "sonnet" for s in other)
    assert result.claude_free == 3
    assert result.idle_after == 0
    assert result.skipped == []


@pytest.mark.parametrize(
    ("slots", "free", "error"),
    [(0, True, None), (1, False, None), (1, True, "probe failed")],
)
def test_a_host_tick_repair_waits_for_a_free_launching_host_slot(slots, free, error):
    result = fill(
        hosts=[host("host-b", slots, 2, local=True, error=error), host("host-c", 2, 0)],
        lines=[
            *headroom(*(["host-b", "host-c"] if free else ["host-c"])),
            HOST_TICK,
            PF_LINE,
            RED_LINE,
        ],
    )
    assert [s["key"] for s in result.sends] == [
        "repo-private-data#812",
        "repo-status:omnimarket",
    ]
    expected_model = "codex" if slots == 0 and free and error is None else "sonnet"
    assert all("host" not in s and s["model"] == expected_model for s in result.sends)
    assert result.skipped == [
        {
            "key": "repair:host-tick:minutes-watch",
            "why": "a host-tick repair runs only on the launching host host-b, which has no free Claude slot",
        }
    ]


def test_a_host_tick_repair_with_no_local_host_row_is_not_sent():
    result = fill(
        hosts=[host("host-c", 2, 0)],
        lines=[*headroom("host-c"), HOST_TICK, PF_LINE, RED_LINE],
    )
    assert [s["key"] for s in result.sends] == [
        "repo-private-data#812",
        "repo-status:omnimarket",
    ]
    assert result.skipped == [
        {
            "key": "repair:host-tick:minutes-watch",
            "why": "a host-tick repair runs only on the launching host, and no local host row was read",
        }
    ]


@pytest.mark.parametrize(("slots", "sweep"), [(1, 0), (2, 2)])
def test_host_tick_repairs_take_no_more_than_the_launching_host_s_and_the_pool_s_slots(
    slots, sweep
):
    second = HOST_TICK.replace("minutes-watch", "pr-watcher")
    result = fill(
        hosts=[host("host-b", slots, 0, local=True), host("host-c", 0, 2)],
        sweep_planned=sweep,
        lines=[*headroom("host-b", "host-c"), HOST_TICK, second, PF_LINE, RED_LINE],
    )
    repairs = [s for s in result.sends if s["kind"] == "lab-repair"]
    assert len(repairs) == (1 if sweep == 0 else 0)
    assert all(s["host"] == "host-b" and s["model"] == "sonnet" for s in repairs)
    assert [s["model"] for s in result.sends if s["kind"] != "lab-repair"] == [
        "codex",
        "codex",
    ]
    assert result.idle_after == 0


def test_the_sweep_s_planned_lanes_take_their_slots_first():
    result = fill(sweep_planned=1)
    assert [s["kind"] for s in result.sends] == ["process-fix"]
    assert result.skipped == [{"key": "repo-status:omnimarket", "why": "no free slot"}]


def test_codex_lanes_go_to_codex_only_hosts_after_claude_slots():
    """Historical case: the current policy spends Codex capacity first."""
    result = fill(
        hosts=[host("host-a", 1, 1), host("host-d", 0, 2)],
        lines=[*headroom("host-a", "host-d"), PF_LINE, RED_LINE],
        backlog=[
            {
                "kind": "experiment-design",
                "key": "exp:cache-warm",
                "title": "cache warm",
                "repo": "omnimarket",
            }
        ],
    )
    assert [(s["key"], s["model"]) for s in result.sends] == [
        ("repo-private-data#812", "codex"),
        ("repo-status:omnimarket", "codex"),
        ("exp:cache-warm", "codex"),
    ]
    assert result.codex_only_free == 2
    assert result.idle_after == 0


def test_only_hosts_the_node_reported_free_and_read_count():
    result = fill(
        hosts=[
            host("host-a", 2, 2),
            host("host-e", 4, 4),
            host("host-f", 3, 3, error="ssh timeout"),
        ],
        lines=[*headroom("host-a", "host-f"), PF_LINE],
    )
    assert result.claude_free == 2
    assert len(result.sends) == 1
    assert result.idle_after == 1


def test_owned_process_fix_lines_are_not_backlog():
    result = fill(lines=[*headroom("host-a"), OWNED_PF])
    assert result.sends == []
    assert result.idle_after == 2


def test_backlog_kinds_follow_the_tick_lines_and_a_key_sent_inside_the_window_is_skipped():
    backlog = [
        {
            "kind": "experiment-design",
            "key": "exp:a",
            "title": "A",
            "repo": "omnimarket",
        },
        {
            "kind": "m4-board-red",
            "key": "m4:C13",
            "title": "C13 red",
            "repo": "omnibase_infra",
        },
    ]
    result = fill(
        lines=[*headroom("host-a")],
        backlog=backlog,
        dispatched={"m4:C13": {"lane": "fill-m4-C13-x", "at": "2026-10-18T20:00:00Z"}},
    )
    assert [s["key"] for s in result.sends] == ["exp:a"]
    assert {
        "key": "m4:C13",
        "why": "sent as fill-m4-C13-x at 2026-10-18T20:00:00Z",
    } in result.skipped
    older = fill(
        lines=[*headroom("host-a")],
        backlog=backlog,
        dispatched={"m4:C13": {"lane": "fill-m4-C13-x", "at": "2026-10-18T17:00:00Z"}},
    )
    assert [s["key"] for s in older.sends] == ["m4:C13", "exp:a"]


def test_no_free_slot_sends_nothing():
    assert fill(lines=[PF_LINE]).sends == []


# A red landing branch is fixed forward by a lane the tick sends itself (RULING 2026-10-14T03:57:46Z, OMN-20730:
# "I'm fine with fixing forward for dev ... 5 auto fix"). The tick's repo-status verdict for it was a NOTE that
# nothing acted on, so a red dev waited for a person.
DEV_RED = (
    "NOTE repo-status:omnimarket | omnimarket: 5 open, 0 merged in 60 min; base_branch_red: base branch red on "
    "Tests Gate, lint"
)
EJECTING = (
    "MISSING repo-status:omnibase_infra | omnibase_infra: 3 open, 0 merged in 60 min; merge_queue_ejecting: 3 "
    "PRs ejected for base-branch red pytest | FIX: the merge queue ejected omnibase_infra#1, omnibase_infra#2, "
    "omnibase_infra#3 for the base-branch red pytest: the controller re-admits them once the base is green"
)
EJECTING_OWNED = (
    "MISSING repo-status:omniclaude | omniclaude: 2 open, 0 merged in 60 min; merge_queue_ejecting: 2 PRs "
    "ejected for base-branch red lint | FIX: the controller owns it (cause lease 41): read "
    "/omni:landing-controller status; the session starts no fix work for it"
)


def test_a_red_landing_branch_gets_a_fix_forward_lane_before_other_work():
    result = fill(
        hosts=[host("host-a", 4, 0)],
        lines=[*headroom("host-a"), PF_LINE, RED_LINE, DEV_RED, EJECTING],
    )
    assert [(s["kind"], s["key"]) for s in result.sends] == [
        ("dev-red", "dev-red:omnimarket"),
        ("dev-red", "dev-red:omnibase_infra"),
        ("process-fix", "repo-private-data#812"),
        ("cause-red", "repo-status:omnimarket"),
    ]
    dev = result.sends[0]
    assert dev["lane"] == "fill-dev-red-omnimarket-20261018T2130Z"
    assert dev["repo"] == "omnimarket"
    assert dev["priority_class"] == "mvp-landing"
    assert DEV_RED in dev["brief"]
    assert "fix forward" in dev["brief"]
    assert "never revert" in dev["brief"]
    assert "RULING 2026-10-14T03:57:46Z" in dev["brief"]


def test_a_landing_branch_red_the_controller_owns_and_a_green_repo_status_send_nothing():
    quiet = "NOTE repo-status:omnimarket | omnimarket: 2 open, 0 merged in 60 min; waiting_on_ci: 2 PRs with checks pending"
    result = fill(lines=[*headroom("host-a"), EJECTING_OWNED, quiet])
    assert result.sends == []
    assert result.idle_after == 2


def test_a_dev_red_sent_inside_the_window_is_not_sent_again():
    result = fill(
        lines=[*headroom("host-a"), DEV_RED],
        dispatched={
            "dev-red:omnimarket": {
                "lane": "fill-dev-red-omnimarket-x",
                "at": "2026-10-18T20:00:00Z",
            }
        },
    )
    assert result.sends == []
    assert result.skipped == [
        {
            "key": "dev-red:omnimarket",
            "why": "sent as fill-dev-red-omnimarket-x at 2026-10-18T20:00:00Z",
        }
    ]


# 2026-10-19 12:28Z and 12:33Z: the tick printed a dev-red line for onex_change_control, retired by RULING
# 2026-10-18T05:25:04Z, and the fill sent lane fill-dev-red-onex_change_control into the lab. A repository the merge
# sweep's one scope setting (sweep_scope.json) rules out gets no lane of any kind.
OCC_RED = (
    "MISSING repo-status:onex_change_control | onex_change_control: 3 open, 0 merged in 60 min; base_branch_red: "
    "base branch red on validate"
)
OCC_SCOPE = {
    "onex_change_control": "RULING 2026-10-18T05:25:04Z lane=orchestrator-bb1a: retired"
}


def test_a_retired_repository_s_tick_line_yields_no_candidate():
    occ_pf = "MISSING process-fix:onex_change_control#9362 | handed off; red | FIX: dispatch it"
    occ_shared = "MISSING repo-status:onex_change_control | shared_red_unowned: check validate red on 2 PRs | FIX: "
    backlog = [
        {
            "kind": "m4-board-red",
            "key": "m4:occ",
            "title": "occ red",
            "repo": "onex_change_control",
        }
    ]
    items = handler.candidates(
        [OCC_RED, occ_pf, occ_shared, DEV_RED], backlog, out_of_scope=OCC_SCOPE
    )
    assert [i["key"] for i in items] == ["dev-red:omnimarket"]


def test_positive_control_without_the_scope_the_retired_line_is_a_candidate():
    assert [i["key"] for i in handler.candidates([OCC_RED], [])] == [
        "dev-red:onex_change_control"
    ]


def test_a_generated_item_of_a_retired_repository_is_no_candidate():
    generated = [
        {
            "kind": "ticket",
            "key": "ticket:OMN-1",
            "repo": "onex_change_control",
            "title": "occ",
        },
        {
            "kind": "ticket",
            "key": "ticket:OMN-2",
            "repo": "omnimarket",
            "title": "market",
        },
    ]
    items = handler.candidates([], [], generated, out_of_scope=OCC_SCOPE)
    assert [i["key"] for i in items] == ["ticket:OMN-2"]


def test_the_fill_sends_nothing_for_an_out_of_scope_repository():
    result = fill(lines=[*headroom("host-a"), OCC_RED, PF_LINE], out_of_scope=OCC_SCOPE)
    assert [s["key"] for s in result.sends] == ["repo-private-data#812"]
    assert result.idle_after == 1


def test_an_unread_scope_sends_no_repository_lane_and_says_why():
    drift = "MISSING running-vs-merged:host-a-dispatch-venv | differs from main for 3h | FIX: reinstall the venv"
    result = fill(
        lines=[*headroom("host-a"), OCC_RED, drift],
        scope_unread="sweep scope unreadable: x",
    )
    assert [s["key"] for s in result.sends] == [
        "repair:running-vs-merged:host-a-dispatch-venv"
    ]
    assert {
        "key": "dev-red:onex_change_control",
        "why": "repo scope unread: sweep scope unreadable: x",
    } in result.skipped


def guard(uses: list[dict], results: list[str]) -> models.ModelIdleTurnGuardResult:
    """Each result is the output of the first tool call; the rest of the calls follow it in the turn."""
    uses = [{"id": f"t{i}", **use} for i, use in enumerate(uses)]
    reader = [
        {"id": "r0", "name": "Bash", "input": {"command": "python3 throughput_tick.py"}}
    ]
    return handler.HandlerIdleTurnGuard().handle(
        models.ModelIdleTurnGuardRequest(
            reader + uses, [{"id": "r0", "text": text} for text in results]
        )
    )


TICK_OUT = "\n".join(
    [
        *headroom("host-a", "host-d"),
        "THROUGHPUT STALL n=2 unknown=0: lab-headroom:host-a, lab-headroom:host-d",
    ]
)


def test_a_turn_that_read_free_headroom_and_dispatched_nothing_is_refused():
    out = guard(
        [{"name": "Bash", "input": {"command": "python3 throughput_tick.py"}}],
        [TICK_OUT],
    )
    assert out.refuse
    assert "MISSING lab-headroom:host-a" in out.reason
    assert "RULING 2026-10-14T21:46:25Z" in out.reason


def test_an_unowned_red_with_no_dispatch_is_refused():
    pulse = "armed-red-stall omnimarket pytest on 4 PRs: UNOWNED | NEXT: dispatch a cause lane"
    out = guard(
        [{"name": "Bash", "input": {"command": "python3 queue_status.py"}}], [pulse]
    )
    assert out.refuse
    assert "UNOWNED" in out.reason


def test_a_dispatch_in_the_same_turn_passes():
    for use in (
        {"name": "Agent", "input": {"prompt": "x"}},
        {"name": "Workflow", "input": {"script": "x"}},
        {"name": "Skill", "input": {"skill": "omni:remote-lane"}},
        {
            "name": "Bash",
            "input": {
                "command": "python3 onex_remote_lane.py run --brief b.md --lane x"
            },
        },
    ):
        assert not guard([use], [TICK_OUT]).refuse, use


def test_the_tick_filling_every_slot_itself_passes():
    out = (
        TICK_OUT
        + "\nNOTE lab-fill | sent=2 (claude=2 codex=0) free=2+0 sweep_planned=0 idle_after=0: a, b | FIX: "
    )
    assert not guard(
        [{"name": "Bash", "input": {"command": "python3 throughput_tick.py"}}], [out]
    ).refuse
    left = out.replace("idle_after=0", "idle_after=3")
    assert guard(
        [{"name": "Bash", "input": {"command": "python3 throughput_tick.py"}}], [left]
    ).refuse


def test_only_the_tick_s_and_the_pulse_s_own_output_counts():
    uses = [
        {
            "id": "g",
            "name": "Bash",
            "input": {
                "command": "grep -n UNOWNED queue_status.py.bak lab-headroom.txt"
            },
        }
    ]
    request = models.ModelIdleTurnGuardRequest(
        uses, [{"id": "g", "text": TICK_OUT + "\n  UNOWNED, with no lease"}]
    )
    assert not handler.HandlerIdleTurnGuard().handle(request).refuse
    uses = [
        {
            "id": "q",
            "name": "Bash",
            "input": {"command": "python3 /x/queue_status.py --no-write"},
        }
    ]
    request = models.ModelIdleTurnGuardRequest(
        uses, [{"id": "q", "text": "pytest on 4 PRs: UNOWNED"}]
    )
    assert handler.HandlerIdleTurnGuard().handle(request).refuse


def test_a_broken_lab_piece_with_no_dispatch_is_refused():
    for line in (
        "MISSING running-vs-merged:host-a-dispatch-venv | differs from main for 3h | FIX: reinstall",
        "MISSING host-tick:pr-watcher | evidence_file 2h old | FIX: reload it",
        "UNKNOWN throughput-node | onex node exited 1 without a result | FIX: deploy",
    ):
        out = guard([{"name": "Read", "input": {}}], [line])
        assert out.refuse, line
        assert "broken lab piece" in out.reason, line
        assert line in out.reason, line
        assert not guard(
            [{"name": "Agent", "input": {"prompt": "repair"}}], [line]
        ).refuse
    assert not guard([], ["NOTE running-vs-merged | all hosts match main"]).refuse


def test_a_quiet_turn_passes():
    assert not guard(
        [{"name": "Read", "input": {}}], ["THROUGHPUT OK checked=controller"]
    ).refuse


# ---- the generated backlog: the file backlog spent, live state fills the slots (OMN-17427) ---------------------------
# 2026-10-19 13:00Z to 15:45Z: the tick printed MISSING lab-fill-backlog with 7 to 18 slots idle on most ticks and the
# orchestrator refilled pillar-backlog.json by hand five times (experiments, sprint tickets, triage readbacks). Once the
# file is spent the fill generates items itself: the operator's open delegation-pillar and M4-carrier tickets with no
# live CLAIM and no open PR (the session's Linear snapshot), tickets whose triage verdict is blocked on a missing
# readback (the ledger), and the checked-in delegation experiment designs, in rotation.


OPERATOR = "user-operator"
FILL_NOW = "2026-10-19T16:00:00Z"
FREE3 = "MISSING lab-headroom:host-a | placed units 0 of cap 3, free Claude slots 3 | FIX: dispatch"
POOL3 = {
    "lab_headroom": {
        "hosts": [
            {"name": "host-a", "admission": {"claude_slots": 3, "codex_slots": 0}}
        ]
    }
}


def issue(
    tid: str,
    title: str,
    *,
    assignee: str | None = OPERATOR,
    labels: tuple[str, ...] = (),
    project: str = "",
    status: str = "Todo",
    status_type: str = "unstarted",
    repo: str | None = None,
) -> dict:
    out = {
        "id": tid,
        "title": title,
        "status": status,
        "statusType": status_type,
        "assigneeId": assignee,
        "labels": list(labels),
        "project": project,
    }
    return {**out, "repo": repo} if repo else out


EXPERIMENTS = [
    {
        "id": "glm-flash-pr-body",
        "title": "GLM flash vs glm-5.3 on PR bodies",
        "repo": "repo-plugin-source",
        "design": "draft 10 PR bodies on each model from the same fact tables and grade them",
    },
    {
        "id": "jev-ci-cause",
        "title": "Jev ci-cause preset accuracy",
        "repo": "omnimarket",
        "design": "ask 20 recorded CI reds through the ci-cause preset and score against the recorded class",
    },
]


def fake_rule(facts: dict) -> dict:
    """Deterministic fixture plan; integration below checks the real owning rule."""
    items, drops = [], []
    for i in facts["issues"]:
        carriers = [c for c, ids in facts["m4_carriers"].items() if i["id"] in ids]
        text = " ".join((i["title"], *i["labels"], i["project"])).lower()
        reason = (
            "closed"
            if i["status_type"] == "completed"
            else "other-owner"
            if i["assignee"] not in (facts["operator"], "me")
            else "out-of-scope"
            if i["repo"] in facts["out_of_scope"]
            or any(r in text for r in facts["out_of_scope"])
            else "dashboard"
            if "dashboard" in text or set(carriers) & {"C31", "C32"}
            else "open-pr"
            if i["id"] in facts["open_pr"]
            else "claimed"
            if i["id"] in facts["claimed"]
            else None
        )
        if reason:
            drops.append({"id": i["id"], "reason": reason, "detail": "fixture"})
        elif carriers or "delegation" in text or i["pillar"] == "delegation":
            items.append(
                {**i, "source": "delegation", "carriers": carriers, "criteria": []}
            )
    items.sort(key=lambda i: not i["carriers"])
    return {"items": items, "drops": drops}


def sources(
    tmp: Path,
    issues: list[dict],
    *,
    claimed: set[str] = frozenset(),
    readbacks: dict | None = None,
    carriers: dict | None = None,
    experiments: list[dict] | None = None,
    ledger_error: str | None = None,
):
    (tmp / "linear.json").write_text(
        json.dumps(
            {
                "operator": OPERATOR,
                "generated_at": "2026-10-19T15:30:00Z",
                "issues": issues,
            }
        )
    )
    (tmp / "m4-carriers.json").write_text(
        json.dumps(
            {
                "carriers": sorted(
                    {t for ids in (carriers or {}).values() for t in ids}
                ),
                "criteria": carriers or {},
            }
        )
    )
    (tmp / "scope.json").write_text(
        json.dumps({"out_of_scope": {"repo-deploy": "M4.5", "repo-web-a": "parked"}})
    )
    designs = EXPERIMENTS if experiments is None else experiments

    def ledger_tickets() -> tuple[set[str], dict[str, str]]:
        if ledger_error:
            raise RuntimeError(ledger_error)
        return set(claimed), dict(readbacks or {})

    return models.ModelBacklogSources(
        linear_snapshot=tmp / "linear.json",
        m4_carriers=tmp / "m4-carriers.json",
        scope=tmp / "scope.json",
        experiments=lambda: designs,
        ledger_tickets=ledger_tickets,
        ticket_rule=fake_rule,
    )


def stub_runner(tmp: Path) -> Path:
    path = tmp / "runner.py"
    path.write_text(
        "import json, sys\n"
        f"open({str(tmp / 'calls.jsonl')!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print('DETACHED lane=' + sys.argv[sys.argv.index('--lane') + 1])\n"
    )
    return path


def lab_fill(
    tmp: Path, src, *, watcher: dict | None = None, lines: list[str] | None = None
):
    if not (tmp / "backlog.json").exists():
        (tmp / "backlog.json").write_text(json.dumps({"standing_tickets": STANDING}))
    request = {**POOL3, **({"watcher_state": watcher} if watcher is not None else {})}
    found = (
        handler.HandlerLabFill()
        .handle(
            models.ModelLabFillRequest(
                FILL_NOW,
                request,
                lines or [FREE3],
                None,
                tmp / "state",
                str(tmp / "backlog.json"),
                stub_runner(tmp),
                "orch-1",
                sources=src,
            )
        )
        .found
    )
    path = tmp / "calls.jsonl"
    sent = (
        [json.loads(x) for x in path.read_text().splitlines()] if path.is_file() else []
    )
    return found, [c[c.index("--lane") + 1] for c in sent], sent


def test_an_empty_file_backlog_with_free_slots_yields_generated_items(tmp_path: Path):
    src = sources(
        tmp_path,
        [
            issue(
                "OMN-101",
                "omnimarket: delegation receipts carry the task class",
                repo="omnimarket",
            ),
            issue("OMN-102", "route GLM first for text", labels=("delegation",)),
            issue(
                "OMN-103",
                "someone else's delegation work",
                assignee="user-other",
                labels=("delegation",),
            ),
        ],
        carriers={"C13": ["OMN-101"]},
    )
    found, lanes, sent = lab_fill(tmp_path, src)
    assert lanes == [
        "fill-ticket-OMN-101-20261019T1600Z",
        "fill-ticket-OMN-102-20261019T1600Z",
        "fill-exp-glm-flash-pr-body-20261019T1600Z",
    ]
    first = sent[0]
    assert first[first.index("--repo") + 1] == "omnimarket"
    assert first[first.index("--priority-class") + 1] == "mvp-landing"
    brief = Path(first[first.index("--brief") + 1]).read_text()
    assert "OMN-101" in brief
    assert "M4 carrier of C13" in brief
    assert "no live CLAIM" in brief
    assert not any(f[1] == "lab-fill-backlog" for f in found), found
    note = next(f for f in found if f[1] == "lab-fill-generator")
    assert note[0] == "note", note
    assert "tickets=2 readbacks=0 experiments=2" in note[2], note
    assert "other-owner=1" in note[2], note
    assert next(f for f in found if f[1] == "lab-fill")[2].startswith(
        "lab-fill | sent=3 (claude=3 codex=0) free=3+0"
    )


def test_a_ticket_with_a_live_claim_is_not_generated(tmp_path: Path):
    src = sources(
        tmp_path,
        [
            issue("OMN-101", "delegation receipts", labels=("delegation",)),
            issue("OMN-102", "route GLM first", labels=("delegation",)),
        ],
        claimed={"OMN-101"},
        experiments=[],
    )
    found, lanes, _ = lab_fill(tmp_path, src)
    assert lanes == ["fill-ticket-OMN-102-20261019T1600Z"]
    assert "claimed=1" in next(f for f in found if f[1] == "lab-fill-generator")[2]
    assert any(f[0] == "missing" and f[1] == "lab-fill-backlog" for f in found), (
        "two slots stay idle and say so"
    )


def test_skipped_tickets_open_pr_retired_repo_deploy_repo_dashboard_closed_and_unassigned():
    issues = [
        issue("OMN-201", "delegation with an open PR", labels=("delegation",)),
        issue(
            "OMN-202", "delegation in infra", labels=("delegation",), repo="repo-deploy"
        ),
        issue("OMN-203", "repo-web-a delegation page", labels=("delegation",)),
        issue(
            "OMN-204", "delegation dashboard tile", labels=("delegation", "dashboard")
        ),
        issue("OMN-205", "M4 local dashboard page"),
        issue(
            "OMN-206",
            "done delegation work",
            labels=("delegation",),
            status="Done",
            status_type="completed",
        ),
        issue(
            "OMN-207",
            "unassigned delegation work",
            labels=("delegation",),
            assignee=None,
        ),
        issue("OMN-208", "not a pillar ticket"),
        issue("OMN-209", "a delegation project ticket", project="Delegation pillar"),
    ]
    items, skipped = handler.generate_backlog(
        issues,
        OPERATOR,
        m4={"C31": ["OMN-205"]},
        out_of_scope={"repo-deploy", "repo-web-a"},
        claimed=set(),
        open_pr={"OMN-201"},
        readbacks={},
        experiments=[],
        sent={},
        rule=fake_rule,
        today=FILL_NOW[:10],
    )
    assert [i["key"] for i in items] == ["ticket:OMN-209"]
    assert skipped == {
        "open-pr": 1,
        "out-of-scope": 2,
        "dashboard": 2,
        "closed": 1,
        "other-owner": 1,
    }


def test_a_ticket_blocked_on_a_missing_readback_is_generated_unless_claimed():
    row = (
        "2026-10-19T12:17:01Z | TERMINAL | lane=fill-ticket-OMN-301-x | ticket=OMN-17427 | outcome=blocked | "
        "subject=OMN-301 | blocker=the AC2 live readback on the lab is missing"
    )
    items, _ = handler.generate_backlog(
        [],
        OPERATOR,
        m4={},
        out_of_scope=set(),
        claimed=set(),
        open_pr=set(),
        readbacks={"OMN-301": row},
        experiments=[],
        sent={},
        rule=fake_rule,
        today=FILL_NOW[:10],
    )
    assert [(i["kind"], i["key"]) for i in items] == [("readback", "readback:OMN-301")]
    assert row in items[0]["fact"]
    assert items[0]["ticket"] == "OMN-301"
    items, skipped = handler.generate_backlog(
        [],
        OPERATOR,
        m4={},
        out_of_scope=set(),
        claimed={"OMN-301"},
        open_pr=set(),
        readbacks={"OMN-301": row},
        experiments=[],
        sent={},
        rule=fake_rule,
        today=FILL_NOW[:10],
    )
    assert items == []
    assert skipped == {"claimed": 1}


def test_the_file_backlog_goes_first_and_the_generator_waits_until_it_is_spent(
    tmp_path: Path,
):
    (tmp_path / "backlog.json").write_text(
        json.dumps(
            {
                "standing_tickets": STANDING,
                "items": [
                    {
                        "kind": "experiment-design",
                        "key": f"exp:file-{n}",
                        "repo": "omnimarket",
                        "title": f"file {n}",
                    }
                    for n in range(3)
                ],
            }
        )
    )
    src = sources(
        tmp_path, [issue("OMN-101", "delegation receipts", labels=("delegation",))]
    )
    found, lanes, _ = lab_fill(tmp_path, src)
    assert lanes == [f"fill-exp-file-{n}-20261019T1600Z" for n in range(3)]
    assert not any(f[1] == "lab-fill-generator" for f in found)


def test_an_open_pr_in_the_watcher_state_holds_its_ticket(tmp_path: Path):
    watcher = {
        "prs": {
            "omnimarket#9": {
                "facts": {
                    "state": "OPEN",
                    "title": "feat(OMN-101): receipts",
                    "head_ref": "x",
                }
            },
            "omnimarket#8": {
                "facts": {
                    "state": "MERGED",
                    "title": "feat(OMN-102): glm",
                    "head_ref": "y",
                }
            },
        }
    }
    src = sources(
        tmp_path,
        [
            issue("OMN-101", "receipts", labels=("delegation",)),
            issue("OMN-102", "glm", labels=("delegation",)),
        ],
        experiments=[],
    )
    _, lanes, _ = lab_fill(tmp_path, src, watcher=watcher)
    assert lanes == ["fill-ticket-OMN-102-20261019T1600Z"]


def test_experiment_designs_rotate_least_recently_sent_first():
    sent = {"exp:glm-flash-pr-body": {"lane": "x", "at": "2026-10-19T09:00:00Z"}}
    items, _ = handler.generate_backlog(
        [],
        OPERATOR,
        m4={},
        out_of_scope=set(),
        claimed=set(),
        open_pr=set(),
        readbacks={},
        experiments=EXPERIMENTS,
        sent=sent,
        rule=fake_rule,
        today=FILL_NOW[:10],
    )
    assert [i["key"] for i in items] == ["exp:jev-ci-cause", "exp:glm-flash-pr-body"]
    assert items[0]["kind"] == "experiment-design"
    assert EXPERIMENTS[1]["design"] in items[0]["fact"]


def test_an_unread_ledger_generates_no_ticket_and_says_so(tmp_path: Path):
    src = sources(
        tmp_path,
        [issue("OMN-101", "delegation receipts", labels=("delegation",))],
        ledger_error="live ledger missing",
    )
    found, lanes, _ = lab_fill(tmp_path, src)
    assert lanes == [
        "fill-exp-glm-flash-pr-body-20261019T1600Z",
        "fill-exp-jev-ci-cause-20261019T1600Z",
    ]
    assert (
        "live ledger missing"
        in next(f for f in found if f[1] == "lab-fill-generator")[2]
    )


def test_the_overlay_experiment_designs_are_well_formed():
    designs = handler.read_experiments(lambda: handler.overlay.experiment_designs())
    assert designs == EXPERIMENT_DESIGNS
    assert len(designs) >= 2
    assert len({d["id"] for d in designs}) == len(designs)
    assert all(
        d["title"] and d["design"] and d["repo"] not in ("repo-deploy", "omnidash")
        for d in designs
    )


def test_default_sources_read_the_session_s_files_and_can_be_turned_off(tmp_path: Path):
    env = {
        "ONEX_REGISTRY_ROOT": str(tmp_path),
        "ONEX_LEDGER_PATH": str(tmp_path / "ledger.md"),
        "HOME": str(tmp_path),
        **ENV,
    }
    src = handler.default_sources(FILL_NOW, env)
    assert (
        src.linear_snapshot
        == tmp_path / ".claude_scratch" / "plans-today" / "linear-2026-10-19.json"
    )
    assert (
        src.m4_carriers
        == tmp_path / ".local" / "state" / "omni" / "landing" / "m4-carriers.json"
    )
    assert src.experiments is not None
    assert src.experiments() == EXPERIMENT_DESIGNS
    assert src.scope is not None
    assert src.scope.name == "sweep_scope.json"
    assert src.ledger_tickets is not None
    assert src.ticket_rule is not None
    assert (
        handler.default_sources(FILL_NOW, {**env, "OMNI_LAB_FILL_GENERATE": "off"})
        is None
    )


def test_a_compact_snapshot_generates_no_ticket_and_says_why(tmp_path: Path):
    src = sources(
        tmp_path,
        [issue("OMN-101", "delegation receipts", labels=("delegation",))],
        experiments=[],
    )
    (tmp_path / "linear.json").unlink()
    (tmp_path / "linear.txt").write_text("OMN-101 Todo 2026-10-19T15:00:00Z\n")
    found, lanes, _ = lab_fill(tmp_path, src)
    assert lanes == []
    assert (
        "the compact form with no assignee"
        in next(f for f in found if f[1] == "lab-fill-generator")[2]
    )


FAKE_DRAIN_MAP = """
from dataclasses import dataclass
from datetime import datetime, timezone

@dataclass
class Row:
    ts: str
    rtype: str
    lane: str
    text: str

ROWS = [
    Row("2026-10-19T15:40:00Z", "CLAIM", "fill-ticket-OMN-401-x", "lane=fill-ticket-OMN-401-x | ticket=OMN-17427"),
    Row("2026-10-19T12:00:00Z", "CLAIM", "old-lane", "lane=old-lane | ticket=OMN-402"),
    Row("2026-10-19T12:10:00Z", "TERMINAL", "rb-1", "lane=rb-1 | outcome=blocked | subject=OMN-403 | blocker=the AC3 readback needs the lab"),
    Row("2026-10-19T12:20:00Z", "TERMINAL", "rb-2", "lane=rb-2 | outcome=blocked | subject=OMN-404 | blocker=readback missing"),
    Row("2026-10-19T13:20:00Z", "TERMINAL", "rb-3", "lane=rb-3 | outcome=done | subject=OMN-404 | readback passed"),
    # the ticket cell or the lane's own name carries it when no subject cell does
    Row("2026-10-19T13:30:00Z", "TERMINAL", "bk-omn-405", "lane=bk-omn-405 | ticket=OMN-405 | outcome=blocked | "
        "blocker=AC3 needs the drainer log readback"),
    Row("2026-10-19T13:40:00Z", "TERMINAL", "rb-406", "lane=rb-406 | ticket=OMN-406 | outcome=blocked | "
        "blocker=the readback is missing"),
    # a fill lane's TERMINAL rides on the carrier ticket and names the generated one only in its lane
    Row("2026-10-19T14:00:00Z", "TERMINAL", "fill-readback-OMN-406-20261019T1340Z",
        "lane=fill-readback-OMN-406-20261019T1340Z | ticket=OMN-17427 | outcome=done | readback taken"),
    # a fill lane that could not take the readback either: sending it again would end the same way
    Row("2026-10-19T15:00:00Z", "TERMINAL", "fill-readback-OMN-405-20261019T1440Z",
        "lane=fill-readback-OMN-405-20261019T1440Z | ticket=OMN-17427 | outcome=blocked | "
        "blocker=the readback needs the operator's own host"),
]

def load_grammar(home):
    return None

def load_rows(home, days, as_of, grammar, ledger):
    # the real reader compares aware stamps: a naive as_of raises there
    assert as_of.tzinfo is not None
    return ROWS, []

def open_claims(rows, grammar, horizon):
    assert horizon.tzinfo is not None
    return [r for r in rows if r.rtype == "CLAIM"]
"""


def test_the_ledger_reader_names_live_claims_and_unsettled_readbacks(tmp_path: Path):
    (tmp_path / "drain_map.py").write_text(FAKE_DRAIN_MAP)
    claimed, readbacks = handler.ledger_tickets(
        tmp_path / "ledger.md", tmp_path / "drain_map.py", tmp_path, FILL_NOW
    )
    assert claimed == {"OMN-401", "OMN-17427"}, (
        "a CLAIM whose lane is silent past two hours owns nothing"
    )
    reason = (
        "OMN-405 was tried by a fill lane that blocked again, OMN-406 was taken by one"
    )
    assert list(readbacks) == ["OMN-403"], reason
    assert "AC3 readback" in readbacks["OMN-403"], reason


def test_a_readback_named_only_by_its_ticket_cell_or_lane_is_read(tmp_path: Path):
    # 2026-10-19 lanes bk-omn-19519 and deleg-omn-11187-1005 named their ticket in ticket= and the lane, not subject=
    (tmp_path / "drain_map.py").write_text(
        FAKE_DRAIN_MAP
        + '\nROWS = [r for r in ROWS if not r.lane.startswith("fill-")]\n'
    )
    _, readbacks = handler.ledger_tickets(
        tmp_path / "ledger.md", tmp_path / "drain_map.py", tmp_path, FILL_NOW
    )
    assert sorted(readbacks) == ["OMN-403", "OMN-405", "OMN-406"]


# OMN-20864: ticket admission belongs to omnimarket (omnimarket#4384); a child ticket and a fresh fact gate every fill.
def test_a_lab_fill_dispatch_never_passes_the_umbrella_ticket(tmp_path: Path):
    src = sources(
        tmp_path,
        [issue("OMN-9100", "delegation child", labels=("delegation",))],
        experiments=[],
    )
    found, lanes, calls = lab_fill(tmp_path, src)
    assert len(lanes) == 1
    assert calls[0][calls[0].index("--ticket") + 1] == "OMN-9100"
    brief = Path(calls[0][calls[0].index("--brief") + 1]).read_text()
    assert "(OMN-9100)" in brief.splitlines()[0]
    assert "Ticket: OMN-9100" in brief
    assert "OMN-17427" not in brief.splitlines()[0]
    for name, standing, expected in (
        ("missing", {}, None),
        ("child", {"m4-board-red": "OMN-9101"}, "OMN-9101"),
        ("umbrella", {"m4-board-red": "OMN-17427"}, None),
    ):
        tmp = tmp_path / name
        tmp.mkdir()
        src = sources(tmp, [], experiments=[])
        (tmp / "backlog.json").write_text(
            json.dumps(
                {
                    "standing_tickets": standing,
                    "items": [
                        {"kind": "m4-board-red", "key": "m4:C1", "title": "fix red"}
                    ],
                }
            )
        )
        found, lanes, calls = lab_fill(tmp, src)
        if expected:
            assert len(lanes) == 1
            assert calls[0][calls[0].index("--ticket") + 1] == expected
        else:
            assert lanes == []
            missing = next(f for f in found if f[1] == "lab-fill-backlog")
            assert "no child ticket" in missing[2]
            assert "standing_tickets" in missing[3]
            if name == "umbrella":
                assert "names the umbrella OMN-17427" in missing[2]
    result = fill(
        lines=headroom("host-a"),
        backlog=[{"kind": "m4-board-red", "key": "m4:x"}],
        standing_tickets={},
    )
    assert result.sends == []
    assert result.idle_after == 2
    assert result.skipped[0]["why"].startswith("no child ticket")
    result = fill(
        lines=[*headroom("host-a"), PF_LINE],
        standing_tickets={},
        watcher_state={
            "prs": {
                "repo-private-data#812": {"facts": {"title": "feat(OMN-9102): repair"}}
            }
        },
    )
    assert result.sends[0]["ticket"] == "OMN-9102"


def test_an_item_whose_named_pr_merged_or_closed_is_dropped_with_its_reason(
    tmp_path: Path,
):
    src = sources(tmp_path, [], experiments=[])
    refs = ["omnibase_infra#5148", "omnimarket#8", "omnimarket#7", "omnimarket#9"]
    (tmp_path / "backlog.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "kind": "m4-board-red",
                        "key": f"red:{n}",
                        "ticket": "OMN-9101",
                        **{cell: ref},
                    }
                    for n, (cell, ref) in enumerate(
                        zip(("pr", "title", "fact", "title"), refs, strict=False)
                    )
                ]
            }
        )
    )
    watcher = {
        "repos": {"omnibase_infra": {}},
        "prs": {
            f"omnimarket#{n}": {"facts": {"state": state}}
            for n, state in ((8, "MERGED"), (7, "CLOSED"), (9, "OPEN"))
        },
    }
    found, lanes, _ = lab_fill(tmp_path, src, watcher=watcher)
    assert lanes == ["fill-m4-3-20261019T1600Z"]
    note = next(f for f in found if f[1] == "lab-fill-dropped")
    dropped = json.loads((tmp_path / "state/lab-fill.dropped.json").read_text())
    for n, reason, ref in zip(
        range(3), ("pr-not-open", "pr-merged", "pr-closed"), refs, strict=False
    ):
        assert f"red:{n} {reason} {ref}" in note[2]
        assert dropped[f"red:{n}"]["why"].startswith(f"{reason} {ref}")
        assert dropped[f"red:{n}"]["at"] == FILL_NOW


def test_an_item_whose_fact_is_older_than_48_hours_is_dropped(tmp_path: Path):
    src = sources(tmp_path, [], experiments=[])
    (tmp_path / "backlog.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "kind": "m4-board-red",
                        "key": "old",
                        "ticket": "OMN-9101",
                        "fact": "read 2026-10-15",
                    },
                    {
                        "kind": "m4-board-red",
                        "key": "fresh",
                        "ticket": "OMN-9101",
                        "title": "2026-10-18 readback",
                    },
                    {
                        "kind": "m4-board-red",
                        "key": "explicit",
                        "ticket": "OMN-9101",
                        "fact_at": "2026-10-16T00:00:00Z",
                    },
                ]
            }
        )
    )
    found, lanes, _ = lab_fill(tmp_path, src)
    assert lanes == ["fill-m4-fresh-20261019T1600Z"]
    note = next(f for f in found if f[1] == "lab-fill-dropped")
    assert "old fact-stale 2026-10-15" in note[2]
    assert "explicit fact-stale 2026-10-16T00:00:00Z" in note[2]
    dropped = json.loads((tmp_path / "state/lab-fill.dropped.json").read_text())
    assert set(dropped) == {"old", "explicit"}


def test_an_experiment_with_a_completed_receipt_in_24_hours_is_not_resent(
    tmp_path: Path, monkeypatch
):
    for name, ended_at, status, dropped in (
        ("recent", "2026-10-19T04:00:00Z", "done", True),
        ("old", "2026-10-18T10:00:00Z", "done", False),
        ("error", "2026-10-19T04:00:00Z", "lane-error", False),
    ):
        tmp = tmp_path / name
        tmp.mkdir()
        src = sources(tmp, [], experiments=EXPERIMENTS[:1])
        (tmp / "backlog.json").write_text(
            json.dumps({"standing_tickets": {"experiment-design": "OMN-9101"}})
        )
        lanes = tmp / "lanes"
        monkeypatch.setenv("ONEX_REMOTE_LANE_DIR", str(lanes))
        lane = "fill-exp-glm-flash-pr-body-20261019T0300Z"
        receipt = lanes / lane / "r.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(
            json.dumps({"final": True, "status": status, "ended_at": ended_at})
        )
        found, sent, _ = lab_fill(tmp, src)
        assert bool(sent) is not dropped
        if dropped:
            note = next(f for f in found if f[1] == "lab-fill-dropped")
            assert (
                f"exp:glm-flash-pr-body experiment-done {lane} at {ended_at}" in note[2]
            )
            assert (
                "experiment-done"
                in json.loads((tmp / "state/lab-fill.dropped.json").read_text())[
                    "exp:glm-flash-pr-body"
                ]["why"]
            )


@pytest.mark.parametrize(
    ("case", "lane", "receipt", "dropped"),
    [
        (
            "done-after-fact",
            "fill-defect-nemotron-ultra-rejects-20261019T0617Z",
            {"outcome": "done", "ended_at": "2026-10-19T06:26:05Z"},
            True,
        ),
        (
            "handed-off-after-fact",
            "fill-defect-nemotron-ultra-rejects-20261019T0919Z",
            {"outcome": "handed-off", "ended_at": "2026-10-19T09:43:54Z"},
            True,
        ),
        (
            "done-before-fact",
            "fill-defect-nemotron-ultra-rejects-20261019T0100Z",
            {"outcome": "done", "ended_at": "2026-10-19T02:00:00Z"},
            False,
        ),
        (
            "blocked-after-fact",
            "fill-defect-nemotron-ultra-rejects-20261019T0617Z",
            {"outcome": "blocked", "ended_at": "2026-10-19T06:26:05Z"},
            False,
        ),
        (
            "running",
            "fill-defect-nemotron-ultra-rejects-20261019T0617Z",
            {"final": False, "ended_at": "2026-10-19T06:26:05Z"},
            False,
        ),
        (
            "other-key-sharing-the-prefix",
            "fill-defect-nemotron-ultra-rejects-extra-20261019T0617Z",
            {"outcome": "done", "ended_at": "2026-10-19T06:26:05Z"},
            False,
        ),
    ],
)
def test_a_stored_item_a_fill_lane_answered_after_its_fact_is_not_resent(
    tmp_path: Path, monkeypatch, case, lane, receipt, dropped
):
    # 2026-10-26: deleg:nemotron-ultra-rejects (fact_at 03:05Z) was sent five times in twelve hours, though its 06:17Z lane
    # ended done and its 09:19Z lane handed-off: only a merged PR or a 48-hour fact dropped a stored defect item.
    src = sources(tmp_path, [], experiments=[])
    (tmp_path / "backlog.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "kind": "defect",
                        "key": "deleg:nemotron-ultra-rejects",
                        "repo": "omnimarket",
                        "ticket": "OMN-9101",
                        "fact_at": "2026-10-19T03:05:00Z",
                        "fact": "a rung accepts 8 of 14 answers",
                    }
                ]
            }
        )
    )
    lanes = tmp_path / "lanes"
    monkeypatch.setenv("ONEX_REMOTE_LANE_DIR", str(lanes))
    path = lanes / lane / "r.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"final": True, "status": "done", **receipt}))
    found, sent, _ = lab_fill(tmp_path, src)
    assert bool(sent) is not dropped, case
    if dropped:
        note = next(f for f in found if f[1] == "lab-fill-dropped")
        answer = f"answered {lane} at {receipt['ended_at']}"
        assert f"deleg:nemotron-ultra-rejects {answer}" in note[2]
        assert json.loads((tmp_path / "state/lab-fill.dropped.json").read_text())[
            "deleg:nemotron-ultra-rejects"
        ]["why"].startswith(answer)
    else:
        assert sent == ["fill-defect-nemotron-ultra-rejects-20261019T1600Z"]


def test_the_generator_calls_the_omnimarket_ticket_rule_with_the_facts(tmp_path: Path):
    seen = []

    def rule(facts):
        seen.append(facts)
        return {
            "items": [
                {
                    "id": "OMN-9101",
                    "title": "rule title",
                    "repo": "omnimarket",
                    "status": "Todo",
                    "source": "sprint-current",
                    "carriers": ["C1"],
                    "project": "Sprint",
                    "criteria": ["AC1"],
                }
            ],
            "drops": [
                {"id": "OMN-9102", "reason": "claimed", "detail": "owned"},
                {"id": "OMN-9103", "reason": "no-criteria", "detail": "none"},
            ],
        }

    src = sources(
        tmp_path,
        [
            {**issue("OMN-9101", "snapshot title"), "body": "- [ ] AC1: x"},
            issue("uuid", "bad id"),
        ],
        claimed={"OMN-9102"},
        experiments=[],
        carriers={"C1": ["OMN-9101"]},
    )
    src = replace(src, ticket_rule=rule)
    watcher = {
        "prs": {"omnimarket#4": {"facts": {"state": "OPEN", "title": "OMN-9103: PR"}}}
    }
    found, lanes, calls = lab_fill(tmp_path, src, watcher=watcher)
    assert len(lanes) == 1
    facts = seen[0]
    assert facts["today"] == FILL_NOW[:10]
    assert facts["operator"] == OPERATOR
    assert facts["issues"][0]["description"] == "- [ ] AC1: x"
    assert facts["issues"][0]["status_type"] == "unstarted"
    assert len(facts["issues"]) == 1
    assert facts["m4_carriers"] == {"C1": ["OMN-9101"]}
    assert facts["claimed"] == ["OMN-9102"]
    assert facts["open_pr"] == ["OMN-9103"]
    assert facts["out_of_scope"] == ["repo-deploy", "repo-web-a"]
    brief = Path(calls[0][calls[0].index("--brief") + 1]).read_text()
    assert "rule title" in brief
    assert "sprint-current; M4 carrier of C1" in brief
    assert handler.TICKET_RULE in brief
    note = next(f[2] for f in found if f[1] == "lab-fill-generator")
    assert all(
        text in note
        for text in (
            "tickets=1",
            "bad-id=1",
            "claimed=1",
            "no-criteria=1",
            handler.TICKET_RULE,
        )
    )

    def broken(facts):
        raise ValueError("broken market rule")

    generated, note = handler.read_generated(
        replace(src, ticket_rule=broken), watcher, {}, FILL_NOW
    )
    assert generated == []
    assert "ticket rule unread: broken market rule" in note
    generated, note = handler.read_generated(
        replace(src, ticket_rule=None), watcher, {}, FILL_NOW
    )
    assert generated == []
    assert "rule-unavailable=2" in note
    assert "ticket rule unread: no ticket rule supplied" in note
    generated, note = handler.read_generated(
        replace(
            src,
            experiments=lambda: EXPERIMENTS[:1],
            ticket_rule=broken,
            ledger_tickets=lambda: (set(), {"OMN-9104": "missing readback"}),
        ),
        watcher,
        {},
        FILL_NOW,
    )
    assert [i["kind"] for i in generated] == ["readback", "experiment-design"]
    assert "ticket rule unread: broken market rule" in note


def test_omnimarket_ticket_rule_runs_the_merged_rule():
    plan = handler.omnimarket_ticket_rule()(
        {
            "today": "2026-10-19",
            "operator": OPERATOR,
            "issues": [
                {
                    "id": "OMN-9101",
                    "title": "Sprint task",
                    "status": "Todo",
                    "status_type": "unstarted",
                    "assignee": OPERATOR,
                    "labels": [],
                    "project": "Sprint 2026-10-19 → 2026-10-25",
                    "repo": "omnimarket",
                    "pillar": None,
                    "description": "- [ ] AC1: x",
                }
            ],
            "m4_carriers": {},
            "claimed": [],
            "open_pr": [],
            "out_of_scope": ["repo-deploy"],
        }
    )
    assert [item["id"] for item in plan["items"]] == ["OMN-9101"]


@pytest.mark.parametrize(
    ("item", "watcher", "expected"),
    [
        ({"title": "old 2026-10-10, new 2026-10-18"}, None, None),
        (
            {"fact_at": "2026-10-17", "title": "explicit day ends at midnight"},
            None,
            None,
        ),
        ({"fact_at": "2026-10-17T16:00:00Z"}, None, None),
        (
            {"fact_at": "2026-10-17T15:59:59Z", "title": "new 2026-10-19"},
            None,
            "fact-stale",
        ),
        ({"fact": "unknown timestamp"}, None, None),
        ({"title": "omnimarket#1"}, None, None),
        ({"title": "omnimarket#1"}, {"repos": {}, "prs": {}}, None),
        (
            {"key": "work:OmniNode-ai/omnimarket#1"},
            {"repos": {"omnimarket": {}}, "prs": {}},
            "pr-not-open omnimarket#1",
        ),
    ],
)
def test_stale_fact_boundaries_use_the_newest_stamp_and_only_watched_repositories(
    item, watcher, expected
):
    reason = handler.stale_reason(
        {**item, "_verify": "stored"},
        now=handler.datetime.fromisoformat(FILL_NOW),
        watcher_state=watcher,
        receipts={},
    )
    # a generated item is checked for completed receipts only: no PR or fact-age drop
    assert (
        handler.stale_reason(
            {**item, "_verify": "receipt"},
            now=handler.datetime.fromisoformat(FILL_NOW),
            watcher_state=watcher,
            receipts={},
        )
        is None
    )
    assert reason.startswith(expected) if expected else reason is None


def test_completed_receipts_include_the_sent_lane_and_exclude_jobs_failed_outcomes_and_nonfinals(
    tmp_path: Path,
):
    now = FILL_NOW
    items = [
        {"kind": "experiment-design", "key": f"exp:{key}"}
        for key in ("recorded", "job", "blocked", "running", "handed-off")
    ]
    for _key, lane, extra, filename in (
        ("recorded", "custom-lane", {}, "r.json"),
        ("job", "fill-exp-job-1", {}, "r.job.json"),
        ("blocked", "fill-exp-blocked-1", {"outcome": "blocked"}, "r.json"),
        ("running", "fill-exp-running-1", {"final": False}, "r.json"),
        ("handed-off", "fill-exp-handed-off-1", {"outcome": "handed-off"}, "r.json"),
    ):
        path = tmp_path / lane / filename
        path.parent.mkdir()
        path.write_text(
            json.dumps(
                {
                    "final": True,
                    "status": "done",
                    "ended_at": "2026-10-19T04:00:00Z",
                    **extra,
                }
            )
        )
    completed = handler.completed_experiments(
        items, {"exp:recorded": {"lane": "custom-lane"}}, now=now, lanes=tmp_path
    )
    assert completed == {
        "exp:recorded": "custom-lane at 2026-10-19T04:00:00Z",
        "exp:handed-off": "fill-exp-handed-off-1 at 2026-10-19T04:00:00Z",
    }
    result = fill(
        now=now,
        lines=headroom("host-a"),
        backlog=[{**items[0], "ticket": "OMN-9101"}],
        completed=completed,
    )
    assert result.sends == []
    assert result.dropped[0]["why"].startswith("experiment-done custom-lane")


@pytest.mark.parametrize("mode", ["merge", "read-only", "write-error"])
def test_dropped_items_are_not_sent_and_recording_is_read_only_safe_and_failure_tolerant(
    tmp_path: Path, monkeypatch, mode
):
    src = sources(tmp_path, [], experiments=[])
    backlog = tmp_path / "backlog.json"
    backlog.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "kind": "m4-board-red",
                        "key": f"stale:{n}",
                        "ticket": "OMN-9101",
                        "fact_at": "2026-10-15",
                    }
                    for n in range(8)
                ]
            }
        )
    )
    state = tmp_path / "state"
    state.mkdir()
    path = state / "lab-fill.dropped.json"
    if mode == "merge":
        path.write_text(
            json.dumps({"prior": {"why": "prior reason", "at": "2026-10-18T00:00:00Z"}})
        )
    if mode == "write-error":

        def refuse(*args):
            raise OSError("not writable")

        monkeypatch.setattr(handler, "write_sent", refuse)
    found = (
        handler.HandlerLabFill()
        .handle(
            models.ModelLabFillRequest(
                FILL_NOW,
                POOL3,
                [FREE3],
                None,
                state,
                str(backlog),
                stub_runner(tmp_path),
                sources=src,
                read_only=mode == "read-only",
            )
        )
        .found
    )
    assert not (tmp_path / "calls.jsonl").exists()
    note = next(f for f in found if f[0] == "note" and f[1] == "lab-fill-dropped")
    assert "n=8" in note[2]
    assert "and 2 more" in note[2]
    if mode == "merge":
        recorded = json.loads(path.read_text())
        assert set(recorded) == {"prior", *(f"stale:{n}" for n in range(8))}
        assert recorded["prior"]["why"] == "prior reason"
        assert recorded["stale:0"]["at"] == FILL_NOW
    else:
        assert not path.exists()
    if mode == "write-error":
        assert any(
            f[0] == "unknown" and f[1] == "lab-fill-dropped" and "not writable" in f[2]
            for f in found
        )


def test_no_opus_claude_fill_brief_routes_sonnet():
    result = fill(hosts=[host("host-a", 2, 0)])
    assert result.sends
    for send in result.sends:
        assert send["model"] == "sonnet"
        assert "ROUTE: model=sonnet effort=high" in send["brief"]
        assert "model=opus" not in send["brief"]


@pytest.mark.parametrize("required", ["opus", "claude-opus", "claude_opus", " OPUS "])
def test_no_opus_required_engine_is_skipped_with_ruling(required):
    result = fill(
        lines=headroom("host-a"),
        backlog=[
            {
                "kind": "ticket",
                "key": "opus-only",
                "repo": "omnimarket",
                "ticket": "OMN-21396",
                "required_engine": required,
            }
        ],
    )
    assert result.sends == []
    assert result.idle_after == 2
    assert result.skipped[0]["key"] == "opus-only"
    assert (
        "RULING 2026-10-28T13:32:09Z lane=orchestrator-31d9" in result.skipped[0]["why"]
    )


@pytest.mark.parametrize(
    "declaration",
    [
        "Required engine: opus",
        "Required model: claude-opus",
        "Required engine: claude_opus",
    ],
)
def test_no_opus_brief_requirement_is_skipped_with_ruling(declaration):
    result = fill(
        lines=headroom("host-a"),
        backlog=[
            {
                "kind": "ticket",
                "key": "brief-opus",
                "repo": "omnimarket",
                "ticket": "OMN-21396",
                "brief": declaration,
            }
        ],
    )
    assert result.sends == []
    assert "RULING 2026-10-28T13:32:09Z" in result.skipped[0]["why"]


def test_no_opus_skip_preserves_slot_for_the_next_item():
    result = fill(
        lines=headroom("host-a"),
        hosts=[host("host-a", 1, 0)],
        backlog=[
            {
                "kind": "ticket",
                "key": "opus-only",
                "ticket": "OMN-21396",
                "required_engine": "opus",
            },
            {
                "kind": "ticket",
                "key": "claude-work",
                "ticket": "OMN-21396",
                "required_engine": "claude",
            },
        ],
    )
    assert [(s["key"], s["model"]) for s in result.sends] == [("claude-work", "sonnet")]
    assert len(result.skipped) == 1
    assert result.idle_after == 0


@pytest.mark.parametrize(
    "required", ["claude", "sonnet", "claude-sonnet", "claude_sonnet"]
)
def test_no_opus_claude_requirement_waits_instead_of_falling_to_codex(required):
    result = fill(
        lines=headroom("host-a"),
        hosts=[host("host-a", 0, 1)],
        backlog=[
            {
                "kind": "ticket",
                "key": "claude-only",
                "ticket": "OMN-21396",
                "required_engine": required,
            }
        ],
    )
    assert result.sends == []
    assert result.skipped == [
        {"key": "claude-only", "why": "claude-required: waiting for Claude quota"}
    ]
    assert result.idle_after == 1


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_no_opus_quoted_requirement_is_not_a_pin(fence):
    result = fill(
        lines=headroom("host-a"),
        backlog=[
            {
                "kind": "ticket",
                "key": "quoted",
                "ticket": "OMN-21396",
                "brief": f"{fence}\nRequired engine: opus\n{fence}\nUse the active routing policy.",
            }
        ],
    )
    assert len(result.sends) == 1
    assert result.sends[0]["model"] == "codex"
    assert result.skipped == []


def test_no_opus_lab_fill_runner_gets_sonnet_and_reports_opus_skip(tmp_path: Path):
    src = sources(tmp_path, [], experiments=[])
    (tmp_path / "backlog.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "kind": "ticket",
                        "key": "opus-only",
                        "ticket": "OMN-21396",
                        "required_engine": "opus",
                    },
                    {
                        "kind": "ticket",
                        "key": "claude-work",
                        "ticket": "OMN-21396",
                        "required_engine": "claude",
                    },
                ]
            }
        )
    )
    found, lanes, calls = lab_fill(tmp_path, src)
    assert len(lanes) == len(calls) == 1
    call = calls[0]
    assert call[call.index("--model") + 1] == "sonnet"
    brief = Path(call[call.index("--brief") + 1]).read_text()
    assert "ROUTE: model=sonnet effort=high" in brief
    assert "Required engine: claude" in brief
    missing = next(row for row in found if row[1] == "lab-fill-backlog")
    assert "opus-only" in missing[2]
    assert "RULING 2026-10-28T13:32:09Z lane=orchestrator-31d9" in missing[2]


def test_retired_plugin_registry_environment_keeps_snapshot_sources(tmp_path):
    root = tmp_path / "registry"
    env = {
        "OMNI_HOME": str(root),
        "OMNI_PLUGIN_ROOT": str(tmp_path / "plugin"),
        "HOME": str(tmp_path / "home"),
    }
    source = handler.default_sources(FILL_NOW, env)
    assert source is not None
    assert (
        source.linear_snapshot
        == root / ".claude_scratch/plans-today" / f"linear-{FILL_NOW[:10]}.json"
    )
    canonical = handler.default_sources(
        FILL_NOW, {**env, "ONEX_REGISTRY_ROOT": str(root)}
    )
    assert canonical is not None
    assert canonical.linear_snapshot == source.linear_snapshot


def test_no_opus_brief_requirement_cannot_be_hidden_by_an_item_requirement():
    result = fill(
        lines=headroom("host-a"),
        backlog=[
            {
                "kind": "ticket",
                "key": "contradiction",
                "ticket": "OMN-21396",
                "required_engine": "claude",
                "brief": "Required engine: opus",
            }
        ],
    )
    assert result.sends == []
    assert result.idle_after == 2
    assert "RULING 2026-10-28T13:32:09Z" in result.skipped[0]["why"]


def test_glm_queue_entries_preserve_the_existing_claude_reservation_accounting():
    reserved = handler.queue_reservations(
        entries=[{"lane": "queued-glm", "model": "glm"}],
        started=[],
        request={"lab_headroom": {"hosts": [host("host-a", 2, 0)]}},
        pin_of=lambda _entry: None,
    )
    assert reserved == {"claude": 1, "codex": 0}
