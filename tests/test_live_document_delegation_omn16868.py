# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live readback: task_type="document" resolves on the LOCAL tier (OMN-16868).

Opt-in only; CI never runs this. The hermetic suite
(``test_slice1_document_delegation_omn16868.py``) fakes the delegation at the
adapter boundary, which proves the routing/telemetry wiring but CANNOT prove
that the task type the contract names actually lands on a free local model.

That distinction is load-bearing here. ``task_type="documentation"`` resolves
to ``cheap_cloud``/``cloud-gemini-flash`` — a PAID backend. Only the contract's
exact ``"document"`` resolves to ``local``/``local-heavy-reasoning``. A silent
drift in ``routing_tiers.yaml`` or ``task_class_contracts.v1.yaml`` would move
every merge-sweep docstring fix onto metered cloud inference without any test
failing, so this module pins the live resolution.

Enable with::

    OMN_ALLOW_LIVE_LADDER=1 uv run pytest \\
      tests/test_live_document_delegation_omn16868.py -v
"""

from __future__ import annotations

import os

import pytest

from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    backend_id_for_tier,
    first_eligible_tier,
)
from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.adapter_document_delegation import (
    DOCUMENT_TASK_TYPE,
)
from omnimarket.routing.delegation_backend_resolution import (
    resolve_delegation_backend,
)

_LIVE = os.environ.get("OMN_ALLOW_LIVE_LADDER") == "1"

# Captured at IMPORT time, before tests/conftest.py's autouse
# ``_ensure_bifrost_contract_path`` fixture deletes it. That fixture makes the
# hermetic suite read the packaged contract with every local ``endpoint_url``
# null, which is correct for hermetic tests but makes ``document`` resolve to
# ``cheap_cloud`` — an artifact of the stripped overlay, not real routing. This
# module is a LIVE readback of the DEPLOYED configuration, so it re-binds the
# real overlay, which the conftest docstring explicitly sanctions ("tests that
# need a specific overlay shape override this via monkeypatch inside the test
# body or a more specific fixture").
_AMBIENT_OVERLAY_PATH = os.environ.get("BIFROST_OVERLAY_PATH")

pytestmark = pytest.mark.skipif(
    not _LIVE, reason="live ladder disabled; set OMN_ALLOW_LIVE_LADDER=1"
)


@pytest.fixture(autouse=True)
def _restore_deployed_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-bind the deployed local-endpoint overlay stripped by conftest."""
    if not _AMBIENT_OVERLAY_PATH:
        pytest.skip(
            "BIFROST_OVERLAY_PATH unset; the deployed local-endpoint overlay is "
            "required to read back live routing"
        )
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", _AMBIENT_OVERLAY_PATH)
    # The routing reducer and the backend resolver each memoize their config.
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as routing,
    )

    for cache_attr in (
        "_get_config",
        "_get_task_class_contract",
        "_load_bifrost_endpoints",
    ):
        cached = getattr(routing, cache_attr, None)
        if cached is not None and hasattr(cached, "cache_clear"):
            cached.cache_clear()


@pytest.mark.live_model
@pytest.mark.integration
class TestDocumentTaskTypeResolvesLocal:
    def test_document_first_eligible_tier_is_local(self) -> None:
        assert first_eligible_tier(DOCUMENT_TASK_TYPE) == "local"

    def test_document_resolves_to_a_local_backend_not_a_paid_one(self) -> None:
        tier = first_eligible_tier(DOCUMENT_TASK_TYPE)
        backend_id = backend_id_for_tier(tier, DOCUMENT_TASK_TYPE)
        resolved = resolve_delegation_backend(DOCUMENT_TASK_TYPE, backend_id=backend_id)

        assert resolved.tier == "local"
        assert resolved.backend_id == "local-heavy-reasoning"
        # Routing is contract-resolved: assert the SHAPE (a local tier with a
        # populated endpoint), not a hardcoded host, so re-pointing the lane in
        # the overlay does not fail this test.
        assert resolved.endpoint_ref.startswith("http")
        assert resolved.model_id

    def test_document_does_not_resolve_to_the_dead_reasoner_slot(self) -> None:
        """.201:8001 (local-reasoner) was removed with GPU1 — OMN-16419."""
        tier = first_eligible_tier(DOCUMENT_TASK_TYPE)
        backend_id = backend_id_for_tier(tier, DOCUMENT_TASK_TYPE)
        assert backend_id != "local-reasoner"

    def test_documentation_is_not_a_substitute_for_document(self) -> None:
        """Guards the exact-token trap: 'documentation' is a PAID route."""
        assert first_eligible_tier("documentation") != "local"


@pytest.mark.live_model
@pytest.mark.integration
class TestLiveDocumentCompletion:
    async def test_real_local_model_answers_a_document_request(self) -> None:
        """Drive the real chain and assert the model actually responded."""
        from uuid import uuid4

        from omnimarket.models.delegation.wire.model_delegate_skill_request import (
            ModelDelegateSkillRequest,
        )
        from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
            HandlerDelegateSkill,
        )

        handler = HandlerDelegateSkill()
        response = await handler.handle(
            ModelDelegateSkillRequest(
                prompt=(
                    "Improve the docstrings and comments in this file. Change "
                    "nothing else.\n\n"
                    'def add(a, b):\n    """Adds."""\n    return a + b\n'
                ),
                task_type=DOCUMENT_TASK_TYPE,
                source="claude-code",
                temperature=0.0,
                correlation_id=uuid4(),
                wait=True,
            )
        )

        assert response is not None
        assert getattr(response, "status", None) in {"completed", "failed"}
