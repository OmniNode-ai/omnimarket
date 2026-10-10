"""All first-match controller rules and conservative missing-fact behavior."""

import hashlib
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.handlers.cause_signature import cause_key, normalize_signature
from omnimarket.models.ci_red_triage import (
    EnumCiRedClass,
    ModelCiRedPeer,
    ModelCiRunFailedEvent,
    ci_run_failed_event_id,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_ci_red import (
    HandlerClassifyCiRed,
    ModelCiRedFacts,
    classify_ci_red,
)


def event(
    checks: tuple[str, ...] = ("CI Summary", "check"),
    peers: tuple[ModelCiRedPeer, ...] = (),
) -> ModelCiRunFailedEvent:
    checks = tuple(sorted(set(checks)))
    return ModelCiRunFailedEvent(
        event_id=ci_run_failed_event_id("omniclaude", 1, "head", checks),
        repo="omniclaude",
        pr_number=1,
        head_sha="head",
        base="main",
        armed=True,
        queued=False,
        failing_checks=checks,
        ci_read_at="2026-10-08T10:00:00Z",
        observed_at="2026-10-08T10:00:00Z",
        source_digest="d" * 64,
        peers=peers,
    )


def peers(
    count: int, checks: tuple[str, ...] = ("check",), armed: bool = True
) -> tuple[ModelCiRedPeer, ...]:
    return tuple(
        ModelCiRedPeer(
            pr_number=n, head_sha=f"head-{n}", armed=armed, red_contexts=checks
        )
        for n in range(2, count + 1)
    )


@pytest.mark.parametrize(
    ("checks", "deciding"),
    [
        (("CI Summary", "check"), "check"),
        (("ci summary", "check"), "check"),
        (("CI Summary",), "CI Summary"),
    ],
)
def test_r0_summary(checks: tuple[str, ...], deciding: str) -> None:
    result = classify_ci_red(ModelCiRedFacts(event=event(checks)))
    assert result.check == deciding
    assert result.red_class == EnumCiRedClass.PR_OWN
    assert result.owner_key == f"OmniNode-ai/omniclaude#1@head:{deciding}"


def test_r1_hostile_review_only_precedes_base() -> None:
    checks = (
        "CI Summary",
        "Hostile Reviewer (adversarial gate)",
        "HOSTILE REVIEW Gate",
    )
    result = classify_ci_red(
        ModelCiRedFacts(event=event(checks), base_read=True, base_red_checks=checks)
    )
    assert result.red_class == EnumCiRedClass.RUNNER
    assert result.owner_key == "OmniNode-ai/omniclaude#1@head:rerun"
    mixed = classify_ci_red(
        ModelCiRedFacts(event=event(("Hostile Review Gate", "product")))
    )
    assert mixed.red_class == EnumCiRedClass.PR_OWN
    assert mixed.check == "product"


@pytest.mark.parametrize(
    ("base_read", "base_checks", "expected"),
    [
        (False, ("check",), EnumCiRedClass.PR_OWN),
        (True, ("check",), EnumCiRedClass.DEV_HEAD),
        (True, ("CHECK",), EnumCiRedClass.PR_OWN),
        (True, (), EnumCiRedClass.PR_OWN),
    ],
)
def test_r2_requires_read_and_exact_base_names(
    base_read: bool, base_checks: tuple[str, ...], expected: EnumCiRedClass
) -> None:
    result = classify_ci_red(
        ModelCiRedFacts(
            event=event(),
            base_read=base_read,
            base_red_checks=base_checks,
            check_conclusions={"check": "failure"},
        )
    )
    assert result.red_class == expected
    if base_read and base_checks == ("check",):
        assert result.owner_key == "dev:OmniNode-ai/omniclaude:main:check"


def test_r2_requires_every_red_and_precedes_runner_and_cluster() -> None:
    facts = ModelCiRedFacts(
        event=event(("a", "b"), peers(3, ("a", "b"))),
        base_read=True,
        base_red_checks=("a",),
        check_conclusions={"a": "timed_out", "b": "cancelled"},
    )
    assert classify_ci_red(facts).red_class == EnumCiRedClass.RUNNER
    assert (
        classify_ci_red(
            facts.model_copy(update={"base_red_checks": ("a", "b")})
        ).red_class
        == EnumCiRedClass.DEV_HEAD
    )


@pytest.mark.parametrize(
    ("conclusions", "expected"),
    [
        ({"a": "timed_out", "b": "cancelled"}, EnumCiRedClass.RUNNER),
        ({"a": "timed_out"}, EnumCiRedClass.PR_OWN),
        ({"a": "failure", "b": "cancelled"}, EnumCiRedClass.PR_OWN),
        ({}, EnumCiRedClass.PR_OWN),
    ],
)
def test_r3_all_conclusions_known(
    conclusions: dict[str, str], expected: EnumCiRedClass
) -> None:
    result = classify_ci_red(
        ModelCiRedFacts(event=event(("a", "b")), check_conclusions=conclusions)
    )
    assert result.red_class == expected
    assert result.check == "a"


@pytest.mark.parametrize(
    ("count", "armed", "expected"),
    [
        (2, True, EnumCiRedClass.PR_OWN),
        (3, True, EnumCiRedClass.SHARED_CAUSE),
        (4, False, EnumCiRedClass.PR_OWN),
    ],
)
def test_r4_member_floor_and_armed_peers(
    count: int, armed: bool, expected: EnumCiRedClass
) -> None:
    result = HandlerClassifyCiRed().handle(
        ModelCiRedFacts(event=event(peers=peers(count, armed=armed)))
    )
    assert result.red_class == expected
    if expected == EnumCiRedClass.SHARED_CAUSE:
        assert result.members == (1, 2, 3)
        assert (
            result.cause_key
            == result.owner_key
            == "cause:OmniNode-ai/omniclaude:"
            + hashlib.sha256(b"check").hexdigest()[:12]
        )
    else:
        assert result.members == (1,)
        assert result.cause_key is None


def test_r4_largest_cluster_then_lexical_tie_and_unique_members() -> None:
    shared = peers(3, ("a", "b"))
    result = classify_ci_red(ModelCiRedFacts(event=event(("a", "b"), shared + shared)))
    assert result.check == "a"
    assert result.members == (1, 2, 3)
    result = classify_ci_red(
        ModelCiRedFacts(
            event=event(
                ("a", "b"),
                (
                    *shared,
                    ModelCiRedPeer(
                        pr_number=4, head_sha="h", armed=True, red_contexts=("b",)
                    ),
                ),
            )
        )
    )
    assert result.check == "b"
    assert result.members == (1, 2, 3, 4)


def test_summary_only_never_clusters() -> None:
    result = classify_ci_red(
        ModelCiRedFacts(event=event(("CI Summary",), peers(4, ("CI Summary",))))
    )
    assert result.red_class == EnumCiRedClass.PR_OWN


def test_full_slug_is_preserved() -> None:
    ev = event().model_dump()
    ev["repo"] = "Other-org/repo"
    ev["event_id"] = ci_run_failed_event_id(ev["repo"], 1, "head", ev["failing_checks"])
    result = classify_ci_red(
        ModelCiRedFacts(event=ModelCiRunFailedEvent.model_validate(ev))
    )
    assert result.owner_key == "Other-org/repo#1@head:check"


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"event_id": "0" * 64}, "event_id"),
        ({"failing_checks": ("b", "a")}, "sorted"),
        ({"failing_checks": ("a", "a")}, "unique"),
        ({"failing_checks": ()}, "at least 1"),
        ({"observed_at": "2026-02-30T10:00:00Z"}, "day"),
        ({"unexpected": "transport"}, "Extra inputs"),
    ],
)
def test_invalid_event_rejected(updates: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelCiRunFailedEvent.model_validate({**event().model_dump(), **updates})


def annotated(
    peer_count: int,
    own: dict[str, str],
    peer_text: dict[int, dict[str, str]],
    checks: tuple[str, ...] = ("check",),
) -> ModelCiRedFacts:
    return ModelCiRedFacts(
        event=event(checks, peers(peer_count, checks)),
        annotations=own,
        peer_annotations=peer_text,
        annotations_read=True,
    )


def test_r4_annotation_level_key_matches_the_controller_signature() -> None:
    text = "pull request cites OMN-17292 but carries no contracts/OMN-17292.yaml"
    other = "pull request cites OMN-18606 but carries no contracts/OMN-18606.yaml"
    result = classify_ci_red(
        annotated(3, {"check": text}, {2: {"check": other}, 3: {"check": text}})
    )
    assert result.red_class == EnumCiRedClass.SHARED_CAUSE
    assert result.members == (1, 2, 3)
    assert (
        result.cause_key
        == result.owner_key
        == cause_key("OmniNode-ai/omniclaude", normalize_signature("check", text))
    )
    assert result.reason == "annotation-level cause reaches cluster_min_members"


@pytest.mark.parametrize(
    ("own", "peer_text"),
    [
        ({"check": ""}, {2: {"check": "boom"}, 3: {"check": "boom"}}),
        ({}, {2: {"check": "boom"}, 3: {"check": "boom"}}),
        ({"check": "boom"}, {2: {"check": "boom"}, 3: {"check": "other"}}),
        ({"check": "boom"}, {2: {"check": "boom"}}),
    ],
)
def test_r4_unread_or_different_signatures_never_cluster(
    own: dict[str, str], peer_text: dict[int, dict[str, str]]
) -> None:
    result = classify_ci_red(annotated(3, own, peer_text))
    assert result.red_class == EnumCiRedClass.PR_OWN
    assert result.cause_key is None


def test_r4_a_cluster_mostly_inside_a_chosen_cause_is_absorbed() -> None:
    # (a, x) holds 2-6 and leads on the tie; (b, y) holds 1-5, 4 of 5 inside it:
    # absorbed, so PR 1, whose a-annotation differs, is in the (a, x) cause.
    peer_text = {n: {"a": "x", "b": "y"} for n in (2, 3, 4, 5)} | {6: {"a": "x"}}
    facts = ModelCiRedFacts(
        event=event(
            ("a", "b"),
            tuple(
                ModelCiRedPeer(
                    pr_number=n,
                    head_sha=f"head-{n}",
                    armed=True,
                    red_contexts=("a",) if n == 6 else ("a", "b"),
                )
                for n in (2, 3, 4, 5, 6)
            ),
        ),
        annotations={"a": "z", "b": "y"},
        peer_annotations=peer_text,
        annotations_read=True,
    )
    result = classify_ci_red(facts)
    assert result.red_class == EnumCiRedClass.SHARED_CAUSE
    assert result.check == "a"
    assert result.members == (1, 2, 3, 4, 5, 6)
    assert result.cause_key == cause_key(
        "OmniNode-ai/omniclaude", normalize_signature("a", "x")
    )
    # Under the ratio (PR 5 off b), (b, y) holds 1-4 with 3 of 4 inside: PR 1 owns its red.
    peer_text[5] = {"a": "x"}
    apart = classify_ci_red(facts.model_copy(update={"peer_annotations": peer_text}))
    assert apart.red_class == EnumCiRedClass.PR_OWN


def test_r4_unread_annotations_keep_check_level_clusters_and_key() -> None:
    result = classify_ci_red(
        ModelCiRedFacts(event=event(peers=peers(3)), annotations={"check": "boom"})
    )
    assert result.red_class == EnumCiRedClass.SHARED_CAUSE
    assert result.cause_key == (
        "cause:OmniNode-ai/omniclaude:" + hashlib.sha256(b"check").hexdigest()[:12]
    )
    assert result.reason.endswith("(annotations unread)")
