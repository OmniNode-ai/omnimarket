# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compaction packs carry operator words from the live session projection."""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.models.context_pack_orchestrator import (
    EnumContextPackOrchestratorStatus,
)
from omnimarket.nodes.node_context_pack_orchestrator.handlers.handler_context_pack_orchestrator import (
    HandlerContextPackOrchestrator,
)
from omnimarket.nodes.node_context_pack_orchestrator.protocols import (
    ProtocolContextTierSource,
)
from omnimarket.nodes.node_session_context_sources_effect.handlers.handler_session_context_sources import (
    HandlerSessionContextSources,
)
from tests.nodes.node_session_context_sources_effect.test_session_context_sources import (
    LIVE_SESSION,
    LIVE_UNRECORDED,
    live_projection,
)

from .conftest import FIXED_IDENTITY, command, sources

pytestmark = pytest.mark.unit


def test_compaction_pack_carries_session_projection_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_SESSION_TRANSCRIPT_ROOT", raising=False)
    monkeypatch.delenv("ONEX_LEDGER_PATH", raising=False)
    projection = live_projection()
    bound: dict[EnumContextFactor, ProtocolContextTierSource] = dict(sources())
    bound[EnumContextFactor.OPERATOR_WORDS_UNRECORDED] = HandlerSessionContextSources(
        content_reader=projection, ledger_reader=projection, tenant_ids=("tenant-a",)
    )
    built = HandlerContextPackOrchestrator(
        bound, implementation_identity=lambda: FIXED_IDENTITY
    ).handle(command(session_id=LIVE_SESSION, newest_turn="2026-10-19T08:00:00Z"))
    assert built.status is EnumContextPackOrchestratorStatus.OK, built.errors
    assert LIVE_UNRECORDED in "\n".join(message.text for message in built.messages)
    assert projection.sessions == [LIVE_SESSION]
