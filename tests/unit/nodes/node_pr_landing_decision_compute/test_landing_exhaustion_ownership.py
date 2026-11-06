# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exhaustion retains its budget and names who owns the next step.

The two PR heads and red checks are the recorded watcher read of November 4.
Only their exhausted records are reconstructed: the brief did not include a
controller state snapshot. The cause case uses the recorded Runner Routing
Audit check with a minimal synthetic cause record, not a claimed live replay.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.nodes.node_pr_landing_decision_compute import (
    NodePrLandingDecisionCompute,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)

NOW = datetime(2026, 11, 4, 23, 21, 50, tzinfo=UTC)
REPO = "OmniNode-ai/omnimarket"
HEADS = {
    5003: "ee0e40d9cd83b9d7dd068b4ccb5b42196f37327f",
    5049: "14402f353cacc5ae78f6027014d8d4e939c7a689",
}
CHECK = "runner-routing-audit / Runner Routing Audit"
CAUSE = f"cause:{REPO}:0123456789ab"


def facts(*, cause: bool = False, **extra: object) -> ModelLandingFacts:
    prs = [
        {
            "pr": f"{REPO}#{number}",
            "head_sha": head,
            "state": "open",
            "ci": "red",
            "red_class": "product",
            "red_checks": ["Tests (Split 2/20)"],
            "created_at": NOW - timedelta(days=1),
        }
        for number, head in HEADS.items()
    ]
    records = [
        {
            "pr": pr["pr"],
            "ladder_index": 3,
            "parked_head": pr["head_sha"],
            "parked_fingerprint": hashlib.sha256(b"").hexdigest(),
        }
        for pr in prs
    ]
    causes = (
        [
            {
                "key": CAUSE,
                "repo": REPO,
                "pairs": [
                    {
                        "check": CHECK,
                        "signature": "0123456789ab",
                        "example": CHECK,
                        "members": 2,
                    }
                ],
                "members": [{"pr": p["pr"], "head_sha": p["head_sha"]} for p in prs],
                "attempts": 3,
                "parked_until": NOW + timedelta(hours=12),
            }
        ]
        if cause
        else []
    )
    return ModelLandingFacts.model_validate(
        {
            "tick": 1,
            "observed_at": NOW,
            "prs": prs,
            "state": {"records": records, "causes": causes},
            "policy": {"max_workers": 0},
            **extra,
        }
    )


@pytest.mark.unit
def test_recorded_red_heads_get_persisted_named_owner_without_retry() -> None:
    request = facts()
    node = NodePrLandingDecisionCompute()
    result = node.handle(request)
    assert result.actions == ()
    assert len(result.degraded) == 2
    for row in result.degraded:
        assert row.owner is not None
        assert row.owner.kind == "lane"
        assert row.owner.name == "landing-controller"
    for rec in result.next_state.records:
        assert rec.exhaustion_owner == result.degraded[0].owner
        assert rec.ladder_index == 3
        assert rec.parked_head == HEADS[int(rec.pr.split("#")[1])]
    assert node.handle(request).model_dump_json() == result.model_dump_json()
    later = node.handle(
        request.model_copy(update={"tick": 2, "state": result.next_state})
    )
    assert later.actions == ()
    assert later.degraded == result.degraded


@pytest.mark.unit
def test_legacy_parked_cause_acquires_its_live_claim_owner() -> None:
    request = facts(
        cause=True,
        cause_owners=[{"lane": "runner-routing-fix", "repo": REPO, "cause": CAUSE}],
    )
    result = NodePrLandingDecisionCompute().handle(request)
    row = next(row for row in result.degraded if row.subject == CAUSE)
    assert row.owner is not None
    assert row.owner.name == "runner-routing-fix"
    rec = result.next_state.causes[0]
    assert rec.exhaustion_owner == row.owner
    assert rec.attempts == 3
    assert rec.parked_until == NOW + timedelta(hours=12)
    assert result.actions == ()


@pytest.mark.unit
def test_explicit_human_owner_persists_until_the_pr_head_moves() -> None:
    request = facts(
        policy={
            "max_workers": 0,
            "exhaustion_owner": {"kind": "human", "name": "release-maintainer"},
        }
    )
    node = NodePrLandingDecisionCompute()
    first = node.handle(request)
    assert all(row.owner.name == "release-maintainer" for row in first.degraded)
    next_input = request.model_dump(mode="json")
    next_input.update(tick=2, state=first.next_state.model_dump(mode="json"))
    next_input["policy"]["exhaustion_owner"] = {"kind": "lane", "name": "new-fallback"}
    same_head = node.handle(ModelLandingFacts.model_validate(next_input))
    assert same_head.degraded == first.degraded
    next_input["prs"][0]["head_sha"] = "b" * 40
    moved = node.handle(ModelLandingFacts.model_validate(next_input))
    rec = next(rec for rec in moved.next_state.records if rec.pr.endswith("#5003"))
    assert rec.exhaustion_owner is None
    assert rec.parked_head is None
    assert rec.ladder_index == 0
    assert moved.actions == ()


@pytest.mark.unit
@pytest.mark.parametrize("suspension", ["hold", "owned", "draft", "do_not_land"])
def test_accountability_never_overrides_a_suspension(suspension: str) -> None:
    raw = facts().model_dump(mode="json")
    for pr in raw["prs"]:
        pr["suspensions"] = [suspension]
    result = NodePrLandingDecisionCompute().handle(
        ModelLandingFacts.model_validate(raw)
    )
    assert result.actions == ()
    assert result.degraded == ()
    assert all(rec.exhaustion_owner is None for rec in result.next_state.records)


@pytest.mark.unit
def test_exact_cause_claim_precedes_check_claim_in_either_input_order() -> None:
    owners = [
        {"lane": "a-check-fixer", "repo": REPO, "check": CHECK},
        {"lane": "z-exact-fixer", "repo": REPO, "cause": CAUSE},
        {"lane": "unrelated", "repo": REPO, "check": "lint"},
    ]
    node = NodePrLandingDecisionCompute()
    first = node.handle(facts(cause=True, cause_owners=owners))
    other = node.handle(facts(cause=True, cause_owners=list(reversed(owners))))
    assert first.model_dump_json() == other.model_dump_json()
    assert all(row.owner.name == "z-exact-fixer" for row in first.degraded)


@pytest.mark.unit
def test_new_cause_park_handoff_names_owner_once_without_resetting_budget() -> None:
    from omnimarket.handlers.cause_signature import cause_key, normalize_signature

    raw = facts(cause=True).model_dump(mode="json")
    signature = normalize_signature(CHECK, "Runner label does not match policy")
    key = cause_key(REPO, signature)
    for pr in raw["prs"]:
        pr["red_checks"] = [CHECK]
        pr["red_annotations"] = {CHECK: "Runner label does not match policy"}
    rec = raw["state"]["causes"][0]
    rec.update(key=key, parked_until=None)
    rec["pairs"][0].update(signature=signature)
    raw["policy"]["cluster_min_members"] = 2
    node = NodePrLandingDecisionCompute()
    request = ModelLandingFacts.model_validate(raw)
    first = node.handle(request)
    (ticket,) = first.actions
    assert ticket.kind.value == "file_cause_ticket"
    assert ticket.owner.name == "landing-controller"
    cause = first.next_state.causes[0]
    assert cause.exhaustion_owner == ticket.owner
    assert cause.attempts == 3
    assert ticket.members == cause.members
    assert ticket.dedupe_key == cause.ticketed
    later = node.handle(
        request.model_copy(update={"tick": 2, "state": first.next_state})
    )
    assert later.actions == ()
    assert later.next_state.causes[0].attempts == 3
    assert all(row.owner is not None for row in later.degraded)


@pytest.mark.unit
def test_overlay_is_resolved_before_the_tick_and_survives_serialization(
    tmp_path, monkeypatch
) -> None:
    from omnimarket.nodes.node_pr_landing_decision_compute import overlay

    root = tmp_path / overlay.NODE_NAME
    root.mkdir()
    (root / "overlay.yaml").write_text("kind: lane\nname: budget-duty\n")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    request = facts()
    assert request.policy.exhaustion_owner.name == "budget-duty"
    (root / "overlay.yaml").write_text("kind: human\nname: changed-after-snapshot\n")
    snapshot = ModelLandingFacts.model_validate_json(request.model_dump_json())
    result = NodePrLandingDecisionCompute().handle(snapshot)
    assert all(row.owner.name == "budget-duty" for row in result.degraded)


@pytest.mark.unit
@pytest.mark.parametrize(
    "document",
    [
        "kind: lane\nname: ''\n",
        "kind: bot\nname: duty\n",
        "kind: lane\nname: two words\n",
        "kind: lane\nname: duty\nunknown: true\n",
    ],
)
def test_invalid_owner_overlay_refuses_instead_of_falling_back(
    tmp_path, monkeypatch, document: str
) -> None:
    from omnimarket.handlers.node_overlay_reader import NodeOverlayError
    from omnimarket.nodes.node_pr_landing_decision_compute import overlay

    root = tmp_path / overlay.NODE_NAME
    root.mkdir()
    (root / "overlay.yaml").write_text(document)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError, match="invalid"):
        facts()


@pytest.mark.unit
def test_ended_cause_claim_returns_accountability_to_configured_lane() -> None:
    request = facts(
        cause=True,
        cause_owners=[{"lane": "finished-fix-lane", "repo": REPO, "cause": CAUSE}],
    )
    node = NodePrLandingDecisionCompute()
    first = node.handle(request)
    assert all(row.owner.name == "finished-fix-lane" for row in first.degraded)
    next_request = request.model_copy(
        update={"tick": 2, "state": first.next_state, "cause_owners": ()}
    )
    later = node.handle(next_request)
    assert all(row.owner.name == "landing-controller" for row in later.degraded)
    assert all(
        rec.exhaustion_owner.name == "landing-controller"
        for rec in later.next_state.records
    )
    assert later.next_state.causes[0].exhaustion_owner.name == "landing-controller"
    assert later.next_state.causes[0].attempts == 3
    assert later.actions == ()
