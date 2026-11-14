# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The signing key and the deploy agent's key meet at one check value, and a command is not published when they differ (OMN-24396).

* seam: the signer's check value for the vector's key equals the value node_deploy_agent_key_check_effect serves for it
  (``key_check_vector.json`` of that node's tests, which this test reads too), so a change on either side fails a test;
* refusals: a differing key (typed, naming both check values) and a check value the node did not serve each refuse and
  publish nothing; an overlay that does not ask for the check publishes and says it did not check; the signing key is in
  no detail;
* controls: the same request publishes when the agent holds the node's key, so the red is the check's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_branch_deploy_command_effect.handlers.handler_branch_deploy_command import (
    HandlerBranchDeployCommand,
)
from omnimarket.nodes.node_deploy_agent_key_check_effect.models.model_deploy_agent_key_check import (
    KEY_CHECK_LABEL,
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
WIRE_FILE = (
    Path(__file__).resolve().parents[1]
    / "node_deploy_agent_key_check_effect/key_check_vector.json"
)


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SIGNING_ENV, SIGNING_KEY)


def _vector() -> dict[str, str]:
    vector: dict[str, str] = json.loads(WIRE_FILE.read_text())
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
    assert result.signer_key_check == key_check_value(SIGNING_KEY)
    assert result.agent_key_check == OTHER_CHECK
    assert SIGNING_KEY not in result.detail


async def test_match_publishes_the_signed_command() -> None:
    bus = FakeBus()
    handler = HandlerBranchDeployCommand(
        event_bus=bus, overlay=overlay(), key_check_reader=matching_reader()
    )
    result = await handler.handle(request())

    assert result.status == "published"
    assert [topic for topic, _, _ in bus.published] == [TOPIC]


async def test_unserved_check_value_refuses_and_publishes_nothing() -> None:
    bus = FakeBus()
    reader = FakeKeyCheckReader(
        "refused", detail="the secret named by the overlay is not set"
    )
    handler = HandlerBranchDeployCommand(
        event_bus=bus, overlay=overlay(), key_check_reader=reader
    )
    result = await handler.handle(request())

    assert result.status == "refused"
    assert bus.published == []
    assert (
        "was not served (the secret named by the overlay is not set)" in result.detail
    )
    assert result.signer_key_check == result.agent_key_check == ""


async def test_overlay_that_does_not_ask_for_the_check_publishes_and_says_it_did_not_check() -> (
    None
):
    bus = FakeBus()
    for asked in (None, False):
        unasked = overlay().model_copy(update={"check_agent_key": asked})
        handler = HandlerBranchDeployCommand(event_bus=bus, overlay=unasked)
        result = await handler.handle(request())
        assert result.status == "published"
        assert "was not checked against the deploy agent's key" in result.detail
    assert [topic for topic, _, _ in bus.published] == [TOPIC, TOPIC]


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
