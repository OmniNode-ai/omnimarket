# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Event identity, strict wire schema, and durable registration (OMN-19999)."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from omnimarket.events.pr_state import (
    ModelPrStateObservedEvent,
)
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    EnumDurabilityTier,
    resolve_event_type,
)
from tests.unit.nodes.node_pr_state_emit_effect.helpers import event

pytestmark = pytest.mark.unit

FIELDS = {
    "repo",
    "pr_number",
    "state",
    "head_sha",
    "base",
    "head_ref",
    "title",
    "draft",
    "author",
    "author_is_bot",
    "labels",
    "armed",
    "queued",
    "watcher_class",
    "ci_verdict",
    "red_contexts",
    "pending_contexts",
    "ci_read_at",
    "merged_at",
    "observed_at",
    "digest",
}


def test_exact_fields_and_canonical_digest() -> None:
    e = event()
    assert set(type(e).model_fields) == FIELDS
    data = e.model_dump(mode="json", exclude={"observed_at", "digest"})
    expected = hashlib.sha256(
        json.dumps(
            data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    assert e.digest == expected
    assert ModelPrStateObservedEvent.model_validate_json(e.model_dump_json()) == e
    assert event(observed_at="2026-09-28T12:00:00Z").digest == e.digest
    assert event(armed=True).digest != e.digest
    assert event(title="Unicode café").digest != e.digest


@pytest.mark.parametrize(
    "changes",
    [
        {"pr_number": "3050"},
        {"pr_number": True},
        {"draft": 0},
        {"title": "x" * 201},
        {"state": "invalid"},
        {"labels": ["ready"]},
        {"unexpected": 1},
        {"observed_at": "2026-09-28T10:00:00"},
        {"observed_at": "2026-09-28T11:00:00+01:00"},
        {"observed_at": "2026-02-30T10:00:00Z"},
        {"ci_verdict": "green"},
        {"digest": "f" * 64},
    ],
)
def test_invalid_events_fail_closed(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        event(**changes)


def test_event_is_frozen() -> None:
    with pytest.raises(ValidationError):
        event().title = "changed"


def test_registration_is_one_duty_critical_topic() -> None:
    resolved = resolve_event_type("pr.state.observed")
    assert [(r.topic, r.tier) for r in resolved] == [
        ("onex.evt.omnimarket.pr-state-observed.v1", EnumDurabilityTier.DUTY_CRITICAL)
    ]


def test_emit_contract_declares_observation_and_both_terminal_outcomes() -> None:
    from pathlib import Path

    import yaml

    path = (
        Path(__file__).parents[4]
        / "src/omnimarket/nodes/node_pr_state_emit_effect/contract.yaml"
    )
    contract = yaml.safe_load(path.read_text())
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.pr-state-observed.v1",
        "onex.evt.omnimarket.pr-state-emit-completed.v1",
        "onex.evt.omnimarket.pr-state-emit-failed.v1",
    ]
    assert contract["runtime_dispatch"]["terminal_events"] == {
        "success": "onex.evt.omnimarket.pr-state-emit-completed.v1",
        "failure": "onex.evt.omnimarket.pr-state-emit-failed.v1",
    }
    assert (
        contract["terminal_event"]
        == contract["runtime_dispatch"]["terminal_events"]["success"]
    )
