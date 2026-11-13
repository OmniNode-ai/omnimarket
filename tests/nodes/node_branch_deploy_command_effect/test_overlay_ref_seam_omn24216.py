# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The optional overlays revision the branch deploy command carries to the deploy agent (OMN-24216).

The command effect puts an optional 40-hex ``overlay_ref`` into the signed rebuild command it publishes, so the
deploy agent (omnibase_infra PR 5946) mounts that overlays revision for the deploy. These tests pin the seam:
the envelope carries the revision when set, omits the key when empty (byte-identical to before), stays within
the agent's wire contract, and the full orchestrator chain threads the revision through the real command effect.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.branch_deploy_command import ModelBranchDeployCommandRequest
from omnimarket.nodes.node_branch_deploy_command_effect.handlers.handler_branch_deploy_command import (
    HandlerBranchDeployCommand,
    _sign_envelope,
)
from omnimarket.nodes.node_branch_deploy_orchestrator.models import (
    ModelBranchDeployRequest,
)
from tests.nodes.node_branch_deploy_command_effect.support import (
    SIGNING_ENV,
    SIGNING_KEY,
    FakeBus,
    overlay,
    request,
)
from tests.nodes.node_branch_deploy_orchestrator.support import (
    FLOWS,
    Plant,
)
from tests.nodes.node_branch_deploy_orchestrator.support import (
    request as orch_request,
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


async def _published_envelope(**cells: Any) -> dict[str, Any]:
    """The envelope the handler publishes for the given request cells, straight off the fake bus."""
    bus = FakeBus()
    handler = HandlerBranchDeployCommand(event_bus=bus, overlay=overlay())
    await handler.handle(request(**cells))
    return json.loads(bus.published[0][2])


async def test_golden_overlay_ref_envelope() -> None:
    """A command with an overlays revision publishes it in the envelope, within the wire contract, signed."""
    wire = _wire()
    envelope = await _published_envelope(overlay_ref="e" * 40)
    assert envelope["overlay_ref"] == "e" * 40
    assert _is_subset(envelope, wire["rebuild_requested_fields"])
    assert re.fullmatch(wire["overlay_ref_pattern"], envelope["overlay_ref"])
    signed = _sign_envelope(
        {k: v for k, v in envelope.items() if k != "_signature"}, SIGNING_KEY
    )
    assert signed["_signature"] == envelope["_signature"]


def test_golden_chain_threads_overlay_ref() -> None:
    """The full chain carries the overlays revision into the deploy command the agent receives."""
    plant = Plant(**FLOWS["golden"])
    plant.run(orch_request().model_copy(update={"overlay_ref": "e" * 40}))
    assert plant.commands[0]["overlay_ref"] == "e" * 40
    assert plant.completed is not None
    assert plant.completed.outcome == "deployed"


def test_golden_chain_rollback_clears_overlay_ref() -> None:
    """A rollback command carries no overlays revision, so the agent clears it."""
    plant = Plant(**FLOWS["wrong_build"])
    plant.run(orch_request().model_copy(update={"overlay_ref": "e" * 40}))
    assert plant.commands[0]["overlay_ref"] == "e" * 40
    assert "overlay_ref" not in plant.commands[1]


async def test_control_empty_overlay_ref_is_absent() -> None:
    """A command without an overlays revision publishes no key, so an implementation that always emits it fails."""
    envelope = await _published_envelope()
    assert "overlay_ref" not in envelope


def test_error_malformed_overlay_ref_rejected() -> None:
    for bad in ("not-a-sha", "E" * 40, "e" * 39):
        with pytest.raises(ValidationError):
            ModelBranchDeployCommandRequest(
                correlation_id="00000000-0000-0000-0000-000000000001",
                mode="deploy",
                runtime_lane="dev",
                repo="omnibase_infra",
                pr_number=1,
                git_ref="a" * 40,
                rollback_actor="a",
                rollback_reason="r",
                requested_by="u",
                overlay_ref=bad,
            )
        with pytest.raises(ValidationError):
            ModelBranchDeployRequest(
                correlation_id="00000000-0000-0000-0000-000000000001",
                host="h",
                unit="u",
                actor="a",
                runtime_lane="dev",
                repo="omnibase_infra",
                pr_number=1,
                head_sha="a" * 40,
                rollback_head_sha="b" * 40,
                rollback_image="i",
                rollback_reason="r",
                overlay_ref=bad,
            )


async def test_error_no_signing_key_refuses_without_publishing() -> None:
    bus = FakeBus()
    handler = HandlerBranchDeployCommand(event_bus=bus, overlay=overlay(), environ={})
    result = await handler.handle(request(overlay_ref="e" * 40))
    assert result.status == "refused"
    assert bus.published == []


async def test_failing_control_trimmed_field_list_fails_subset_check() -> None:
    wire = _wire()
    envelope = await _published_envelope(overlay_ref="e" * 40)
    trimmed = [f for f in wire["rebuild_requested_fields"] if f != "overlay_ref"]
    assert not _is_subset(envelope, trimmed)
