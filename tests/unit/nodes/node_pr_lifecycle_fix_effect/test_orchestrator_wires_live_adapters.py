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

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli import (
    GitHubCliAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_pr_polish_dispatch import (
    PrPolishDispatchAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
    _NoopAgentDispatchAdapter,
    _NoopGitHubAdapter,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    HandlerPrLifecycleOrchestrator,
)


@pytest.mark.unit
class TestOrchestratorWiresLiveAdapters:
    def test_default_fix_handler_has_live_adapters_not_noop(self) -> None:
        orch = HandlerPrLifecycleOrchestrator()
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
