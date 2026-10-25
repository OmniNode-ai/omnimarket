"""A stall the tick detects gets an actor, not an owner on paper (OMN-17427).

Replays of 2026-10-19. A plugin repository: main red (validate-plugin, validator-tests) about 10 hours, plugin version
bump #1019 open 625 minutes, every tick printing ``the controller owns it (owned by landing-L11740 ...)`` while the
controller's decisions.jsonl read claim-refused ``cause-held-same-base`` after an attempt whose result never ran
(workers/11542/result.json kind=blocked reason=lab_capacity_unavailable). public-repo-a: the Hostile Reviewer red on public-repo-a#31-34
came from the pull_request_target workflow on public-repo-a's main, which no PR head changes, yet a process-fix driver lane would
be named for it. Nothing here reaches GitHub, a real runner, a real ledger or a real controller.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

stuck = importlib.import_module(
    "omnimarket.nodes.node_stuck_owner_actor_effect.handler"
)
models = importlib.import_module(
    "omnimarket.nodes.node_stuck_owner_actor_effect.models"
)

NOW = datetime(2026, 10, 20, 0, 5, 7, tzinfo=UTC)
CAUSE = "cause:OmniNode-ai/plugin-repo-a:5f2a0c19d7e4"
BASE_SHA = "64989895fe6c1360b42a873d340aa1edc9343d8b"
BUMP = "OmniNode-ai/plugin-repo-a#1019"
OWNED_LINE = (
    "MISSING repo-status:plugin-repo-a | plugin-repo-a: 4 open, 0 merged in 120 min; "
    "merge_queue_ejecting: ejected for base-branch red validate-plugin | FIX: the controller owns it (owned by "
    f"landing-L11740 cause={CAUSE}): read /omni:landing-controller status; the session starts no fix work for it"
)
HELD = (
    "cause-held-same-base: the last attempt (landing-L11542 2026-10-19T15:02:11Z blocked) failed on base "
    "64989895fe; no worker until it moves"
)


def ts(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def landing(
    tmp_path: Path, *rows: dict, results: dict | None = None, leases: list | None = None
) -> Path:
    d = tmp_path / "landing"
    d.mkdir(parents=True, exist_ok=True)
    default = [
        {
            "lease_id": 11740,
            "cause": CAUSE,
            "repo": "plugin-repo-a",
            "revoked": False,
            "checks": ["validate-plugin", "validator-tests (1/4)"],
        },
        {
            "lease_id": 11815,
            "cause": CAUSE,
            "repo": "plugin-repo-a",
            "revoked": False,
            "checks": ["validate-plugin"],
        },
    ]
    (d / "state.json").write_text(
        json.dumps({"leases": default if leases is None else leases})
    )
    (d / "decisions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    for lid, res in (results or {}).items():
        (d / "workers" / str(lid)).mkdir(parents=True)
        (d / "workers" / str(lid) / "result.json").write_text(json.dumps(res))
    return d


def replay_rows() -> list[dict]:
    """Tick 3290's history: one spawn whose worker never ran, then the hold that refused every later claim."""
    return [
        {
            "ts": "2026-10-19T15:02:11Z",
            "tick": 3101,
            "kind": "dispatch_worker",
            "subject": CAUSE,
            "lease_id": 11542,
            "outcome": "spawned",
            "detail": "pid=4711",
        },
        {
            "ts": ts(2),
            "tick": 3290,
            "kind": "dispatch_worker",
            "subject": CAUSE,
            "lease_id": 11815,
            "outcome": "claim-refused",
            "detail": HELD,
        },
    ]


NEVER_RAN = {
    11542: {"kind": "blocked", "reason": "lab_capacity_unavailable", "pr": CAUSE}
}


def watcher(red_minutes: float = 600, bump_minutes: float = 625) -> dict:
    rows = [
        [
            "validate-plugin",
            "completed",
            "failure",
            ts(red_minutes + 20),
            ts(red_minutes),
        ],
        [
            "validator-tests (1/4)",
            "completed",
            "failure",
            ts(red_minutes + 20),
            ts(red_minutes),
        ],
        [
            "Canonical File Shape (OMN-20304)",
            "completed",
            "success",
            ts(red_minutes + 20),
            ts(red_minutes),
        ],
    ]
    return {
        "branches": {
            "plugin-repo-a@main": {
                "repo": "plugin-repo-a",
                "branch": "main",
                "default": True,
                "sha": BASE_SHA,
                "checks": {"rows": rows},
            }
        },
        "settings": {
            "plugin-repo-a": {
                "branches": {
                    "main": {
                        "contexts": ["validate-plugin", "validator-tests (1/4)"],
                        "rule_contexts": [],
                    }
                }
            }
        },
        "prs": {
            BUMP: {
                "facts": {
                    "state": "OPEN",
                    "head_ref": "automation/omn-20497-plugin-version-bump",
                    "created_at": ts(bump_minutes),
                    "head_sha": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
                },
                "ci": {"red": ["validate-plugin"]},
            }
        },
        "repos": {"plugin-repo-a": {"default_branch": "main"}},
    }


class Runner:
    def __init__(self, ok: bool = True) -> None:
        self.calls: list[tuple[str, str, list[str], str]] = []
        self.ok = ok

    def __call__(
        self, lane: str, repo: str, causes: list[str], text: str
    ) -> tuple[bool, str]:
        self.calls.append((lane, repo, causes, text))
        return self.ok, f"exit {0 if self.ok else 1}: DETACHED lane={lane}"


def act(
    tmp_path: Path,
    land: Path,
    runner=None,
    *,
    lines=(OWNED_LINE,),
    w=None,
    ledger=None,
    now=NOW,
    scope=True,
):
    return stuck.HandlerStuckOwnerActor().handle(
        models.ModelStuckOwnerRequest(
            list(lines),
            watcher() if w is None else w,
            land,
            ledger,
            now,
            tmp_path / "state",
            runner,
            "orchestrator-test",
            scope=frozenset({"plugin-repo-a", "public-repo-a"})
            if scope is True
            else scope,
        )
    )


# ---------------------------------------------------------------------------------------------- the acting rule


def test_claim_refused_after_an_attempt_that_never_ran_is_not_acting(tmp_path) -> None:
    land = landing(tmp_path, *replay_rows(), results=NEVER_RAN)
    acting, evidence = stuck.judge(
        stuck.read_decisions(land),
        stuck.read_leases(land),
        land,
        CAUSE,
        NOW,
        11740,
        models.ACTING_WINDOW,
    )
    assert not acting
    assert "claim-refused" in evidence
    assert "cause-held-same-base" in evidence
    assert "tick 3290" in evidence


def test_a_spawn_whose_worker_never_ran_is_not_acting(tmp_path) -> None:
    land = landing(
        tmp_path,
        {
            "ts": ts(10),
            "tick": 3300,
            "kind": "dispatch_worker",
            "subject": CAUSE,
            "lease_id": 11542,
            "outcome": "spawned",
        },
        results=NEVER_RAN,
    )
    acting, evidence = stuck.judge(
        stuck.read_decisions(land),
        stuck.read_leases(land),
        land,
        CAUSE,
        NOW,
        None,
        models.ACTING_WINDOW,
    )
    assert not acting
    assert "lab_capacity_unavailable" in evidence
    assert "never ran" in evidence


def test_a_spawn_in_the_last_30_minutes_is_acting(
    tmp_path,
) -> None:  # the positive control of the rule
    land = landing(
        tmp_path,
        {
            "ts": ts(10),
            "tick": 3300,
            "kind": "dispatch_worker",
            "subject": CAUSE,
            "lease_id": 11900,
            "outcome": "spawned",
        },
    )
    assert stuck.judge(
        stuck.read_decisions(land),
        stuck.read_leases(land),
        land,
        CAUSE,
        NOW,
        None,
        models.ACTING_WINDOW,
    )[0]


def test_an_older_spawn_is_acting_only_while_its_lease_is_live(tmp_path) -> None:
    row = {
        "ts": ts(50),
        "tick": 3280,
        "kind": "dispatch_worker",
        "subject": CAUSE,
        "lease_id": 11740,
        "outcome": "spawned",
    }
    land = landing(tmp_path, row)
    assert stuck.judge(
        stuck.read_decisions(land),
        stuck.read_leases(land),
        land,
        CAUSE,
        NOW,
        None,
        models.ACTING_WINDOW,
    )[0]
    land = landing(tmp_path / "x", row, results={11740: {"kind": "pushed_fix"}})
    acting, evidence = stuck.judge(
        stuck.read_decisions(land),
        stuck.read_leases(land),
        land,
        CAUSE,
        NOW,
        None,
        models.ACTING_WINDOW,
    )
    assert not acting
    assert "50m ago" in evidence


def test_a_lease_with_no_decision_row_is_not_acting(tmp_path) -> None:
    land = landing(tmp_path)
    acting, evidence = stuck.judge(
        [], stuck.read_leases(land), land, CAUSE, NOW, 11740, models.ACTING_WINDOW
    )
    assert not acting
    assert "no decisions.jsonl row" in evidence


# ------------------------------------------------------------------------------ the plugin-repo-a replay


def test_replay_plugin_repo_owned_line_is_unowned_not_acting_and_one_cause_lane_is_dispatched(
    tmp_path,
) -> None:
    land = landing(tmp_path, *replay_rows(), results=NEVER_RAN)
    runner = Runner()
    out = act(tmp_path, land, runner)
    assert len(runner.calls) == 1, runner.calls
    lane, repo, causes, text = runner.calls[0]
    assert repo == "plugin-repo-a"
    assert lane.startswith("cause-stuck-plugin-repo-a-")
    assert causes == [
        "plugin-repo-a:validate-plugin",
        "plugin-repo-a:validator-tests (1/4)",
    ]
    assert "## Task" in text
    assert "plugin version bump plugin-repo-a#1019 open 625m" in text
    line = out.lines[0]
    assert line.startswith("MISSING repo-status:plugin-repo-a | ")
    assert "the controller owns it" not in line
    assert "UNOWNED-NOT-ACTING (landing-L11740" in line
    assert "cause-held-same-base" in line
    assert f"FIX: cause lane {lane} carries plugin-repo-a" in line
    notes = [f for f in out.found if f[0] == "note"]
    assert {f[1] for f in notes} == {
        "stuck-owner:plugin-repo-a@main",
        "stuck-owner:plugin-repo-a#1019",
    }
    assert all(f"the tick dispatched cause lane {lane}" in f[2] for f in notes)


def test_replay_the_next_tick_dispatches_nothing_for_the_same_cause_and_base(
    tmp_path,
) -> None:
    land = landing(tmp_path, *replay_rows(), results=NEVER_RAN)
    first, second = Runner(), Runner()
    act(tmp_path, land, first)
    out = act(tmp_path, land, second, now=NOW + timedelta(minutes=30))
    assert len(first.calls) == 1
    assert second.calls == []
    assert all("not dispatched again" in f[2] for f in out.found), out.found
    lane = first.calls[0][0]
    assert f"FIX: cause lane {lane} carries plugin-repo-a" in out.lines[0]


def test_a_new_base_tip_still_red_gets_its_own_lane(tmp_path) -> None:
    land = landing(tmp_path, *replay_rows(), results=NEVER_RAN)
    first, second = Runner(), Runner()
    act(tmp_path, land, first)
    moved = watcher()
    moved["branches"]["plugin-repo-a@main"]["sha"] = "f" * 40
    act(tmp_path, land, second, w=moved, now=NOW + timedelta(minutes=90))
    assert len(second.calls) == 1
    assert second.calls[0][2] == first.calls[0][2]


def test_a_read_only_tick_names_the_lane_and_records_nothing(tmp_path) -> None:
    land = landing(tmp_path, *replay_rows(), results=NEVER_RAN)
    out = act(tmp_path, land, None)
    missing = [f for f in out.found if f[0] == "missing"]
    assert len(missing) == 2
    assert all("read-only tick: dispatch cause lane" in f[3] for f in missing)
    assert all(f[2].startswith("UNOWNED-NOT-ACTING: ") for f in missing)
    runner = Runner()
    act(tmp_path, land, runner)
    assert len(runner.calls) == 1


def test_a_failed_dispatch_is_missing_and_retried_next_tick(tmp_path) -> None:
    land = landing(tmp_path, *replay_rows(), results=NEVER_RAN)
    out = act(tmp_path, land, Runner(ok=False))
    assert [f[0] for f in out.found] == ["missing", "missing"]
    assert "failed" in out.found[0][3]
    runner = Runner()
    act(tmp_path, land, runner, now=NOW + timedelta(minutes=5))
    assert len(runner.calls) == 1


def test_an_acting_controller_keeps_its_line_and_gets_no_lane(
    tmp_path,
) -> None:  # positive control of the replay
    land = landing(
        tmp_path,
        {
            "ts": ts(5),
            "tick": 3299,
            "kind": "dispatch_worker",
            "subject": CAUSE,
            "lease_id": 11740,
            "outcome": "spawned",
        },
        {
            "ts": ts(5),
            "tick": 3299,
            "kind": "dispatch_worker",
            "subject": BUMP,
            "lease_id": 11741,
            "outcome": "spawned",
        },
    )
    runner = Runner()
    out = act(tmp_path, land, runner)
    assert runner.calls == []
    assert out.lines == [OWNED_LINE]
    assert out.promoted == []
    assert all(
        f[0] == "note"
        and ("owned by landing-L11740" in f[2] or "controller is acting" in f[2])
        for f in out.found
    ), out.found


def test_a_live_cause_claim_owns_the_red_base(tmp_path) -> None:
    land = landing(tmp_path, *replay_rows(), results=NEVER_RAN)
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        f"{ts(200)} | CLAIM | lane=cause-occ-internal-base-red-0020 | ticket=OMN-17427 | "
        "cause=plugin-repo-a:validate-plugin | fix\n"
        f"{ts(10)} | STATUS | lane=cause-occ-internal-base-red-0020 | ticket=OMN-17427 | pushed\n"
    )
    runner = Runner()
    out = act(tmp_path, land, runner, w={**watcher(), "prs": {}}, ledger=ledger)
    assert runner.calls == []
    assert any(
        "live cause CLAIM cause-occ-internal-base-red-0020" in f[2] for f in out.found
    ), out.found
    ledger.write_text(
        ledger.read_text().replace(ts(10), ts(60))
    )  # silent 60 minutes: the CLAIM owns nothing
    act(tmp_path, land, runner, w={**watcher(), "prs": {}}, ledger=ledger)
    assert len(runner.calls) == 1


def test_a_red_under_two_hours_and_a_young_bump_get_no_lane(tmp_path) -> None:
    land = landing(tmp_path, *replay_rows(), results=NEVER_RAN)
    runner = Runner()
    out = act(tmp_path, land, runner, w=watcher(red_minutes=60, bump_minutes=60))
    assert runner.calls == []
    assert out.found == []
    assert out.lines[0].startswith("MISSING ")
    assert "UNOWNED-NOT-ACTING" in out.lines[0]


def test_only_a_required_context_red_in_the_landing_scope_gets_a_lane(tmp_path) -> None:
    """2026-10-20 live read: eight bases carried reds, mostly scheduled workflows and out-of-scope repositories."""
    land = landing(tmp_path, *replay_rows(), results=NEVER_RAN)
    advisory = watcher()
    advisory["settings"]["plugin-repo-a"]["branches"]["main"]["contexts"] = [
        "Canonical File Shape (OMN-20304)"
    ]
    advisory["prs"] = {}
    runner = Runner()
    assert act(tmp_path, land, runner, w=advisory).found == []
    assert runner.calls == []
    assert (
        act(tmp_path / "s", land, runner, scope=frozenset({"omnimarket"})).found == []
    )
    assert runner.calls == []
    assert act(tmp_path / "u", land, runner, scope=None).found == []
    assert runner.calls == []
    act(tmp_path / "ok", land, runner)  # the positive control
    assert len(runner.calls) == 1


def test_an_owned_note_line_is_promoted_to_missing(tmp_path) -> None:
    land = landing(tmp_path, *replay_rows(), results=NEVER_RAN)
    note = f"NOTE floor:plugin-repo-a | 0 merges in 6h | owned by landing-L11740 cause={CAUSE}"
    out = act(tmp_path, land, Runner(), lines=(note,))
    assert out.promoted == ["floor:plugin-repo-a"]
    assert out.lines[0].startswith("MISSING floor:plugin-repo-a")


def test_unreadable_controller_state_judges_nothing_and_dispatches_nothing(
    tmp_path,
) -> None:
    runner = Runner()
    out = act(tmp_path, tmp_path / "absent", runner)
    assert runner.calls == []
    assert out.lines == [OWNED_LINE]
    assert out.found
    assert "unread" in out.found[0][2]


# ------------------------------------------------------------------------------------------- the public-repo-a replay

PUBLIC_PR = "public-repo-a#33"
PUBLIC_HEAD = "9c1e5b7a2d4f6081e3a5c7b9d1f3a5c7e9b1d3f5"
PUBLIC_RED = ["Hostile Review Gate", "Hostile Reviewer (adversarial gate)"]
DRIVER = (
    f"driver lane pf-drive-public-repo-a-33: dispatch it with --priority-class process-fix --pr {PUBLIC_PR} to carry "
    f"{PUBLIC_PR} to merged"
)
WORKFLOW = """name: Hostile Review Gate
on:
  pull_request_target:
    types: [opened, synchronize]
jobs:
  reviewer:
    name: Hostile Reviewer (adversarial gate)
    runs-on: ubuntu-latest
  gate:
    name: Hostile Review Gate
    needs: reviewer
"""


def public_watcher(red=PUBLIC_RED) -> dict:
    return {
        "prs": {
            PUBLIC_PR: {
                "facts": {"state": "OPEN", "head_sha": PUBLIC_HEAD, "base": "main"},
                "ci": {"red": red},
            }
        },
        "repos": {"public-repo-a": {"default_branch": "main"}},
    }


def gate(found, *, w=None, ledger=None, target=None):
    return (
        stuck.HandlerDriverGate()
        .handle(
            models.ModelDriverGateRequest(
                found, public_watcher() if w is None else w, ledger, NOW, target
            )
        )
        .found
    )


FINDING = (
    "missing",
    f"process-fix:{PUBLIC_PR}",
    f"handed off 2026-10-19T20:40:00Z by pf-origin; red ({', '.join(PUBLIC_RED)}), "
    f"unmerged 200m after hand-off at head {PUBLIC_HEAD[:12]}",
    DRIVER,
)


def test_replay_public_repo_a_base_inherited_pull_request_target_red_names_no_driver_lane() -> (
    None
):
    names = stuck._target_names(WORKFLOW)
    assert names == {"Hostile Review Gate", "Hostile Reviewer (adversarial gate)"}
    [out] = gate(
        [FINDING],
        target=lambda repo, base: (
            names if (repo, base) == ("public-repo-a", "main") else None
        ),
    )
    assert out[0] == "note"
    assert out[3] == ""
    assert "base-inherited pull_request_target" in out[2]
    assert "no driver lane" in out[2]


def test_a_red_the_pr_can_change_keeps_its_driver_lane() -> (
    None
):  # the positive control
    names = stuck._target_names(WORKFLOW)
    [out] = gate(
        [FINDING],
        w=public_watcher([*PUBLIC_RED, "tests"]),
        target=lambda _repo, _base: names,
    )
    assert out == FINDING
    [out] = gate([FINDING], target=lambda _repo, _base: set())
    assert out == FINDING
    [out] = gate(
        [FINDING], target=lambda _repo, _base: None
    )  # unread workflows: the driver lane stands
    assert out == FINDING
    assert (
        stuck._target_names(WORKFLOW.replace("pull_request_target", "pull_request"))
        is None
    )


def test_a_head_an_earlier_driver_handed_off_names_no_driver_lane(tmp_path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        f"{ts(90)} | TERMINAL | lane=pf-drive-public-repo-a-33 | ticket=OMN-17427 | outcome=handed-off | "
        f"pr=OmniNode-ai/{PUBLIC_PR} | head={PUBLIC_HEAD[:12]} | friction=none | handed\n"
    )
    [out] = gate([FINDING], ledger=ledger)
    assert out[0] == "note"
    assert "already handed off by pf-drive-public-repo-a-33" in out[2]
    assert out[3] == ""
    moved = public_watcher()
    moved["prs"][PUBLIC_PR]["facts"]["head_sha"] = "0" * 40
    [out] = gate([FINDING], w=moved, ledger=ledger)
    assert out == FINDING


def test_a_driver_msg_to_the_controller_counts_as_a_hand_off(tmp_path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        f"{ts(90)} | MSG | from=pf-drive-public-repo-a-33 | to=landing-controller | id=x | ticket=OMN-17427 | "
        f"pr=OmniNode-ai/{PUBLIC_PR} | head={PUBLIC_HEAD} | needs=land | land it\n"
    )
    [out] = gate([FINDING], ledger=ledger)
    assert out[0] == "note"


# ----------------------------------------------------------------------------------------- the tick's wiring
