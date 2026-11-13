# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden, error and failing-control chains of the branch deploy command effect (OMN-24027).

* golden: deploy -> one signed message on the contract topic; rollback -> no open_pr, image_ref present;
* error: signing key not set -> refused; publish error -> failed;
* control: a tampered git_ref changes the recomputed signature; the signing key never leaks into detail.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from omnimarket.nodes.node_branch_deploy_command_effect.handlers.handler_branch_deploy_command import (
    HandlerBranchDeployCommand,
)
from tests.nodes.node_branch_deploy_command_effect.support import (
    HEAD,
    RUN_ID,
    SIGNING_ENV,
    SIGNING_KEY,
    FailingBus,
    FakeBus,
    overlay,
    request,
)

pytestmark = pytest.mark.unit

TOPIC = "onex.cmd.deploy.rebuild-requested.v1"


def _expected_signature(body: dict) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(SIGNING_KEY.encode(), raw, hashlib.sha256).hexdigest()


@pytest.mark.unit
async def test_golden_deploy_published(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SIGNING_ENV, SIGNING_KEY)
    bus = FakeBus()
    handler = HandlerBranchDeployCommand(event_bus=bus, overlay=overlay())
    result = await handler.handle(request(mode="deploy"))

    assert result.status == "published"
    assert result.mode == "deploy"
    assert result.correlation_id == RUN_ID
    assert len(bus.published) == 1
    topic, key, value = bus.published[0]
    assert topic == TOPIC
    assert key == str(RUN_ID).encode()
    payload = json.loads(value)
    body = {k: v for k, v in payload.items() if k != "_signature"}
    expected = {
        "correlation_id": str(RUN_ID),
        "requested_by": "user-a",
        "scope": "runtime",
        "runtime_lane": "lane-a",
        "build_source": "workspace",
        "git_ref": HEAD,
        "rollback": {"actor": "actor-a", "reason": "reason-a"},
        "open_pr": f"omnibase_infra#5917@{HEAD}",
    }
    assert body == expected
    assert payload["_signature"] == _expected_signature(body)


@pytest.mark.unit
async def test_golden_rollback_published(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SIGNING_ENV, SIGNING_KEY)
    bus = FakeBus()
    handler = HandlerBranchDeployCommand(event_bus=bus, overlay=overlay())
    result = await handler.handle(
        request(mode="rollback", image_ref="registry.example/omni:rollback")
    )

    assert result.status == "published"
    assert result.mode == "rollback"
    topic, _key, value = bus.published[0]
    assert topic == TOPIC
    payload = json.loads(value)
    body = {k: v for k, v in payload.items() if k != "_signature"}
    assert "open_pr" not in body
    assert body["image_ref"] == "registry.example/omni:rollback"
    assert payload["_signature"] == _expected_signature(body)


@pytest.mark.unit
async def test_error_chain_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SIGNING_ENV, raising=False)
    bus = FakeBus()
    handler = HandlerBranchDeployCommand(event_bus=bus, overlay=overlay())
    result = await handler.handle(request(mode="deploy"))

    assert result.status == "refused"
    assert bus.published == []
    assert SIGNING_KEY not in result.detail


@pytest.mark.unit
async def test_error_chain_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SIGNING_ENV, SIGNING_KEY)
    handler = HandlerBranchDeployCommand(event_bus=FailingBus(), overlay=overlay())
    result = await handler.handle(request(mode="deploy"))

    assert result.status == "failed"
    assert result.detail == "ConnectionError"


@pytest.mark.unit
async def test_failing_control_tampered_git_ref_changes_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SIGNING_ENV, SIGNING_KEY)
    bus = FakeBus()
    handler = HandlerBranchDeployCommand(event_bus=bus, overlay=overlay())
    await handler.handle(request(mode="deploy"))

    _topic, _key, value = bus.published[0]
    payload = json.loads(value)
    published_sig = payload["_signature"]
    tampered = dict(payload)
    tampered["git_ref"] = tampered["git_ref"][:-1] + (
        "b" if tampered["git_ref"][-1] != "b" else "c"
    )
    body = {k: v for k, v in tampered.items() if k != "_signature"}
    recomputed = _expected_signature(body)
    assert recomputed != published_sig


@pytest.mark.unit
async def test_signing_key_never_in_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SIGNING_ENV, SIGNING_KEY)
    bus = FakeBus()
    handler = HandlerBranchDeployCommand(event_bus=bus, overlay=overlay())
    result = await handler.handle(request(mode="deploy"))

    assert result.status == "published"
    assert SIGNING_KEY not in result.detail
