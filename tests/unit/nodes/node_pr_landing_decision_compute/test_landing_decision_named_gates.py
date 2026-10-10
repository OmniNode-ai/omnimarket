# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A green, clean, unheld PR is merged, or the decision names what holds it.

omnimemory#557 (head 83ddb778b5) sat green, CLEAN and unheld from 2026-10-02
with a bare ``gate`` suspension: the controller's sidecar recorded kind
``gate`` for it and four ops_gated PRs, and no decision row named the
gate. Selectors (one ``-k`` each):

* ``unnamed``: a gate suspension carries named reasons, or the facts are refused;
* ``omnimemory_557``: the PR as read is held by the release train and the
  decision names it; the same head held only by a gate whose premise is a red
  head is merged once that gate is older than two ticks;
* ``sidecar_2026_10_10``: every one of the five gated PRs is named by its gate;
* ``omnibase_infra_4798``: green, CLEAN, a worker spawned, no arm: the decision
  names the lease (or the lab gate) that holds the merge;
* ``fleet``: fifteen green, CLEAN, unarmed PRs each get a merge or one named
  skip, and a decision missing one is reported as a gap.
* ``generated``: two thousand seeded random fleets (leases, records, a companion,
  the token, observe-only, drain and fixer holds, parents, gates) raise no coverage
  gap, and every skip reason, a merge and a released gate occur among them.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_pr_landing_decision_compute.handlers import (
    handler_pr_landing_decision,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    HandlerPrLandingDecision,
    LandingCoverageError,
    decide_landing,
    land_coverage_gaps,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    STALE_WHEN_GREEN_GATE_REASONS,
    EnumLandingActionKind,
    EnumLandingGateReason,
    EnumLandingLandSkipReason,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingDecision,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
    ModelLandingPrFacts,
)

FIXTURE = Path(__file__).parent / "fixtures" / "gate_since_2026_10_10.json"
OMNIMEMORY_557 = "OmniNode-ai/omnimemory#557"
HEAD_557 = "83ddb778b5afed4f45c0b3a1edb85f00a87abbde"
INFRA_4798 = "OmniNode-ai/omnibase_infra#4798"
HEAD_4798 = "b9d41b41f0a119060ad3e289c5be22cfb5387e5d"
OBSERVED = datetime(2026, 10, 10, 4, 15, tzinfo=UTC)
TICK = timedelta(seconds=300)


def _facts(prs: list[dict[str, Any]], **extra: Any) -> ModelLandingFacts:
    return ModelLandingFacts.model_validate(
        {
            "tick": extra.pop("tick", 2),
            "observed_at": extra.pop("observed_at", OBSERVED.isoformat()),
            "prs": prs,
            "state": {"last_tick": 1, **extra.pop("state", {})},
            **extra,
        }
    )


def _pr_557(**fields: Any) -> dict[str, Any]:
    """omnimemory#557 as the ticket read it: a bot's release PR, green and CLEAN."""
    return {
        "pr": OMNIMEMORY_557,
        "head_sha": HEAD_557,
        "state": "open",
        "ci": "green",
        "merge_state": "clean",
        "suspensions": ["gate"],
        "gate_reasons": ["release_train"],
        "gate_since": "2026-10-09T15:47:41Z",
        "created_at": "2026-10-01T07:34:37Z",
        **fields,
    }


def _merges(decision: ModelLandingDecision) -> list[tuple[str, str | None]]:
    return [
        (a.subject, a.head_sha)
        for a in (*decision.actions, *decision.observed_actions)
        if a.kind is EnumLandingActionKind.MERGE
    ]


def _skips(decision: ModelLandingDecision) -> dict[str, EnumLandingLandSkipReason]:
    return {s.pr: s.reason for s in decision.land_skips}


# ------------------------------------------------------------------- AC1
@pytest.mark.unit
def test_unnamed_gate_suspension_is_refused() -> None:
    with pytest.raises(ValidationError, match="gate suspension needs"):
        ModelLandingPrFacts.model_validate(_pr_557(gate_reasons=[]))
    with pytest.raises(ValidationError, match="gate_reasons without"):
        ModelLandingPrFacts.model_validate(_pr_557(suspensions=[]))
    with pytest.raises(ValidationError):
        ModelLandingPrFacts.model_validate(_pr_557(gate_reasons=["gate"]))
    named = ModelLandingPrFacts.model_validate(_pr_557())
    assert named.gate_reasons == (EnumLandingGateReason.RELEASE_TRAIN,)


@pytest.mark.unit
def test_unnamed_every_gate_row_carries_its_reasons() -> None:
    decision = decide_landing(_facts([_pr_557()]))
    assert [(g.pr, g.head_sha, g.reasons, g.released) for g in decision.gates] == [
        (OMNIMEMORY_557, HEAD_557, (EnumLandingGateReason.RELEASE_TRAIN,), False)
    ]


# ------------------------------------------------------------------- AC2
@pytest.mark.unit
def test_omnimemory_557_release_train_gate_is_named() -> None:
    decision = decide_landing(_facts([_pr_557()]))
    assert _merges(decision) == []
    (skip,) = decision.land_skips
    assert (skip.pr, skip.head_sha, skip.reason, skip.gate_reasons) == (
        OMNIMEMORY_557,
        HEAD_557,
        EnumLandingLandSkipReason.GATE,
        (EnumLandingGateReason.RELEASE_TRAIN,),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "reason", sorted(r.value for r in STALE_WHEN_GREEN_GATE_REASONS)
)
def test_omnimemory_557_red_premised_gate_older_than_two_ticks_is_merged(
    reason: str,
) -> None:
    decision = decide_landing(_facts([_pr_557(gate_reasons=[reason])]))
    assert _merges(decision) == [(OMNIMEMORY_557, HEAD_557)]
    assert decision.land_skips == ()
    (gate,) = decision.gates
    assert gate.released is True


@pytest.mark.unit
def test_omnimemory_557_red_premised_gate_within_two_ticks_is_named() -> None:
    for since in (OBSERVED - TICK, OBSERVED - 2 * TICK):
        decision = decide_landing(
            _facts(
                [_pr_557(gate_reasons=["worker_gate"], gate_since=since.isoformat())]
            )
        )
        assert _merges(decision) == []
        assert _skips(decision) == {OMNIMEMORY_557: EnumLandingLandSkipReason.GATE}
    later = OBSERVED - 2 * TICK - timedelta(seconds=1)
    decision = decide_landing(
        _facts([_pr_557(gate_reasons=["worker_gate"], gate_since=later.isoformat())])
    )
    assert _merges(decision) == [(OMNIMEMORY_557, HEAD_557)]


@pytest.mark.unit
def test_omnimemory_557_undated_or_mixed_gate_is_named_not_released() -> None:
    undated = _pr_557(gate_reasons=["companion_wait"], gate_since=None)
    mixed = _pr_557(gate_reasons=["worker_gate", "release_train"])
    for pr in (undated, mixed):
        decision = decide_landing(_facts([pr]))
        assert _merges(decision) == []
        assert _skips(decision) == {OMNIMEMORY_557: EnumLandingLandSkipReason.GATE}
        assert decision.gates[0].released is False


@pytest.mark.unit
def test_omnimemory_557_red_premised_gate_on_a_red_head_holds() -> None:
    red = _pr_557(
        gate_reasons=["companion_wait"],
        ci="red",
        red_class="cascade",
        red_checks=["OCC Companion Merged Gate"],
    )
    decision = decide_landing(_facts([red]))
    assert decision.actions == ()
    assert decision.land_skips == ()
    assert decision.gates[0].released is False


@pytest.mark.unit
def test_omnimemory_557_released_gate_still_waits_for_a_hold() -> None:
    held = _pr_557(gate_reasons=["worker_gate"], suspensions=["gate", "hold"])
    decision = decide_landing(_facts([held]))
    assert _merges(decision) == []
    assert _skips(decision) == {OMNIMEMORY_557: EnumLandingLandSkipReason.HOLD}


# ------------------------------------------------------------------- AC3
@pytest.mark.unit
def test_sidecar_2026_10_10_every_gate_entry_is_named() -> None:
    doc = json.loads(FIXTURE.read_text())
    sidecar = doc["sidecar_gate_since"]
    assert {e["kind"] for e in sidecar.values()} == {"gate"}
    for pr in doc["prs"]:  # the facts as the sidecar left them: refused
        with pytest.raises(ValidationError, match="gate suspension needs"):
            ModelLandingPrFacts.model_validate({**pr, "gate_reasons": []})
    decision = decide_landing(_facts(doc["prs"], observed_at=doc["observed_at"]))
    short = {g.pr.split("/", 1)[1]: g for g in decision.gates}
    assert sorted(short) == sorted(sidecar)
    for name, gate in short.items():
        assert gate.reasons, name
        assert gate.head_sha == sidecar[name]["head"]
        assert gate.since == datetime.fromisoformat(sidecar[name]["since"])
        assert gate.released is False
    assert {n: [r.value for r in g.reasons] for n, g in short.items()} == {
        "omnimemory#557": ["release_train"],
        "ops_gated#1786": ["operator_main"],
        "ops_gated#1799": ["operator_main"],
        "ops_gated#1802": ["operator_main"],
        "ops_gated#1824": ["operator_main"],
    }
    assert _merges(decision) == []
    assert _skips(decision) == {OMNIMEMORY_557: EnumLandingLandSkipReason.GATE}


# ------------------------------------------------- omnibase_infra#4798 shape
def _pr_4798(**fields: Any) -> dict[str, Any]:
    return {
        "pr": INFRA_4798,
        "head_sha": HEAD_4798,
        "state": "open",
        "ci": "green",
        "merge_state": "clean",
        "runtime": True,
        "created_at": "2026-10-10T03:40:41Z",
        **fields,
    }


def _lease_4798() -> dict[str, Any]:
    return {
        "pr": INFRA_4798,
        "lease_id": 7,
        "brief_class": "behind",
        "engine": "claude_sonnet",
        "dispatched_at": "2026-10-10T07:40:51Z",
        "deadline_at": "2026-10-10T08:40:51Z",
        "dispatch_head": HEAD_4798,
        "seen_heads": [HEAD_4798],
        "last_seen_head": HEAD_4798,
    }


@pytest.mark.unit
def test_omnibase_infra_4798_worker_spawned_names_the_lease() -> None:
    facts = _facts(
        [_pr_4798()],
        observed_at="2026-10-10T08:12:53Z",
        state={"next_lease_id": 8, "leases": [_lease_4798()]},
        probes=[{"lease_id": 7, "group_alive": True, "tagged_alive": True}],
    )
    decision = decide_landing(facts)
    assert _merges(decision) == []
    assert _skips(decision) == {INFRA_4798: EnumLandingLandSkipReason.LEASED}
    assert land_coverage_gaps(facts, decision) == ()


@pytest.mark.unit
def test_omnibase_infra_4798_lab_gate_is_named_and_a_free_head_is_merged() -> None:
    gated = _pr_4798(
        suspensions=["gate"],
        gate_reasons=["lab_unproven"],
        gate_since="2026-10-10T07:11:37Z",
    )
    decision = decide_landing(_facts([gated], observed_at="2026-10-10T08:12:53Z"))
    assert _merges(decision) == []
    (skip,) = decision.land_skips
    assert (skip.reason, skip.gate_reasons) == (
        EnumLandingLandSkipReason.GATE,
        (EnumLandingGateReason.LAB_UNPROVEN,),
    )
    free = decide_landing(_facts([_pr_4798()], observed_at="2026-10-10T08:12:53Z"))
    assert _merges(free) == [(INFRA_4798, HEAD_4798)]
    assert free.land_skips == ()


# -------------------------------------------------------------- AC-M5 fleet
def _green(n: int, repo: str = "acme/app", **fields: Any) -> dict[str, Any]:
    return {
        "pr": f"{repo}#{n}",
        "head_sha": f"{n:040x}",
        "state": "open",
        "ci": "green",
        "merge_state": "clean",
        "created_at": (OBSERVED - timedelta(days=1, minutes=-n)).isoformat(),
        **fields,
    }


def _fleet() -> list[dict[str, Any]]:
    gate = {"suspensions": ["gate"], "gate_since": "2026-10-09T15:47:41Z"}
    return [
        *(_green(n) for n in range(1, 8)),
        _green(8, suspensions=["hold"]),
        _green(9, suspensions=["owned"]),
        _green(10, collaborator=True),
        _green(11, parents=["acme/app#90"], open_parents=["acme/app#90"]),
        _green(12, repo="acme/lib", gate_reasons=["operator_main"], **gate),
        _green(13, repo="acme/lib", gate_reasons=["autobind"], **gate),
        _green(14, repo="acme/run", runtime=True),
        _green(15, repo="acme/run", runtime=True),
    ]


@pytest.mark.unit
def test_fleet_fifteen_green_unarmed_each_get_a_merge_or_a_named_skip() -> None:
    facts = _facts(_fleet())
    decision = decide_landing(facts)
    merged = {pr for pr, _ in _merges(decision)}
    skips = _skips(decision)
    assert not merged & set(skips)
    assert len(merged) + len(skips) == 15
    assert merged == {
        *(f"acme/app#{n}" for n in range(1, 8)),
        "acme/lib#13",
        "acme/run#14",
    }
    assert skips == {
        "acme/app#8": EnumLandingLandSkipReason.HOLD,
        "acme/app#9": EnumLandingLandSkipReason.OWNED,
        "acme/app#10": EnumLandingLandSkipReason.COLLABORATOR,
        "acme/app#11": EnumLandingLandSkipReason.OPEN_PARENTS,
        "acme/lib#12": EnumLandingLandSkipReason.GATE,
        "acme/run#15": EnumLandingLandSkipReason.TOKEN_HELD,
    }
    assert land_coverage_gaps(facts, decision) == ()


@pytest.mark.unit
def test_fleet_a_green_pr_with_no_merge_and_no_skip_is_a_gap() -> None:
    facts = _facts(_fleet())
    decision = decide_landing(facts)
    dropped = decision.model_copy(
        update={
            "land_skips": tuple(s for s in decision.land_skips if s.pr != "acme/app#8"),
            "actions": tuple(a for a in decision.actions if a.subject != "acme/app#1"),
        }
    )
    assert land_coverage_gaps(facts, dropped) == ("acme/app#1", "acme/app#8")


@pytest.mark.unit
def test_fleet_a_gap_fails_the_tick_with_no_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick whose land_skips would drop a green, CLEAN PR returns no decision."""
    facts = _facts(_fleet())
    monkeypatch.setattr(handler_pr_landing_decision, "_land_skips", lambda _t: ())
    for _ in range(2):  # deterministic: the same facts raise the same way
        with pytest.raises(LandingCoverageError) as raised:
            HandlerPrLandingDecision().handle(facts)
        assert str(raised.value).endswith(
            "acme/app#10, acme/app#11, acme/app#8, acme/app#9, acme/lib#12, acme/run#15"
        )


# ------------------------------------------------------------------- generated fleets
def _generated_fleet(rng: random.Random) -> dict[str, Any]:
    suspensions = ["hold", "do_not_land", "draft", "owned", "gate"]
    gate_reasons = [r.value for r in EnumLandingGateReason]
    repos = ["acme/app", "acme/lib", "acme/run"]
    prs: list[dict[str, Any]] = []
    n = rng.randint(1, 8)
    for i in range(1, n + 1):
        repo = rng.choice(repos)
        key = f"{repo}#{i}"
        pr: dict[str, Any] = {
            "pr": key,
            "head_sha": f"{i:040x}",
            "state": "open",
            "created_at": (OBSERVED - timedelta(days=1, minutes=-i)).isoformat(),
        }
        pr["ci"] = rng.choice(["green", "green", "green", "pending", "red"])
        if pr["ci"] == "red":
            pr["red_class"] = rng.choice(
                [
                    "cascade",
                    "replay",
                    "cancelled_producer",
                    "runner_saturation",
                    "product",
                ]
            )
        pr["merge_state"] = rng.choice(
            ["clean", "clean", "clean", "behind", "blocked", "conflicting"]
        )
        selected_suspensions = rng.sample(suspensions, rng.choice([0, 0, 0, 1, 2]))
        if selected_suspensions:
            pr["suspensions"] = selected_suspensions
        if "gate" in selected_suspensions:
            pr["gate_reasons"] = rng.sample(gate_reasons, rng.randint(1, 2))
            if rng.random() < 0.7:
                pr["gate_since"] = (
                    OBSERVED - timedelta(seconds=rng.choice([100, 700, 5000]))
                ).isoformat()
        if rng.random() < 0.15:
            pr["collaborator"] = True
        if rng.random() < 0.4:
            pr["runtime"] = True
        prs.append(pr)

    keys = [pr["pr"] for pr in prs]
    for pr in prs:
        if rng.random() < 0.2:
            parent = rng.choice([*keys, "acme/app#99"])
            if parent != pr["pr"]:
                pr["parents"] = [parent]
                if rng.random() < 0.7:
                    pr["open_parents"] = [parent]

    state: dict[str, Any] = {"last_tick": 1}
    if rng.random() < 0.5:
        state["token_holder"] = rng.choice([*keys, "acme/run#77"])
    if rng.random() < 0.2:
        state["observe_only_repos"] = [rng.choice(repos)]
    leases: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for j, pr in enumerate(prs):
        if rng.random() < 0.25:
            leases.append(
                {
                    "pr": pr["pr"],
                    "lease_id": j + 1,
                    "brief_class": "behind",
                    "engine": "claude_sonnet",
                    "dispatched_at": (OBSERVED - timedelta(minutes=10)).isoformat(),
                    "deadline_at": (
                        OBSERVED + timedelta(minutes=rng.choice([-5, 50]))
                    ).isoformat(),
                    "dispatch_head": pr["head_sha"],
                    "seen_heads": [pr["head_sha"]],
                    "last_seen_head": pr["head_sha"],
                }
            )
        if rng.random() < 0.3:
            record: dict[str, Any] = {"pr": pr["pr"]}
            if rng.random() < 0.5:
                record["awaiting_head"] = pr["head_sha"]
            if rng.random() < 0.3:
                record["parked_head"] = pr["head_sha"]
            records.append(record)
    if leases:
        state["leases"] = leases
        state["next_lease_id"] = len(prs) + 5
    if records:
        state["records"] = records

    companions: list[dict[str, Any]] = []
    if rng.random() < 0.4:
        companions.append(
            {
                "pr": "acme/occ#500",
                "head_sha": f"{500:040x}",
                "state": "open",
                "ci": rng.choice(["green", "red", "pending"]),
                "members": [
                    {"pr": member["pr"], "head_sha": member["head_sha"]}
                    for member in rng.sample(prs, min(len(prs), rng.randint(1, 2)))
                ],
            }
        )
    facts: dict[str, Any] = {
        "tick": 2,
        "observed_at": OBSERVED.isoformat(),
        "prs": prs,
        "state": state,
        "companions": companions,
    }
    if rng.random() < 0.2:
        facts["drain_requested"] = [rng.choice(repos)]
    if rng.random() < 0.2:
        facts["fixer_hold"] = [rng.choice([*repos, "all"])]
    return facts


@pytest.mark.unit
def test_generated_fleets_never_leave_a_green_clean_pr_unnamed() -> None:
    """Seeded fleets cover every skip reason, merges and released gates without a gap."""
    rng = random.Random(20865)
    seen_reasons: set[EnumLandingLandSkipReason] = set()
    merge_seen = False
    released_gate_seen = False
    for _ in range(2000):
        facts = ModelLandingFacts.model_validate(_generated_fleet(rng))
        decision = decide_landing(facts)
        assert land_coverage_gaps(facts, decision) == ()
        seen_reasons.update(skip.reason for skip in decision.land_skips)
        merge_seen |= any(
            action.kind is EnumLandingActionKind.MERGE
            for action in (*decision.actions, *decision.observed_actions)
        )
        released_gate_seen |= any(gate.released for gate in decision.gates)
    assert seen_reasons == set(EnumLandingLandSkipReason)
    assert merge_seen
    assert released_gate_seen
