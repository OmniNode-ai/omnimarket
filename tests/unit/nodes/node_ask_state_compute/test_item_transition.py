# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Transitions answer the current content revision and the current CLAIM run."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_ask_state_compute as node
from omnimarket.models.model_ask_state import ModelAskStateRequest
from omnimarket.nodes.node_ask_state_compute.handlers.handler_ask_state import (
    HandlerAskState,
)

pytestmark = pytest.mark.unit
WORDS = "Build the tracked item transition effect."
REVISION = hashlib.sha256(WORDS.encode()).hexdigest()
STAMP = "2026-11-04T12:01:00Z"


def snapshot(
    *rows: str, checks: tuple[dict[str, object], ...] = (), words: str = WORDS
) -> ModelAskStateRequest:
    return ModelAskStateRequest.model_validate(
        {
            "prompts": [
                {
                    "capture_id": "capture",
                    "session_id": "session",
                    "lane": "accountable",
                    "said_at": "2026-11-04T12:00:00Z",
                    "text": words,
                    "items": [
                        {
                            "item_id": "cap-item",
                            "kind": "ask",
                            "quote": words,
                            "subject": "tracked item",
                            "confidence": 1,
                        }
                    ],
                }
            ],
            "rows": rows,
            "now": "2026-11-04T13:00:00Z",
            "since": "2026-11-04T00:00:00Z",
            "config": yaml.safe_load(
                Path(node.__file__).with_name("contract.yaml").read_text()
            )["config"],
            "completion_checks": checks,
        }
    )


def claim(
    run: str = "run-a",
    lane: str = "builder",
    stamp: str = STAMP,
    revision: str = REVISION,
) -> str:
    return f"{stamp} | CLAIM | lane={lane} | for-item=ask-item | owner=accountable | run={run} | revision={revision}"


def terminal(
    outcome: str = "done",
    run: str = "run-a",
    lane: str = "builder",
    stamp: str = "2026-11-04T12:03:00Z",
    revision: str = REVISION,
    evidence: str = "sample#42",
) -> str:
    return f"{stamp} | TERMINAL | lane={lane} | for-item=ask-item | closes-CLAIM={STAMP} | run={run} | revision={revision} | outcome={outcome} | evidence={evidence}"


def check(
    verified: bool = True,
    reference: str = "sample#42",
    evidence_class: str = "merged-pr",
    revision: str = REVISION,
    run: str = "run-a",
) -> dict[str, object]:
    return {
        "reference": reference,
        "evidence_class": evidence_class,
        "verified": verified,
        "revision": revision,
        "run_id": run,
    }


def entry(request: ModelAskStateRequest):
    return HandlerAskState().handle(request).asks[0]


def test_item_transition_terminal_merged_pr_closes_current_binding() -> None:
    got = entry(snapshot(claim(), terminal(), checks=(check(),)))
    assert got.state.value == "done"
    assert got.owner_lane == "accountable"
    assert got.running_lane is None
    assert got.revision == REVISION
    assert got.claim_run_id == "run-a"
    assert got.evidence


@pytest.mark.parametrize(
    "checks", [(), (check(False),), (check(evidence_class="open-pr"),)]
)
def test_item_transition_open_or_unchecked_pr_stays_owned(checks) -> None:
    got = entry(snapshot(claim(), terminal(), checks=checks))
    assert got.state.value == "owned"
    assert got.owner_lane == "accountable"
    assert got.running_lane is None
    assert got.orphaned


@pytest.mark.parametrize("outcome", ["blocked", "failed", "lane-error"])
def test_item_transition_failure_retains_owner_and_ends_binding_same_fold(
    outcome: str,
) -> None:
    got = entry(snapshot(claim(), terminal(outcome)))
    assert got.state.value == "owned"
    assert got.owner_lane == "accountable"
    assert got.running_lane is None
    assert got.orphaned


@pytest.mark.parametrize("outcome", ["failed", "done", "lane-error"])
@pytest.mark.parametrize("replacement_lane", ["builder", "replacement"])
def test_item_transition_replaced_run_ignores_late_terminal(
    outcome: str, replacement_lane: str
) -> None:
    replacement = claim("run-b", replacement_lane, "2026-11-04T12:02:00Z")
    got = entry(snapshot(claim(), replacement, terminal(outcome), checks=(check(),)))
    assert got.state.value == "owned"
    assert got.running_lane == replacement_lane
    assert got.owner_lane == "accountable"
    assert got.claim_run_id == "run-b"


def test_item_transition_amended_content_ignores_old_completion() -> None:
    got = entry(
        snapshot(
            claim(), terminal(), checks=(check(),), words=WORDS + " Include parking."
        )
    )
    assert got.state.value != "done"
    assert got.revision != REVISION


def test_item_transition_stale_status_does_not_change_owner_or_binding() -> None:
    stale = f"2026-11-04T12:04:00Z | STATUS | lane=old | for-item=ask-item | owner=wrong | state=done | run=run-a | revision={REVISION} | evidence=sample#42"
    got = entry(
        snapshot(
            claim(),
            claim("run-b", "replacement", "2026-11-04T12:02:00Z"),
            stale,
            checks=(check(),),
        )
    )
    assert got.state.value == "owned"
    assert got.owner_lane == "accountable"
    assert got.running_lane == "replacement"


def test_item_transition_no_run_terminal_cannot_close_modern_claim() -> None:
    old = f"2026-11-04T12:04:00Z | TERMINAL | lane=builder | outcome=done | evidence=sample#42 | closes-CLAIM={STAMP}"
    got = entry(snapshot(claim(), old, checks=(check(),)))
    assert got.state.value == "owned"
    assert got.running_lane == "builder"


def test_item_transition_amendment_in_later_capture_replaces_content() -> None:
    original = snapshot(claim(), terminal(), checks=(check(),))
    amended = original.prompts[0].model_copy(
        update={
            "capture_id": "amendment",
            "said_at": datetime(2026, 11, 4, 12, 2, tzinfo=UTC),
            "items": (
                original.prompts[0]
                .items[0]
                .model_copy(update={"quote": WORDS + " Include parking."}),
            ),
        }
    )
    got = entry(original.model_copy(update={"prompts": (*original.prompts, amended)}))
    assert got.words == WORDS + " Include parking."
    assert got.state.value != "done"
    assert got.revision != REVISION


def test_item_transition_uses_upstream_content_revision() -> None:
    """The core item's digest can cover more content than its displayed words."""
    revision = hashlib.sha256(b"full typed item including kind and subject").hexdigest()
    request = snapshot(
        claim(revision=revision),
        terminal(revision=revision),
        checks=(check(revision=revision),),
    )
    data = request.model_dump(mode="json")
    data["prompts"][0]["items"][0]["revision"] = revision
    got = entry(ModelAskStateRequest.model_validate(data))
    assert got.revision == revision
    assert got.state.value == "done"
