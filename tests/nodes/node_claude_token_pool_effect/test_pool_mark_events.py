# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every change of a token mark leaves one typed event: who, when, verb, host, label, change, cause (OMN-19998).

Before this change a mark (a limit, a revoke, a clear) left no record but the lane's own closing
line: no events file existed on any host. The pool is the real handler over a temporary local store;
the sink is the in-memory sink, a sink whose bus refuses, or none. The failing control is the pool
built without a sink (the old behaviour): the same operations leave nothing, and the result says so.
Every token is fake and assembled at run time.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr, ValidationError

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_token_pool_effect.handlers.adapter_pool_mark_events import (
    BusPoolMarkEventSink,
    InMemoryPoolMarkEventSink,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    UNATTRIBUTED,
    HandlerClaudeTokenPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool_mark_event import (
    ModelPoolMarkEvent,
    ModelPoolMarkEventsSettings,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationMark,
    ModelRotationOverlay,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 14, 17, 0, tzinfo=UTC)
NODE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_claude_token_pool_effect"
)
TOPIC = "onex.evt.omnimarket.claude-pool-mark-changed.v1"
SETTINGS = ModelPoolMarkEventsSettings(topic=TOPIC, actor_env="ONEX_LANE")


def fake_token(label: str) -> str:
    return "sk-ant-" + "oat01-" + (label.replace("-", "x") * 40)[:96]


class Clock:
    def __init__(self) -> None:
        self.at = T0

    def __call__(self) -> datetime:
        return self.at


class RefusingSink:
    def publish(self, event: ModelPoolMarkEvent) -> None:
        raise RuntimeError("bus refused: no_consumer_for_topic")


def pool_with(
    tmp: Path,
    sink: Any = None,
    *,
    actor: str | None = "tick-unit",
    clock: Clock | None = None,
) -> HandlerClaudeTokenPool:
    pool = HandlerClaudeTokenPool(
        store=LocalByokCredentialStore(db_path=tmp / "store" / "e.sqlite"),
        status_path=tmp / "cache" / "claude-token-pool.json",
        host="h101",
        now=clock or Clock(),
        mark_event_sink=sink,
        default_actor=actor,
    )
    for label in ("acct-a", "onexbot"):
        pool.handle(
            ModelClaudeTokenPoolRequest(
                operation="add",
                label=label,
                setup_token_output=SecretStr(fake_token(label)),
            )
        )
    return pool


def test_golden_a_limit_then_a_revoke_then_a_sync_clear_each_leave_one_event(
    tmp_path: Path,
) -> None:
    sink = InMemoryPoolMarkEventSink()
    clock = Clock()
    pool = pool_with(tmp_path, sink, clock=clock)
    assert sink.events == []  # adding a token changes no mark

    limited = pool.handle(
        ModelClaudeTokenPoolRequest(
            operation="mark_limited",
            label="acct-a",
            seconds=3600,
            exact=True,
            actor="lane-x",
        )
    )
    revoked = pool.handle(
        ModelClaudeTokenPoolRequest(
            operation="mark_revoked", label="onexbot", cause="challenged"
        )
    )
    upgraded = pool.handle(
        ModelClaudeTokenPoolRequest(operation="mark_revoked", label="onexbot")
    )
    cleared = pool.handle(
        ModelClaudeTokenPoolRequest(
            operation="sync_marks",
            marks=(
                ModelRotationMark(label="acct-a"),
                ModelRotationMark(label="onexbot"),
            ),
            marks_at=clock.at + timedelta(seconds=1),
        )
    )

    got = [(e.who, e.verb, e.label, e.change, e.cause) for e in sink.events]
    assert got == [
        ("lane-x", "mark_limited", "acct-a", "limited", "limit_notice_exact"),
        ("tick-unit", "mark_revoked", "onexbot", "challenged", "challenged"),
        ("tick-unit", "mark_revoked", "onexbot", "revoke_confirmed", "revoked"),
        ("tick-unit", "sync_marks", "acct-a", "limit_cleared", "marks_source_sync"),
        ("tick-unit", "sync_marks", "onexbot", "revoke_cleared", "marks_source_sync"),
    ]
    assert all(e.host == "h101" and e.at == T0 for e in sink.events)
    for result in (limited, revoked, upgraded, cleared):
        assert result.mark_events_state == "published"
        assert result.mark_events_published == len(result.mark_events) >= 1
    # an event names labels and facts only: never a token value or its fingerprint
    dumped = json.dumps([e.model_dump(mode="json") for e in sink.events])
    assert "sk-ant" not in dumped
    assert "fingerprint" not in dumped


def test_golden_the_actor_the_request_names_beats_the_default_and_the_default_beats_unattributed(
    tmp_path: Path,
) -> None:
    named = InMemoryPoolMarkEventSink()
    pool_with(tmp_path / "a", named).handle(
        ModelClaudeTokenPoolRequest(
            operation="mark_limited", label="acct-a", seconds=60, actor="lane-9"
        )
    )
    default = InMemoryPoolMarkEventSink()
    pool_with(tmp_path / "b", default).handle(
        ModelClaudeTokenPoolRequest(
            operation="mark_limited", label="acct-a", seconds=60
        )
    )
    nobody = InMemoryPoolMarkEventSink()
    pool_with(tmp_path / "c", nobody, actor=None).handle(
        ModelClaudeTokenPoolRequest(
            operation="mark_limited", label="acct-a", seconds=60
        )
    )
    assert [named.events[0].who, default.events[0].who, nobody.events[0].who] == [
        "lane-9",
        "tick-unit",
        UNATTRIBUTED,
    ]


def test_error_a_refused_publication_is_named_and_the_mark_still_stands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pool = pool_with(tmp_path, RefusingSink())
    result = pool.handle(
        ModelClaudeTokenPoolRequest(
            operation="mark_limited", label="acct-a", seconds=3600
        )
    )
    assert result.mark_events_state == "failed"
    assert result.mark_events_published == 0
    assert result.mark_events_error is not None
    assert "no_consumer_for_topic" in result.mark_events_error
    # the pool kept the limit: the refused event never undoes the change
    assert result.limited == 1
    err = capsys.readouterr().err
    assert "mark event not published" in err
    assert "label=acct-a" in err
    assert "sk-ant" not in err


def test_error_a_dry_run_changes_nothing_and_leaves_no_event(tmp_path: Path) -> None:
    sink = InMemoryPoolMarkEventSink()
    pool = pool_with(tmp_path, sink)
    result = pool.handle(
        ModelClaudeTokenPoolRequest(
            operation="mark_limited", label="acct-a", seconds=60, dry_run=True
        )
    )
    assert sink.events == []
    assert result.mark_events == ()
    assert result.mark_events_state is None


def test_failing_control_without_a_sink_the_same_change_leaves_no_event_and_says_so(
    tmp_path: Path,
) -> None:
    """The old behaviour: the pool with no sink. The same operation changes the mark and records nothing."""
    result = pool_with(tmp_path, None).handle(
        ModelClaudeTokenPoolRequest(
            operation="mark_limited", label="acct-a", seconds=3600
        )
    )
    assert result.limited == 1
    assert result.mark_events_state == "not_configured"
    assert result.mark_events_published == 0
    assert len(result.mark_events) == 1  # decided, and named, but published nowhere


def test_a_change_that_moves_no_mark_leaves_no_event(tmp_path: Path) -> None:
    sink = InMemoryPoolMarkEventSink()
    pool = pool_with(tmp_path, sink)
    for operation in ("status", "record_use"):
        pool.handle(
            ModelClaudeTokenPoolRequest(
                operation=operation,
                label="acct-a" if operation == "record_use" else None,
            )
        )
    assert sink.events == []


def test_the_bus_sink_publishes_the_event_on_the_overlays_topic_and_a_subscriber_validates_it() -> (
    None
):
    """The in-memory bus leg of the seam: what the pool publishes is what a consumer validates."""
    from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

    received: list[ModelPoolMarkEvent] = []

    class Bus:
        async def publish(self, topic: str, key: bytes, value: bytes) -> None:
            bus = EventBusInmemory(environment="pool-mark-seam", group="pool-mark-seam")
            await bus.start()

            async def on_message(message: Any) -> None:
                received.append(ModelPoolMarkEvent.model_validate_json(message.value))

            await bus.subscribe(topic, on_message=on_message, group_id="seam")
            await bus.publish(topic, key, value)
            await asyncio.sleep(0.05)
            await bus.close()

    @asynccontextmanager
    async def open_bus() -> AsyncIterator[Bus]:
        yield Bus()

    event = ModelPoolMarkEvent(
        who="lane-x",
        at=T0,
        verb="mark_limited",
        host="h101",
        label="acct-a",
        change="limited",
        cause="limit_estimate",
    )
    BusPoolMarkEventSink(SETTINGS, open_bus).publish(event)
    assert received == [event]


def test_seam_the_overlays_topic_is_one_the_contract_publishes_and_the_overlay_model_ties_it_to_a_bus() -> (
    None
):
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    example = yaml.safe_load(
        (NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert example["mark_events"]["topic"] in contract["externally_consumed_topics"]
    ModelRotationOverlay.model_validate(
        example
    )  # the example loads through the real model
    no_bus = {k: v for k, v in example.items() if k != "event_bus"}
    with pytest.raises(ValidationError, match="event_bus"):
        ModelRotationOverlay.model_validate(no_bus)


def test_seam_a_renamed_event_field_is_refused_not_read_as_no_change() -> None:
    event = ModelPoolMarkEvent(
        who="lane-x",
        at=T0,
        verb="mark_limited",
        host="h101",
        label="acct-a",
        change="limited",
        cause="limit_estimate",
    )
    body = event.model_dump(mode="json")
    body["actor"] = body.pop("who")
    with pytest.raises(ValidationError):
        ModelPoolMarkEvent.model_validate(body)


def _overlay(**more: Any) -> ModelRotationOverlay:
    return ModelRotationOverlay.model_validate(
        {
            "hosts": [
                {
                    "name": name,
                    "scheduler": "systemd-user",
                    "venv": "omnimarket/.venv",
                    "token_file": ".config/omni/claude-oauth-token",
                }
                for name in ("h053", "h101")
            ],
            "marks_source": {
                "host": "h053",
                "ssh_target": None,
                "mirror_path": ".cache/omni/claude-token-pool.json",
                "max_age_seconds": 600,
            },
            "events_file": "token-rotations.jsonl",
            "unit_environment": {},
            "schedule": {"interval_seconds": 300},
            **more,
        }
    )


def test_golden_the_rotation_tick_names_its_unit_as_who_when_its_sync_clears_a_mark(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_claude_token_pool_effect.handlers.adapter_rotation_ports import (
        FileTokenFile,
    )
    from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_rotation_tick import (
        HandlerClaudeTokenRotation,
    )
    from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
        ModelClaudeTokenRotationRequest,
        ModelRotationMarksRead,
    )

    class Source:
        def read(self) -> ModelRotationMarksRead:
            return ModelRotationMarksRead(
                state="read",
                at=T0 + timedelta(seconds=30),
                marks=(
                    ModelRotationMark(label="acct-a"),
                    ModelRotationMark(label="onexbot"),
                ),
            )

    sink = InMemoryPoolMarkEventSink()
    clock = Clock()
    pool = pool_with(tmp_path, sink, actor="onex-claude-token-rotation", clock=clock)
    pool.handle(ModelClaudeTokenPoolRequest(operation="mark_revoked", label="onexbot"))
    sink.events.clear()
    clock.at = T0 + timedelta(seconds=60)
    login = tmp_path / "home" / "login"
    login.parent.mkdir(parents=True)
    login.write_text(fake_token("acct-a") + "\n", encoding="utf-8")
    login.chmod(0o600)
    HandlerClaudeTokenRotation(
        pool=pool,
        token_file=FileTokenFile(login),
        marks_source=Source(),
        overlay=_overlay(),
        host="h101",
        events_path=tmp_path / "state" / "token-rotations.jsonl",
        now=clock,
    ).handle(ModelClaudeTokenRotationRequest(host="h101"))
    assert [(e.who, e.verb, e.label, e.change) for e in sink.events] == [
        ("onex-claude-token-rotation", "sync_marks", "onexbot", "revoke_cleared")
    ]


def test_the_overlays_mark_events_select_the_sink_and_the_who_variable() -> None:
    from omnimarket.nodes.node_claude_token_pool_effect import (
        runtime_claude_token_pool as cli,
    )

    none = cli.mark_event_wiring(_overlay(), {"ONEX_LANE": "lane-1"})
    assert none == (None, None)
    memory_sink, who = cli.mark_event_wiring(
        _overlay(
            event_bus={"type": "inmemory"},
            mark_events={"topic": TOPIC, "actor_env": "ONEX_LANE"},
        ),
        {"ONEX_LANE": "lane-1"},
    )
    assert isinstance(memory_sink, InMemoryPoolMarkEventSink)
    assert who == "lane-1"
    bus_sink, who = cli.mark_event_wiring(
        _overlay(
            event_bus={"type": "kafka", "lane": "dev"},
            mark_events={"topic": TOPIC, "actor_env": "ONEX_LANE"},
        ),
        {},
    )
    assert isinstance(bus_sink, BusPoolMarkEventSink)
    assert who is None
