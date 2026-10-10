"""Watcher wire ingress, freshness, peer cache and red head deduplication."""

import hashlib
import json
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.events.pr_state import ModelPrStateObservedEvent
from omnimarket.models.ci_red_triage import ci_run_failed_event_id
from omnimarket.nodes.node_pr_state_emit_effect.handlers.handler_detect_ci_red import (
    HandlerDetectCiRed,
    ModelDetectCiRedRequest,
)


def wire(pr: int = 2606, **changes: Any) -> dict[str, Any]:
    payload = {
        "repo": "omniclaude",
        "pr_number": pr,
        "state": "open",
        "head_sha": f"head-{pr}",
        "base": "main",
        "head_ref": "fix/red",
        "title": "Fix CI",
        "draft": False,
        "author": "jonah",
        "author_is_bot": False,
        "labels": [],
        "armed": True,
        "queued": False,
        "watcher_class": "armed",
        "ci_verdict": "RED",
        "red_contexts": ["CI Summary", "branch-claim-check / branch-claim-check"],
        "pending_contexts": [],
        "ci_read_at": "2026-10-08T10:00:00Z",
        "merged_at": "",
        "observed_at": "2026-10-08T10:00:00Z",
    }
    payload.update(changes)
    event = ModelPrStateObservedEvent.model_validate_json(json.dumps(payload))
    return {
        **event.model_dump(mode="json"),
        "actor": "watcher",
        "lane": "dev",
        "lane_source": "lab",
        "lane_ticket": "OMN-20730",
        "workspace_path": "/workspace",
        "turn_id": "turn",
        "hook_fired_at": event.observed_at,
        "correlation_id": "transport",
        "causation_id": "transport",
        "emitted_at": event.observed_at,
        "schema_version": "1",
    }


@pytest.mark.asyncio
async def test_live_wire_and_runtime_ingress() -> None:
    payload = wire(
        red_contexts=[
            "branch-claim-check / branch-claim-check",
            "CI Summary",
            "CI Summary",
        ]
    )
    ingress = ModelDetectCiRedRequest.model_validate(payload)
    output = await HandlerDetectCiRed().handle(ingress)
    assert len(output.events) == 1
    event = output.events[0]
    assert event.failing_checks == (
        "CI Summary",
        "branch-claim-check / branch-claim-check",
    )
    assert event.event_id == ci_run_failed_event_id(
        "omniclaude", 2606, "head-2606", event.failing_checks
    )
    assert (
        event.event_id
        == hashlib.sha256(
            b"omniclaude\n2606\nhead-2606\nCI Summary\nbranch-claim-check / branch-claim-check"
        ).hexdigest()
    )
    assert event.source_digest == payload["digest"]
    assert event.observed_at == payload["observed_at"]
    assert event.peers == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"ci_verdict": "GREEN"},
        {"ci_verdict": "PENDING"},
        {"ci_verdict": "NONE"},
        {"state": "closed"},
        {"state": "merged"},
        {"draft": True},
        {"red_contexts": []},
    ],
)
async def test_drop_non_actionable(changes: dict[str, Any]) -> None:
    assert (await HandlerDetectCiRed().handle(wire(**changes))).events == ()


@pytest.mark.asyncio
async def test_read_timestamp_does_not_trigger_second_event() -> None:
    handler = HandlerDetectCiRed()
    assert len((await handler.handle(wire())).events) == 1
    assert (
        await handler.handle(
            wire(ci_read_at="2026-10-08T11:00:00Z", observed_at="2026-10-08T11:00:00Z")
        )
    ).events == ()
    assert (
        len((await handler.handle(wire(head_sha="new-head"))).events) == 0
    )  # older observation
    assert (
        len(
            (
                await handler.handle(
                    wire(head_sha="new-head", observed_at="2026-10-08T12:00:00Z")
                )
            ).events
        )
        == 1
    )


@pytest.mark.asyncio
async def test_peers_follow_newest_open_red_observations() -> None:
    handler = HandlerDetectCiRed()
    for pr, changes in [
        (1, {}),
        (2, {}),
        (3, {}),
        (4, {"red_contexts": ["other"]}),
        (5, {"repo": "omnimarket"}),
    ]:
        await handler.handle(wire(pr, **changes))
    await handler.handle(
        wire(2, ci_verdict="GREEN", observed_at="2026-10-08T11:00:00Z")
    )
    await handler.handle(wire(3, state="closed", observed_at="2026-10-08T11:00:00Z"))
    # Delayed reds cannot replace newer green or resurrect a closed PR.
    assert (await handler.handle(wire(2))).events == ()
    assert (await handler.handle(wire(3))).events == ()
    event = (await handler.handle(wire(6))).events[0]
    assert tuple(peer.pr_number for peer in event.peers) == (1,)
    assert ("omniclaude", 3) not in handler._observations


@pytest.mark.asyncio
async def test_unarmed_red_is_emitted_and_peer_retains_armed_fact() -> None:
    handler = HandlerDetectCiRed()
    assert len((await handler.handle(wire(1, armed=False))).events) == 1
    event = (await handler.handle(wire(2))).events[0]
    assert event.peers[0].armed is False


@pytest.mark.asyncio
async def test_missing_or_invalid_digest_rejected() -> None:
    payload = wire()
    payload.pop("digest")
    with pytest.raises(ValidationError, match="digest"):
        await HandlerDetectCiRed().handle(payload)
    with pytest.raises(ValidationError, match="digest does not match"):
        await HandlerDetectCiRed().handle({**wire(), "digest": "0" * 64})


@pytest.mark.asyncio
async def test_caches_are_bounded_and_dedupe_refreshes_lru() -> None:
    handler = HandlerDetectCiRed()
    handler.MEMORY_LIMIT = 2
    for pr in (1, 2, 1, 3):
        await handler.handle(wire(pr))
    assert (
        len(handler._observations) == len(handler._newest) == len(handler._emitted) == 2
    )
    assert ("omniclaude", 2) not in handler._observations
    assert len((await handler.handle(wire(2))).events) == 1
