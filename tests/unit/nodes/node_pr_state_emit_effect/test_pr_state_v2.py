# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Schema version 2 of the PR-state event: per-check facts, and both versions accepted."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from omnimarket.events.pr_state import (
    ModelPrCheckFact,
    ModelPrStateEmitRequest,
    ModelPrStateEmitRequestV2,
    ModelPrStateObservedEvent,
    ModelPrStateObservedEventV2,
    pr_state_event_from_wire,
)
from tests.unit.nodes.node_pr_state_emit_effect.helpers import (
    check_fact,
    event,
    event_v2,
)

pytestmark = pytest.mark.unit


def wire_of(e: ModelPrStateObservedEvent) -> dict[str, object]:
    return {
        **e.model_dump(mode="json"),
        "actor": "watcher",
        "lane": "dev",
        "correlation_id": "transport",
        "emitted_at": e.observed_at,
    }


def test_v1_payload_is_unchanged_by_the_new_version() -> None:
    e = event(ci_verdict="RED", red_contexts=("unit",))
    canonical = json.dumps(
        e.model_dump(mode="json", exclude={"observed_at", "digest"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert e.digest == hashlib.sha256(canonical.encode()).hexdigest()
    assert "schema_version" not in e.model_dump()
    parsed = pr_state_event_from_wire(wire_of(e))
    assert type(parsed) is ModelPrStateObservedEvent
    assert parsed == e


def test_v2_carries_check_facts_and_the_digest_covers_them() -> None:
    e = event_v2()
    assert e.schema_version == 2
    assert [f.check for f in e.checks] == ["CI Summary", "unit"]
    assert e.checks[0].run_id == 17000000002
    assert e.checks[1].conclusion == "timed_out"
    assert isinstance(e, ModelPrStateObservedEvent)
    assert event_v2(observed_at="2026-09-28T12:00:00Z").digest == e.digest
    changed = event_v2(
        checks=(
            check_fact("CI Summary", run_id=17000000003),
            check_fact("unit", conclusion="timed_out"),
        )
    )
    assert changed.digest != e.digest
    assert event_v2(base_read=False).digest != e.digest


def test_wire_parser_picks_the_version_from_schema_version() -> None:
    v2 = event_v2()
    parsed = pr_state_event_from_wire(wire_of(v2))
    assert type(parsed) is ModelPrStateObservedEventV2
    assert parsed == v2


def test_wire_parser_checks_the_digest_of_either_version() -> None:
    for e in (event(), event_v2()):
        wire = wire_of(e)
        wire["digest"] = "0" * 64
        with pytest.raises(ValidationError, match="digest does not match"):
            pr_state_event_from_wire(wire)
        wire.pop("digest")
        with pytest.raises(ValueError, match="carry digest"):
            pr_state_event_from_wire(wire)


def test_v2_fields_do_not_pass_through_a_v1_parse() -> None:
    with pytest.raises(ValidationError):
        ModelPrStateObservedEvent.model_validate(event_v2().model_dump())


@pytest.mark.parametrize(
    "changes",
    [
        {"checks": (check_fact("CI Summary"), check_fact("CI Summary"))},
        {"checks": (check_fact("not-red"),)},
        {"checks": (check_fact("unit", run_id=0, workflow="CI"),)},
        {"checks": (check_fact("unit", run_id=7, workflow=""),)},
        {"checks": (check_fact("unit", completed_at="2026-10-08T09:58:00"),)},
        {"checks": (check_fact("unit", conclusion=""),)},
        {"base_red_checks": ("unit",), "base_read": False},
        {"base_red_checks": ("unit", "unit")},
        {"base_red_checks": ("unit", "lint")},
        {"schema_version": 1},
        {"base_read": 1},
    ],
)
def test_invalid_v2_events_fail_closed(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        event_v2(**changes)


def test_a_check_posted_by_another_app_has_no_run_or_workflow() -> None:
    fact = ModelPrCheckFact.model_validate(
        check_fact("CodeRabbit", run_id=0, workflow="")
    )
    assert (fact.run_id, fact.workflow) == (0, "")


def test_v2_emit_request_validates_into_the_v2_event() -> None:
    request = ModelPrStateEmitRequestV2.model_validate(
        event_v2().model_dump(exclude={"digest"})
    )
    assert isinstance(request, ModelPrStateEmitRequest)
    assert (
        ModelPrStateObservedEventV2.model_validate(request.model_dump()).digest
        == event_v2().digest
    )
