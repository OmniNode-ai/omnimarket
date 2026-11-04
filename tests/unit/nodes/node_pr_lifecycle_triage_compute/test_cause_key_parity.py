"""AC1: for the same red set, the bus path and the landing controller produce equal cause keys.

Falsifier: a replay of one tick's facts through both shows a different key.
"""

import json
from pathlib import Path

import pytest

from omnimarket.handlers.cause_signature import cause_key, normalize_signature
from omnimarket.models.ci_red_triage import (
    EnumCiRedClass,
    ModelCiRedFacts,
    ModelCiRedPeer,
    ModelCiRunFailedEvent,
    ci_run_failed_event_id,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
    ModelLandingPrFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_ci_red import (
    classify_ci_red,
)

TICK_725 = (
    Path(__file__).parents[1]
    / "node_pr_landing_decision_compute"
    / "fixtures"
    / "replay_2026_10_03_omnimarket.json"
)
# The key the controller recorded for tick 725 (its cause worker CLAIM rows name it).
TICK_725_KEY = "cause:OmniNode-ai/omnimarket:52c59723e1ad"

# The controller's cause of 2026-10-09 (landing-cause CLAIM and park MSG rows): one
# check, three member PRs, and the first failure annotation of that check at each
# member's head, read from the check rollup on 2026-10-10.
LIVE_KEY = "cause:OmniNode-ai/omnibase_infra:f891e934ce4e"
LIVE_CHECK = "repo-evidence / dod-verify"
LIVE_MEMBERS = {
    4772: (
        "5dbb4db622fd244ca49f874a7bba5c03456eaad7",
        (
            "CI Summary",
            "public-repo-hygiene / public-repo-hygiene",
            LIVE_CHECK,
        ),
        {
            "public-repo-hygiene / public-repo-hygiene": "",
            LIVE_CHECK: "pull request cites OMN-18606 but carries no contracts/OMN-18606.yaml",
        },
    ),
    4786: (
        "cf1bcd7d713b89def1ad90e6def955c41ec26ee2",
        ("CI Summary", "exposure-reader-coverage", LIVE_CHECK, "verify / verify"),
        {
            "exposure-reader-coverage": "",
            LIVE_CHECK: "pull request cites OMN-17292 but carries no contracts/OMN-17292.yaml",
            "verify / verify": "RECEIPT GATE FAILED: PR body is missing required 'Evidence-Source:' line",
        },
    ),
    4796: (
        "fc93680158288e1940ecbd20373fecdc8d374f53",
        ("CI Live Contact (OMN-18648)", "CI Summary", LIVE_CHECK),
        {
            "CI Live Contact (OMN-18648)": "",
            LIVE_CHECK: "pull request cites OMN-17292 but carries no contracts/OMN-17292.yaml",
        },
    ),
}


def _number(pr: str) -> int:
    return int(pr.rsplit("#", 1)[1])


def _event(
    repo: str,
    number: int,
    head: str,
    checks: tuple[str, ...],
    peers: tuple[ModelCiRedPeer, ...],
) -> ModelCiRunFailedEvent:
    checks = tuple(sorted(set(checks)))
    return ModelCiRunFailedEvent(
        event_id=ci_run_failed_event_id(repo, number, head, checks),
        repo=repo,
        pr_number=number,
        head_sha=head,
        base="dev",
        armed=True,
        queued=False,
        failing_checks=checks,
        ci_read_at="2026-10-03T21:00:21Z",
        observed_at="2026-10-03T21:00:21Z",
        source_digest="0" * 64,
        peers=peers,
    )


def _bus_facts(
    p: ModelLandingPrFacts, red: list[ModelLandingPrFacts]
) -> ModelCiRedFacts:
    """The tick's facts for one red PR as the bus path receives them.

    The detector's peers are the other red PRs sharing a check; every red PR of
    the tick is taken as armed, and the reader's annotations are the tick's.
    """
    repo, _, _ = p.pr.partition("#")
    others = [q for q in red if q.pr != p.pr and set(q.red_checks) & set(p.red_checks)]
    peers = tuple(
        ModelCiRedPeer(
            pr_number=_number(q.pr),
            head_sha=q.head_sha,
            armed=True,
            red_contexts=tuple(q.red_checks),
        )
        for q in others
    )
    return ModelCiRedFacts(
        event=_event(repo, _number(p.pr), p.head_sha, tuple(p.red_checks), peers),
        annotations=dict(p.red_annotations),
        peer_annotations={_number(q.pr): dict(q.red_annotations) for q in others},
        annotations_read=True,
    )


@pytest.mark.unit
def test_ac1_tick_725_replayed_through_both_gives_one_key() -> None:
    facts = ModelLandingFacts.model_validate(json.loads(TICK_725.read_text())["facts"])
    decision = decide_landing(facts)
    causes = [
        a
        for a in decision.actions
        if a.kind.value == "dispatch_worker" and a.subject.startswith("cause:")
    ]
    assert [a.subject for a in causes] == [TICK_725_KEY]
    brief = causes[0].cause_brief
    assert brief is not None
    members = {m.pr for m in brief.members}
    assert len(members) == 19
    red = [p for p in facts.prs if p.ci.value == "red" and p.state.value == "open"]
    bus = {p.pr: classify_ci_red(_bus_facts(p, red)) for p in red}
    for pr in sorted(members):
        assert bus[pr].red_class == EnumCiRedClass.SHARED_CAUSE, pr
        assert bus[pr].cause_key == TICK_725_KEY, pr
        assert bus[pr].owner_key == TICK_725_KEY, pr
    # No PR of the tick gets a cause key the controller did not form.
    assert {r.cause_key for r in bus.values()} - {None} == {TICK_725_KEY}


@pytest.mark.unit
def test_ac1_live_cause_of_2026_10_09_replayed_through_the_bus_path() -> None:
    def facts_for(number: int) -> ModelCiRedFacts:
        head, checks, annotations = LIVE_MEMBERS[number]
        others = {n: v for n, v in LIVE_MEMBERS.items() if n != number}
        return ModelCiRedFacts(
            event=_event(
                "omnibase_infra",
                number,
                head,
                checks,
                tuple(
                    ModelCiRedPeer(pr_number=n, head_sha=h, armed=True, red_contexts=c)
                    for n, (h, c, _) in others.items()
                ),
            ),
            annotations=annotations,
            peer_annotations={n: a for n, (_, _, a) in others.items()},
            annotations_read=True,
        )

    for number in LIVE_MEMBERS:
        result = classify_ci_red(facts_for(number))
        assert result.cause_key == LIVE_KEY, number
        assert result.check == LIVE_CHECK
        assert result.members == (4772, 4786, 4796)
    # The controller's own computation over the same annotation.
    text = LIVE_MEMBERS[4796][2][LIVE_CHECK]
    assert (
        cause_key("OmniNode-ai/omnibase_infra", normalize_signature(LIVE_CHECK, text))
        == LIVE_KEY
    )


# One check name, two different first failure annotations: a check-level key would
# put all six PRs in one cause, the annotation-level key separates them into two.
COLLIDE_REPO = "OmniNode-ai/omnimarket"
COLLIDE_CHECK = "repo-evidence / dod-verify"
COLLIDE_TEXT = {
    "missing-contract": "pull request cites OMN-17292 but carries no contracts/OMN-17292.yaml",
    "missing-evidence": "RECEIPT GATE FAILED: PR body is missing required 'Evidence-Source:' line",
}
COLLIDE_PRS = {
    "missing-contract": (101, 102, 103),
    "missing-evidence": (104, 105, 106),
}


def _collide_facts(annotated: bool) -> ModelLandingFacts:
    prs = []
    for kind, numbers in COLLIDE_PRS.items():
        for number in numbers:
            pr = {
                "pr": f"{COLLIDE_REPO}#{number}",
                "head_sha": f"{number:040x}",
                "state": "open",
                "ci": "red",
                "created_at": f"2026-10-02T12:{number % 60:02d}:00+00:00",
                "red_class": "product",
                "red_checks": [COLLIDE_CHECK],
            }
            if annotated:
                pr["red_annotations"] = {COLLIDE_CHECK: COLLIDE_TEXT[kind]}
            prs.append(pr)
    return ModelLandingFacts.model_validate(
        {
            "tick": 1,
            "observed_at": "2026-10-03T12:05:00+00:00",
            "policy": {"max_workers": 18},
            "state": {"records": []},
            "prs": prs,
            "probes": [],
            "results": [],
        }
    )


def _controller_causes(facts: ModelLandingFacts) -> dict[str, set[str]]:
    """The controller's cause keys for the tick, each with its member PRs."""
    decision = decide_landing(facts)
    out: dict[str, set[str]] = {}
    for action in decision.actions:
        if action.kind.value == "dispatch_worker" and action.subject.startswith(
            "cause:"
        ):
            assert action.cause_brief is not None
            out[action.subject] = {m.pr for m in action.cause_brief.members}
    return out


def _bus_causes(facts: ModelLandingFacts, annotated: bool) -> dict[str, set[str]]:
    red = [p for p in facts.prs if p.ci.value == "red" and p.state.value == "open"]
    out: dict[str, set[str]] = {}
    for p in red:
        facts_for_pr = _bus_facts(p, red)
        if not annotated:
            facts_for_pr = facts_for_pr.model_copy(
                update={
                    "annotations": {},
                    "peer_annotations": {},
                    "annotations_read": False,
                }
            )
        result = classify_ci_red(facts_for_pr)
        assert result.red_class == EnumCiRedClass.SHARED_CAUSE, p.pr
        assert result.cause_key is not None
        out.setdefault(result.cause_key, set()).add(p.pr)
    return out


@pytest.mark.unit
def test_ac1_one_check_two_annotations_gets_two_equal_keys_where_check_level_collides() -> (
    None
):
    annotated = _collide_facts(annotated=True)
    controller = _controller_causes(annotated)
    bus = _bus_causes(annotated, annotated=True)
    keys = {
        kind: cause_key(
            COLLIDE_REPO, normalize_signature(COLLIDE_CHECK, COLLIDE_TEXT[kind])
        )
        for kind in COLLIDE_TEXT
    }
    assert keys["missing-contract"] != keys["missing-evidence"]
    expected = {
        keys[kind]: {f"{COLLIDE_REPO}#{n}" for n in numbers}
        for kind, numbers in COLLIDE_PRS.items()
    }
    # Equal keys, and equal members, from both paths.
    assert controller == expected
    assert bus == expected

    # Control: with the annotations unread, the bus path puts the same six PRs under one
    # check-level key, which is neither of the annotation-level keys. The annotation-level
    # key is what separates them.
    all_six = {f"{COLLIDE_REPO}#{n}" for ns in COLLIDE_PRS.values() for n in ns}
    check_level = _bus_causes(_collide_facts(annotated=False), annotated=False)
    assert list(check_level.values()) == [all_six]
    assert set(check_level).isdisjoint(keys.values())
