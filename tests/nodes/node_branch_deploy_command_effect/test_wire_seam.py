# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The envelope the handler publishes must stay within the deploy agent's wire contract (OMN-24027)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_branch_deploy_command_effect.handlers.handler_branch_deploy_command import (
    HandlerBranchDeployCommand,
    _sign_envelope,
)
from tests.nodes.node_branch_deploy_command_effect.support import (
    SIGNING_ENV,
    SIGNING_KEY,
    FakeBus,
    overlay,
    request,
)

pytestmark = pytest.mark.unit

WIRE_FILE = Path(__file__).resolve().parent / "agent_wire.json"


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SIGNING_ENV, SIGNING_KEY)


def _wire() -> dict[str, Any]:
    return json.loads(WIRE_FILE.read_text())


def _is_subset(envelope: dict[str, Any], fields: list[str]) -> bool:
    return {k for k in envelope if k != "_signature"} <= set(fields)


async def _published_envelope(mode: str) -> dict[str, Any]:
    """The envelope the handler publishes for the mode, straight off the fake bus."""
    cells = (
        {"image_ref": "registry.example/omni:rollback"} if mode == "rollback" else {}
    )
    bus = FakeBus()
    handler = HandlerBranchDeployCommand(event_bus=bus, overlay=overlay())
    await handler.handle(request(mode=mode, **cells))
    return json.loads(bus.published[0][2])


async def test_deploy_envelope_keys_are_in_rebuild_requested_fields() -> None:
    wire = _wire()
    envelope = await _published_envelope("deploy")
    assert _is_subset(envelope, wire["rebuild_requested_fields"])
    for field in wire["required_fields"]:
        assert field in envelope
    assert set(envelope["rollback"]) <= set(wire["rollback_fields"])
    assert re.fullmatch(wire["open_pr_pattern"], envelope["open_pr"])
    assert envelope["open_pr"].endswith("@" + envelope["git_ref"])


async def test_rollback_envelope_keys_are_in_rebuild_requested_fields() -> None:
    wire = _wire()
    envelope = await _published_envelope("rollback")
    assert _is_subset(envelope, wire["rebuild_requested_fields"])
    for field in wire["required_fields"]:
        assert field in envelope
    assert set(envelope["rollback"]) <= set(wire["rollback_fields"])
    assert "open_pr" not in envelope


def test_handler_signing_reproduces_the_golden_vector() -> None:
    vector = _wire()["signature_vector"]
    signed = _sign_envelope(vector["envelope"], vector["secret"])
    assert signed["_signature"] == vector["signature"]


async def test_failing_control_trimmed_field_list_fails_subset_check() -> None:
    wire = _wire()
    envelope = await _published_envelope("deploy")
    trimmed = [f for f in wire["rebuild_requested_fields"] if f != "open_pr"]
    assert not _is_subset(envelope, trimmed)


async def test_failing_control_renamed_rollback_key_is_detected() -> None:
    wire = _wire()
    envelope = await _published_envelope("deploy")
    renamed = dict(envelope)
    renamed["rollbak"] = renamed.pop("rollback")
    assert not _is_subset(renamed, wire["rebuild_requested_fields"])
