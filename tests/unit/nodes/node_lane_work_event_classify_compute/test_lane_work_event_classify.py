# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane work event classifier turns every raw result into a named event (OMN-26707).

Raw text is mapped by the ordered overlay pattern table; typed results are mapped
by field and by the action id they answer. Quotes come from the pinned catalogue
fixtures of OMN-26701 wherever the catalogue holds the text.
"""

from __future__ import annotations

import json
import random
import string
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_lane_work_event import EnumLaneWorkEvent
from omnibase_core.enums.enum_lane_work_state import EnumLaneWorkState
from omnibase_core.models.lane_work import (
    ModelLaneHelpTier,
    ModelLaneWorkChange,
    ModelLaneWorkEvent,
    ModelLaneWorkState,
)

from omnimarket.nodes.node_ci_red_kind_rule_compute.handlers.handler_ci_red_kind_rule import (
    HandlerCiRedKindRule,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_overlay import (
    ModelCiRedKindRuleOverlay,
)
from omnimarket.nodes.node_lane_work_event_classify_compute.handlers.handler_lane_work_event_classify import (
    HandlerLaneWorkEventClassify,
)
from omnimarket.nodes.node_lane_work_event_classify_compute.models.model_lane_work_classify_overlay import (
    ModelLaneWorkClassifyOverlay,
)

pytestmark = pytest.mark.unit

E = EnumLaneWorkEvent
FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "lane_work_catalogue"
    / "2026-11-26"
)
NODE = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_lane_work_event_classify_compute"
)
HEAD = "0dbe3156e52dcb33b15fc3c82f5959de6691a4de"
T0 = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
CHANGE = ModelLaneWorkChange(
    owner="OmniNode-ai", repo="omnimarket", pr_number=6904, ticket="OMN-26043"
)
WORKFLOW_ID = "omninode-ai-omnimarket-6904-1"
TIER = ModelLaneHelpTier(
    name="glm-small",
    backend_id="harness-claude-glm-flash",
    harness="claude-glm",
    model="glm-5.3-flash",
    effort="medium",
)
RED_KIND_OVERLAY = {
    "schema_version": "ci_red_kind_rules.v1",
    "rules": [
        {
            "kind": "base_inherited",
            "log_pattern": "fails on dev itself",
            "ignore_case": True,
        }
    ],
}


def quote(case: str, source: str) -> str:
    doc = yaml.safe_load((FIXTURES / f"{case}.yaml").read_text())
    return next(s["quote"] for s in doc["sources"] if s["id"] == source)


def overlay_doc() -> dict[str, Any]:
    doc = yaml.safe_load((NODE / "overlay.example.yaml").read_text())
    assert isinstance(doc, dict)
    return doc


def make(overlay: dict[str, Any] | None = None) -> HandlerLaneWorkEventClassify:
    parsed = ModelLaneWorkClassifyOverlay.model_validate(overlay or overlay_doc())
    red = HandlerCiRedKindRule(
        load_overlay=lambda: ModelCiRedKindRuleOverlay.model_validate(RED_KIND_OVERLAY)
    )
    return HandlerLaneWorkEventClassify(load_overlay=lambda: parsed, red_kind_rule=red)


def state(**kw: Any) -> ModelLaneWorkState:
    base: dict[str, Any] = {
        "workflow_id": WORKFLOW_ID,
        "change": CHANGE,
        "state": EnumLaneWorkState.PUSHING,
        "seq": 3,
        "expected_head": HEAD,
        "worker_lane": "red6909-61400",
    }
    base.update(kw)
    return ModelLaneWorkState(**base)


# --------------------------------------------------------------------------- AC1


def test_ac1_random_string_is_unclassified() -> None:
    rng = random.Random(26707)
    junk = "".join(rng.choice(string.ascii_lowercase + " ") for _ in range(80))
    event = make().classify(raw_result=junk, state=state())
    assert event is not None
    assert event.kind is E.UNCLASSIFIED
    assert event.workflow_id == WORKFLOW_ID
    assert junk[:20] in event.evidence[0]


def test_ac1_unknown_typed_kind_is_unclassified() -> None:
    event = make().classify_typed("no_such_result", {}, state=state())
    assert event is not None
    assert event.kind is E.UNCLASSIFIED


# --------------------------------------------------------------------------- AC2


def test_ac2_guard_refusal_is_push_refused_guard_ledger_cell() -> None:
    event = make().classify(raw_result=quote("case_01_push_guard", "s3"), state=state())
    assert event is not None
    assert (event.kind, event.event_class) == (E.PUSH_REFUSED, "guard_ledger_cell")


def test_ac2_missing_remote_is_not_taken_for_the_guard() -> None:
    event = make().classify(
        raw_result=quote("case_01b_missing_remote", "s3"), state=state()
    )
    assert event is not None
    assert (event.kind, event.event_class) == (E.PUSH_REFUSED, "missing_remote")


def test_ac2_non_fast_forward_push_is_head_moved() -> None:
    raw = (
        "To github.com:OmniNode-ai/omnimarket.git\n"
        " ! [rejected]        omn-26707-x -> omn-26707-x (non-fast-forward)\n"
        "error: failed to push some refs to 'github.com:OmniNode-ai/omnimarket.git'\n"
    )
    event = make().classify(raw_result=raw, state=state())
    assert event is not None
    assert event.kind is E.HEAD_MOVED
    assert event.head_sha == HEAD
    assert event.event_class is None


def test_ac2_release_identity_commit_refusal() -> None:
    event = make().classify(
        raw_result=quote("case_02_release_identity", "s1"),
        state=state(state=EnumLaneWorkState.FIXING),
    )
    assert event is not None
    assert (event.kind, event.event_class) == (E.COMMIT_REFUSED, "release_identity")


def test_log_read_takes_its_red_kind_from_the_red_kind_node() -> None:
    event = make().classify(
        raw_result=quote("case_09_base_inherited", "s1"),
        state=state(state=EnumLaneWorkState.RED),
    )
    assert event is not None
    assert (event.kind, event.red_kind) == (E.LOG_READ, "base_inherited")


def test_cause_unknown_from_the_worker_text() -> None:
    event = make().classify(
        raw_result=quote("case_12_cause_unknown", "s2"),
        state=state(state=EnumLaneWorkState.DIAGNOSING),
    )
    assert event is not None
    assert event.kind is E.CAUSE_UNKNOWN


def test_same_raw_result_has_the_same_event_and_input_id() -> None:
    raw = quote("case_01_push_guard", "s3")
    first = make().classify(raw_result=raw, state=state())
    second = make().classify(raw_result=raw, state=state())
    assert first == second
    assert first is not None
    other = make().classify(raw_result=raw, state=state(seq=4))
    assert other is not None
    assert other.input_id != first.input_id


# --------------------------------------------------------------------------- AC3


def obs(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "pr": "omnimarket#6904",
        "head_sha": HEAD,
        "ci_verdict": "green",
        "observed_at": (T0 + timedelta(minutes=5)).isoformat(),
        "ci_read_at": (T0 + timedelta(minutes=5)).isoformat(),
    }
    base.update(kw)
    return base


def test_ac3_stale_observation_with_the_old_head_is_dropped() -> None:
    st = state(last_head_change_at=T0 + timedelta(minutes=10))
    assert make().classify_typed("pr_observation", obs(), state=st) is None


def test_ac3_stale_observation_naming_another_head_is_dropped() -> None:
    st = state(last_head_change_at=T0 + timedelta(minutes=10))
    other = "66c0953dfbca"
    assert (
        make().classify_typed("pr_observation", obs(head_sha=other), state=st) is None
    )


def test_ac3_stale_ci_read_is_dropped_though_observed_after() -> None:
    st = state(last_head_change_at=T0 + timedelta(minutes=2))
    stale = obs(ci_read_at=(T0 + timedelta(minutes=1)).isoformat())
    assert make().classify_typed("pr_observation", stale, state=st) is None


def test_ac3_fresh_observation_naming_another_head_is_head_moved() -> None:
    st = state(last_head_change_at=T0)
    event = make().classify_typed(
        "pr_observation", obs(head_sha="66c0953dfbca"), state=st
    )
    assert event is not None
    assert event.kind is E.HEAD_MOVED
    assert event.head_sha == "66c0953dfbca"


@pytest.mark.parametrize(
    ("verdict", "kind"),
    [("green", E.CHECK_GREEN), ("red", E.CHECK_RED), ("pending", E.CHECK_PENDING)],
)
def test_fresh_observation_of_the_expected_head_maps_by_ci_verdict(
    verdict: str, kind: EnumLaneWorkEvent
) -> None:
    st = state(last_head_change_at=T0)
    event = make().classify_typed("pr_observation", obs(ci_verdict=verdict), state=st)
    assert event is not None
    assert event.kind is kind
    assert event.head_sha == HEAD
    assert event.ci_read_at is not None


def test_abbreviated_head_matches_the_expected_head() -> None:
    st = state(last_head_change_at=T0)
    event = make().classify_typed("pr_observation", obs(head_sha=HEAD[:12]), state=st)
    assert event is not None
    assert event.kind is E.CHECK_GREEN


def test_observation_flag_after_last_push_false_is_dropped() -> None:
    payload = {"pr": "omnimarket#6904", "head_sha": "66c0953dfbca"}
    dropped = make().classify_typed(
        "pr_observation", {**payload, "observed_after_last_push": False}, state=state()
    )
    assert dropped is None
    kept = make().classify_typed(
        "pr_observation", {**payload, "observed_after_last_push": True}, state=state()
    )
    assert kept is not None
    assert kept.kind is E.HEAD_MOVED


def test_unknown_ci_verdict_is_unclassified() -> None:
    st = state(last_head_change_at=T0)
    event = make().classify_typed("pr_observation", obs(ci_verdict="shrug"), state=st)
    assert event is not None
    assert event.kind is E.UNCLASSIFIED


# --------------------------------------------------------------------------- AC4


def test_ac4_ledger_append_completion_is_an_acknowledgement() -> None:
    result = make().classify_typed(
        "ledger_append_result",
        {"action_id": "omninode-ai-omnimarket-6904-1-3-0", "status": "completed"},
        state=state(),
    )
    assert result is None


# --------------------------------------------------------------------------- AC5


def help_result(answer: dict[str, Any] | str, **kw: Any) -> dict[str, Any]:
    content = answer if isinstance(answer, str) else json.dumps(answer)
    base: dict[str, Any] = {
        "action_id": "omninode-ai-omnimarket-6904-1-3-0",
        "request_id": "4f1f2d4e-1c58-4d6f-9d0c-0a4c5d1b3a11",
        "status": "completed",
        "ok": True,
        "content": content,
        "backend_id": TIER.backend_id,
        "model_name": TIER.model,
        "attempts": [
            {
                "request_id": "4f1f2d4e-1c58-4d6f-9d0c-0a4c5d1b3a11",
                "attempt": 1,
                "tier": "glm-small",
                "backend_id": TIER.backend_id,
                "harness": TIER.harness,
                "model_name": TIER.model,
                "wall_seconds": 3.5,
                "exit_cause": "completed",
                "accepted": True,
            }
        ],
    }
    base.update(kw)
    return base


def help_state() -> ModelLaneWorkState:
    return state(
        state=EnumLaneWorkState.HELP_REQUESTED,
        help_tiers=(TIER,),
        help_tier_index=0,
    )


@pytest.mark.parametrize(
    ("answer", "kind"),
    [
        ({"kind": "help_cause_found", "cause": "stale base"}, E.HELP_CAUSE_FOUND),
        ({"kind": "help_retry", "help_origin": "pushing"}, E.HELP_RETRY),
        (
            {
                "kind": "help_stop",
                "reason": "needs_decision",
                "decision_needed": "merge or close",
            },
            E.HELP_STOP,
        ),
    ],
)
def test_a_valid_help_answer_from_the_tiers_backend(
    answer: dict[str, Any], kind: EnumLaneWorkEvent
) -> None:
    from omnibase_core.enums.enum_lane_work_blocked_reason import (
        EnumLaneWorkBlockedReason,
    )

    if "reason" in answer:
        answer = {**answer, "reason": next(iter(EnumLaneWorkBlockedReason)).value}
    event = make().classify_typed(
        "delegation_harness_result", help_result(answer), state=help_state()
    )
    assert event is not None
    assert event.kind is kind
    assert event.action_id == "omninode-ai-omnimarket-6904-1-3-0"


def test_ac5_answer_that_does_not_parse_is_help_failed() -> None:
    event = make().classify_typed(
        "delegation_harness_result",
        help_result("I think the base is stale, try again"),
        state=help_state(),
    )
    assert event is not None
    assert event.kind is E.HELP_FAILED


def test_ac5_answer_that_fails_the_help_model_is_help_failed() -> None:
    event = make().classify_typed(
        "delegation_harness_result",
        help_result({"kind": "help_retry"}),
        state=help_state(),
    )
    assert event is not None
    assert event.kind is E.HELP_FAILED


def test_ac5_attempt_naming_another_model_is_help_failed() -> None:
    result = help_result({"kind": "help_cause_found", "cause": "stale base"})
    result["attempts"][0]["model_name"] = "glm-5.3"
    result["model_name"] = "glm-5.3"
    event = make().classify_typed(
        "delegation_harness_result", result, state=help_state()
    )
    assert event is not None
    assert event.kind is E.HELP_FAILED


def test_ac5_attempt_naming_another_backend_is_help_failed() -> None:
    result = help_result({"kind": "help_cause_found", "cause": "stale base"})
    result["attempts"][0]["backend_id"] = "harness-codex"
    result["backend_id"] = "harness-codex"
    event = make().classify_typed(
        "delegation_harness_result", result, state=help_state()
    )
    assert event is not None
    assert event.kind is E.HELP_FAILED


def test_failed_delegation_is_help_failed() -> None:
    result = help_result(
        {"kind": "help_cause_found", "cause": "x"}, status="rejected", ok=False
    )
    event = make().classify_typed(
        "delegation_harness_result", result, state=help_state()
    )
    assert event is not None
    assert event.kind is E.HELP_FAILED


# --------------------------------------------------------------------- other typed


def test_lab_work_unit_results_map_by_action_id_and_exit() -> None:
    aid = "omninode-ai-omnimarket-6904-1-3-0"
    ok = make().classify_typed(
        "lab_work_unit_result",
        {"action_id": aid, "status": "completed", "exit_code": 0, "head_sha": HEAD},
        state=state(state=EnumLaneWorkState.PROVING),
    )
    bad = make().classify_typed(
        "lab_work_unit_result",
        {"action_id": aid, "status": "completed", "exit_code": 1, "head_sha": HEAD},
        state=state(state=EnumLaneWorkState.PROVING),
    )
    infra = make().classify_typed(
        "lab_work_unit_result",
        {"action_id": aid, "status": "failed", "head_sha": HEAD},
        state=state(state=EnumLaneWorkState.PROVING),
    )
    assert ok is not None
    assert (ok.kind, ok.action_id) == (E.PROOF_PASSED, aid)
    assert bad is not None
    assert (bad.kind, bad.event_class) == (E.PROOF_FAILED, "tests")
    assert infra is not None
    assert (infra.kind, infra.event_class) == (E.PROOF_FAILED, "infra")


def test_github_write_results() -> None:
    aid = "omninode-ai-omnimarket-6904-1-4-0"
    posted = make().classify_typed(
        "github_write_result", {"action_id": aid, "status": "completed"}, state=state()
    )
    failed = make().classify_typed(
        "github_write_result", {"action_id": aid, "status": "failed"}, state=state()
    )
    assert posted is not None
    assert posted.kind is E.PROOF_POSTED
    assert failed is not None
    assert failed.kind is E.PROOF_POST_FAILED


def test_handoff_answers_carry_request_id_and_head() -> None:
    aid = "omninode-ai-omnimarket-6904-1-5-0"
    written = make().classify_typed(
        "handoff_answer",
        {
            "outcome": "handed_off",
            "request_id": "R1",
            "head_sha": HEAD,
            "action_id": aid,
        },
        state=state(state=EnumLaneWorkState.HANDING_OFF),
    )
    failed = make().classify_typed(
        "handoff_answer",
        {"outcome": "failed", "request_id": "R1", "head_sha": HEAD, "action_id": aid},
        state=state(state=EnumLaneWorkState.HANDING_OFF),
    )
    assert written is not None
    assert (written.kind, written.request_id, written.head_sha) == (
        E.HANDOFF_WRITTEN,
        "R1",
        HEAD,
    )
    assert failed is not None
    assert failed.kind is E.HANDOFF_FAILED


def test_ledger_query_result_confirms_or_misses_the_claim() -> None:
    claim = {"lane": "red6909-61400", "ticket": "OMN-26043", "open": True}
    st = state(state=EnumLaneWorkState.CONFIRMING_CLAIM)
    found = make().classify_typed("ledger_query_result", {"claims": [claim]}, state=st)
    other = make().classify_typed(
        "ledger_query_result",
        {"claims": [{**claim, "ticket": "OMN-1"}, {**claim, "open": False}]},
        state=st,
    )
    none = make().classify_typed("ledger_query_result", {"claims": []}, state=st)
    assert found is not None
    assert found.kind is E.CLAIM_CONFIRMED
    assert other is not None
    assert other.kind is E.CLAIM_MISSING
    assert none is not None
    assert none.kind is E.CLAIM_MISSING


def test_every_event_the_classifier_returns_is_a_valid_event_model() -> None:
    event = make().classify(raw_result=quote("case_01_push_guard", "s3"), state=state())
    assert isinstance(event, ModelLaneWorkEvent)


# --------------------------------------------------------------------------- AC6


def test_ac6_class_names_and_patterns_live_in_the_overlay() -> None:
    source = (NODE / "handlers" / "handler_lane_work_event_classify.py").read_text()
    for word in ("guard_ledger_cell", "release_identity", "base_inherited"):
        assert word not in source
    text = (NODE / "overlay.example.yaml").read_text()
    for word in ("guard_ledger_cell", "release_identity"):
        assert word in text


# --------------------------------------------------------------------------- AC7


def test_ac7_first_matching_rule_in_overlay_order_wins() -> None:
    rules = [
        {
            "pattern": "boom",
            "event": "commit_refused",
            "event_class": "hook",
            "ignore_case": True,
        },
        {
            "pattern": "boom goes",
            "event": "push_refused",
            "event_class": "push_rejected",
            "ignore_case": True,
        },
    ]
    raw = "the boom goes here"
    first = make({"schema_version": "lane_work_classify.v1", "rules": rules})
    swapped = make(
        {"schema_version": "lane_work_classify.v1", "rules": list(reversed(rules))}
    )
    a = first.classify(raw_result=raw, state=state())
    b = swapped.classify(raw_result=raw, state=state())
    assert a is not None
    assert b is not None
    assert (a.kind, a.event_class) == (E.COMMIT_REFUSED, "hook")
    assert (b.kind, b.event_class) == (E.PUSH_REFUSED, "push_rejected")


def test_overlay_refuses_a_class_the_event_does_not_allow() -> None:
    with pytest.raises(ValueError, match="event_class"):
        ModelLaneWorkClassifyOverlay.model_validate(
            {
                "schema_version": "lane_work_classify.v1",
                "rules": [
                    {
                        "pattern": "x",
                        "event": "push_refused",
                        "event_class": "nonsense",
                        "ignore_case": False,
                    }
                ],
            }
        )


def test_overlay_refuses_an_event_the_text_cannot_produce() -> None:
    with pytest.raises(ValueError, match="event"):
        ModelLaneWorkClassifyOverlay.model_validate(
            {
                "schema_version": "lane_work_classify.v1",
                "rules": [{"pattern": "x", "event": "start", "ignore_case": False}],
            }
        )


# ------------------------------------------------------------ catalogue, as built


@pytest.mark.parametrize(
    "case",
    sorted(p.stem for p in FIXTURES.glob("*.yaml")),
)
def test_every_raw_result_step_of_the_catalogue_maps_to_its_event(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default-built classifier (the bootstrap overlay files) names each catalogue raw result."""
    monkeypatch.delenv("LANE_WORK_EVENT_CLASSIFY_OVERLAY", raising=False)
    monkeypatch.delenv("CI_RED_KIND_RULES_OVERLAY", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    doc = yaml.safe_load((FIXTURES / f"{case}.yaml").read_text())
    by_id = {s["id"]: s for s in doc["sources"]}
    handler = HandlerLaneWorkEventClassify()
    for step in doc["replay"]:
        if step["step"] != "raw_result":
            continue
        event = handler.classify(
            raw_result=by_id[step["source"]]["quote"], state=state()
        )
        for key, value in step["expect"]["event"].items():
            attr = "event_class" if key == "cls" else key
            assert getattr(event, attr) == value, f"{case} {step['source']} {key}"


def test_handle_answers_event_acknowledgement_and_dropped() -> None:
    from omnimarket.nodes.node_lane_work_event_classify_compute.models.model_lane_work_classify_request import (
        ModelLaneWorkClassifyRequest,
    )

    handler = make()
    st = state(last_head_change_at=T0 + timedelta(minutes=10))
    event = handler.handle(
        ModelLaneWorkClassifyRequest(
            state=st,
            result_kind="raw_text",
            raw_result=quote("case_01_push_guard", "s3"),
        )
    )
    ack = handler.handle(
        ModelLaneWorkClassifyRequest(
            state=st,
            result_kind="ledger_append_result",
            payload={"status": "completed"},
        )
    )
    dropped = handler.handle(
        ModelLaneWorkClassifyRequest(
            state=st, result_kind="pr_observation", payload=obs()
        )
    )
    assert event.outcome == "event"
    assert event.event is not None
    assert event.event.kind is E.PUSH_REFUSED
    assert (ack.outcome, ack.event) == ("acknowledged", None)
    assert (dropped.outcome, dropped.event) == ("dropped", None)
