# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The signing key and the deploy agent's key meet at one check value, and a command is not published when they differ (OMN-24396).

* seam: the node's check value for the vector's key equals the value the deploy agent computes (``key_check_vector`` of
  agent_wire.json, the same vector omnibase_infra's deploy-agent tests pin), so a change on either side fails a test;
* refusals: a differing key, an unreadable page, a page with no value and an overlay with no read configured each refuse and
  publish nothing; the signing key is in no detail;
* controls: the same request publishes when the agent holds the node's key, so the red is the check's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_branch_deploy_command_effect.handlers.handler_branch_deploy_command import (
    KEY_CHECK_LABEL,
    HandlerBranchDeployCommand,
    key_check_value,
)
from tests.nodes.node_branch_deploy_command_effect.support import (
    SIGNING_ENV,
    SIGNING_KEY,
    FakeBus,
    FakeKeyCheckReader,
    matching_reader,
    overlay,
    request,
)

pytestmark = pytest.mark.unit

TOPIC = "onex.cmd.deploy.rebuild-requested.v1"
OTHER_CHECK = "0123456789abcdef"
WIRE_FILE = Path(__file__).resolve().parent / "agent_wire.json"


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SIGNING_ENV, SIGNING_KEY)


def _vector() -> dict[str, str]:
    vector: dict[str, str] = json.loads(WIRE_FILE.read_text())["key_check_vector"]
    return vector


def test_golden_vector() -> None:
    vector = _vector()
    assert key_check_value(vector["secret"]) == vector["check"]
    assert KEY_CHECK_LABEL.decode() == vector["label"]
    assert len(vector["check"]) == 16


def test_failing_control_another_key_gives_another_value() -> None:
    assert key_check_value("another-fixture-key") != _vector()["check"]


async def test_mismatch_refuses_and_publishes_nothing() -> None:
    bus = FakeBus()
    reader = FakeKeyCheckReader("read", OTHER_CHECK)
    handler = HandlerBranchDeployCommand(
        event_bus=bus, overlay=overlay(), key_check_reader=reader
    )
    result = await handler.handle(request())

    assert result.status == "refused"
    assert bus.published == []
    assert reader.reads == 1
    assert key_check_value(SIGNING_KEY) in result.detail
    assert OTHER_CHECK in result.detail
    assert "does not match the deploy agent's key" in result.detail
    assert SIGNING_KEY not in result.detail


async def test_match_publishes_the_signed_command() -> None:
    bus = FakeBus()
    handler = HandlerBranchDeployCommand(
        event_bus=bus, overlay=overlay(), key_check_reader=matching_reader()
    )
    result = await handler.handle(request())

    assert result.status == "published"
    assert [topic for topic, _, _ in bus.published] == [TOPIC]


async def test_unreachable_page_refuses_and_publishes_nothing() -> None:
    bus = FakeBus()
    reader = FakeKeyCheckReader("unreachable", detail="TimeoutError")
    handler = HandlerBranchDeployCommand(
        event_bus=bus, overlay=overlay(), key_check_reader=reader
    )
    result = await handler.handle(request())

    assert result.status == "refused"
    assert bus.published == []
    assert "could not be read (TimeoutError)" in result.detail


async def test_page_without_a_value_refuses_and_publishes_nothing() -> None:
    bus = FakeBus()
    handler = HandlerBranchDeployCommand(
        event_bus=bus,
        overlay=overlay(),
        key_check_reader=FakeKeyCheckReader("absent"),
    )
    result = await handler.handle(request())

    assert result.status == "refused"
    assert bus.published == []
    assert "carries no key check value" in result.detail


async def test_overlay_without_the_read_refuses_and_publishes_nothing() -> None:
    bus = FakeBus()
    unconfigured = overlay().model_copy(
        update={
            "agent_health_url": None,
            "agent_health_timeout_seconds": None,
            "agent_key_check_field": None,
        }
    )
    handler = HandlerBranchDeployCommand(event_bus=bus, overlay=unconfigured)
    result = await handler.handle(request())

    assert result.status == "refused"
    assert bus.published == []
    assert "names no deploy agent health url" in result.detail


async def test_refusal_detail_is_cut_to_the_overlay_bound() -> None:
    bus = FakeBus()
    tight = overlay().model_copy(update={"detail_max_chars": 20})
    handler = HandlerBranchDeployCommand(
        event_bus=bus,
        overlay=tight,
        key_check_reader=FakeKeyCheckReader("read", OTHER_CHECK),
    )
    result = await handler.handle(request())

    assert result.status == "refused"
    assert len(result.detail) == 20


async def test_without_a_signing_key_the_agent_is_not_read() -> None:
    bus = FakeBus()
    reader = FakeKeyCheckReader("read", OTHER_CHECK)
    handler = HandlerBranchDeployCommand(
        event_bus=bus, overlay=overlay(), environ={}, key_check_reader=reader
    )
    result = await handler.handle(request())

    assert result.status == "refused"
    assert "signing key named by the overlay is not set" in result.detail
    assert reader.reads == 0
