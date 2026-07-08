# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publisher <-> consumer parity for the occ-autobind command (OMN-13990).

The born-path bug was a shape mismatch: the publisher emitted
``{event_id, topic, pr_head_sha, ticket, ...}`` which never validated against
``ModelPrLifecycleFixCommand`` (``extra='forbid'``), so ``payload_type_match``
rejected it and the command was silently DLQ'd — the emitter never fired.

These tests pin the fix end-to-end: the published payload, round-tripped through
JSON (the wire), validates as ``ModelPrLifecycleFixCommand`` with the autobind
block reason AND routes to the OCC autobind adapter through the real handler.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)

_SCRIPT = (
    Path(__file__).resolve().parents[4] / "scripts" / "publish_occ_autobind_command.py"
)


def _load_publisher() -> object:
    spec = importlib.util.spec_from_file_location(
        "publish_occ_autobind_command", _SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingAutobindAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None]] = []

    async def autobind_evidence_source(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> str:
        self.calls.append((repo, pr_number, ticket_id))
        return f"autobound OCC for {repo}#{pr_number}"


@pytest.mark.unit
class TestPublisherConsumerParity:
    def test_block_reason_literal_matches_enum(self) -> None:
        module = _load_publisher()
        block_reason = module._BLOCK_REASON_AUTOBIND  # type: ignore[attr-defined]
        assert block_reason == EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND.value

    def test_payload_validates_as_fix_command(self) -> None:
        module = _load_publisher()
        payload = module.build_payload(  # type: ignore[attr-defined]
            "OmniNode-ai/omnimarket", 42, "OMN-1234", str(uuid4())
        )
        # Round-trip through the wire exactly as the runtime consumes it.
        wire = json.loads(json.dumps(payload))
        command = ModelPrLifecycleFixCommand.model_validate(wire)
        assert (
            command.block_reason is EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND
        )
        assert command.pr_number == 42
        assert command.repo == "OmniNode-ai/omnimarket"
        assert command.ticket_id == "OMN-1234"

    def test_payload_has_no_extra_keys(self) -> None:
        # extra='forbid' means any stray key (the old event_id/topic/pr_head_sha)
        # would raise here — this locks the exact command shape.
        module = _load_publisher()
        payload = module.build_payload(  # type: ignore[attr-defined]
            "OmniNode-ai/omnimarket", 7, "OMN-1", str(uuid4())
        )
        assert set(payload) == {
            "correlation_id",
            "pr_number",
            "repo",
            "block_reason",
            "ticket_id",
            "requested_at",
        }

    def test_ticketless_payload_uses_none(self) -> None:
        module = _load_publisher()
        payload = module.build_payload(  # type: ignore[attr-defined]
            "OmniNode-ai/omnimarket", 9, "", str(uuid4())
        )
        command = ModelPrLifecycleFixCommand.model_validate(
            json.loads(json.dumps(payload))
        )
        assert command.ticket_id is None

    async def test_published_command_routes_to_autobind_adapter(self) -> None:
        """The full born-path seam: publish -> wire -> validate -> route -> adapter."""
        module = _load_publisher()
        payload = module.build_payload(  # type: ignore[attr-defined]
            "OmniNode-ai/omnibase_infra", 2043, "OMN-9999", str(uuid4())
        )
        command = ModelPrLifecycleFixCommand.model_validate(
            json.loads(json.dumps(payload))
        )

        recording = _RecordingAutobindAdapter()
        handler = HandlerPrLifecycleFix(occ_autobind_adapter=recording)
        result = await handler.handle(command)

        assert result.fix_applied is True
        assert result.error is None
        assert recording.calls == [("OmniNode-ai/omnibase_infra", 2043, "OMN-9999")]
