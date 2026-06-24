# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression test for OMN-9284.

Pre-9284 the orchestrator wired ``HandlerPrLifecycleFix()`` with no
adapters, which defaulted to the ``_Noop*`` adapters — every Track B
(FIXING phase) dispatch reported ``fix_applied=True`` with zero external
side effect. This test locks in the wiring so any future regression back
to no-op adapters fails loudly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli import (
    GitHubCliAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_occ_autobind import (
    OccAutobindAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_occ_contract import (
    OccContractAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_pr_polish_dispatch import (
    PrPolishDispatchAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
    _NoopAgentDispatchAdapter,
    _NoopGitHubAdapter,
    _NoopOccAutobindAdapter,
    _NoopOccContractAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    HandlerPrLifecycleOrchestrator,
)


@pytest.mark.unit
class TestOrchestratorWiresLiveAdapters:
    def test_default_fix_handler_has_live_adapters_not_noop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:

        monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
        orch = HandlerPrLifecycleOrchestrator(
            event_bus=MagicMock(spec=ProtocolEventBusPublisher)
        )
        orch._ensure_sub_handlers()

        fix = orch._fix
        assert isinstance(fix, HandlerPrLifecycleFix), (
            "orchestrator must wire real HandlerPrLifecycleFix, not a stub — "
            "the stub path is import-error fallback only"
        )
        assert isinstance(fix._github, GitHubCliAdapter), (
            "regression: OMN-9284 — orchestrator reverted to _NoopGitHubAdapter; "
            "Track B FIX intents will silently no-op. Re-wire GitHubCliAdapter."
        )
        assert isinstance(fix._agent, PrPolishDispatchAdapter), (
            "regression: OMN-9284 — orchestrator reverted to "
            "_NoopAgentDispatchAdapter; pr-polish sub-agents will not spawn. "
            "Re-wire PrPolishDispatchAdapter."
        )
        assert not isinstance(fix._github, _NoopGitHubAdapter)
        assert not isinstance(fix._agent, _NoopAgentDispatchAdapter)
        # OMN-13317 F1: the Evidence-Source autobind path must wire the live
        # OccAutobindAdapter — a no-op here means a Receipt-Gate
        # Evidence-Source failure silently reports fix_applied=True with no bind.
        assert isinstance(fix._occ, OccContractAdapter)
        assert isinstance(fix._occ_autobind, OccAutobindAdapter)
        assert not isinstance(fix._occ, _NoopOccContractAdapter)
        assert not isinstance(fix._occ_autobind, _NoopOccAutobindAdapter)
