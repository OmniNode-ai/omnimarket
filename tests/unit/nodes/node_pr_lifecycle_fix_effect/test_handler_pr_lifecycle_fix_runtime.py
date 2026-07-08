# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerPrLifecycleFixRuntime (OMN-13990).

The runtime resolver zero-arg-constructs the contract's declared handler. This
subclass must bind the LIVE OCC adapters under that path (the base handler would
default to no-ops), stay boot-resolvable (no required non-injectable ctor param),
honour explicit overrides, and still route the autobind command.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_occ_autobind import (
    OccAutobindAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_occ_contract import (
    OccContractAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    _NoopOccAutobindAdapter,
    _NoopOccContractAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix_runtime import (
    HandlerPrLifecycleFixRuntime,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)


class _RecordingAutobindAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None]] = []

    async def autobind_evidence_source(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> str:
        self.calls.append((repo, pr_number, ticket_id))
        return f"autobound OCC for {repo}#{pr_number}"


@pytest.mark.unit
class TestHandlerPrLifecycleFixRuntime:
    def test_zero_arg_binds_live_occ_adapters_not_noop(self) -> None:
        handler = HandlerPrLifecycleFixRuntime()
        assert isinstance(handler._occ_autobind, OccAutobindAdapter)
        assert isinstance(handler._occ, OccContractAdapter)
        assert not isinstance(handler._occ_autobind, _NoopOccAutobindAdapter)
        assert not isinstance(handler._occ, _NoopOccContractAdapter)

    def test_explicit_override_is_honoured(self) -> None:
        recording = _RecordingAutobindAdapter()
        handler = HandlerPrLifecycleFixRuntime(occ_autobind_adapter=recording)
        assert handler._occ_autobind is recording

    def test_no_required_non_injectable_ctor_params(self) -> None:
        # Mirrors test_handler_routing_boot_resolvable: the resolver constructs
        # this via its zero-arg path, so every ctor param must have a default.
        sig = inspect.signature(HandlerPrLifecycleFixRuntime.__init__)
        for name, param in sig.parameters.items():
            if name == "self":
                continue
            assert param.default is not inspect.Parameter.empty, (
                f"{name} must have a default so the runtime resolver can "
                "zero-arg construct HandlerPrLifecycleFixRuntime"
            )

    async def test_routes_autobind_command_to_adapter(self) -> None:
        recording = _RecordingAutobindAdapter()
        handler = HandlerPrLifecycleFixRuntime(occ_autobind_adapter=recording)
        command = ModelPrLifecycleFixCommand(
            correlation_id=uuid4(),
            pr_number=2043,
            repo="OmniNode-ai/omnibase_infra",
            block_reason=EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND,
            ticket_id="OMN-9999",
            requested_at=datetime.now(tz=UTC),
        )

        result = await handler.handle(command)

        assert result.fix_applied is True
        assert result.error is None
        assert recording.calls == [("OmniNode-ai/omnibase_infra", 2043, "OMN-9999")]
