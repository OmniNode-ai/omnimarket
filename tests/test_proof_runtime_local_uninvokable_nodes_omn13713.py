# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13713 proof: previously-uninvokable omnimarket nodes now dispatch.

Before this ticket both contracts failed at dispatch over RuntimeLocal:

* ``node_verify_effect`` — event-driven path raised
  ``could not resolve an initial-payload model for contract 'verify_effect'``
  because no top-level ``input_model`` was declared.
* ``node_agent_learning_retrieval_effect`` — the entry-point marker class
  ``NodeAgentLearningRetrievalEffect`` was wired as the handler; it has no
  ``handle()`` method, so dispatch raised
  ``Handler NodeAgentLearningRetrievalEffect has no handle()/run()/execute()``.

These tests run each node end-to-end over the in-memory bus and assert
COMPLETED.
"""

from __future__ import annotations

import json
from pathlib import Path

from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from tests.runtime_local_compat import RuntimeLocal

_NODES = Path(__file__).resolve().parents[1] / "src/omnimarket/nodes"
VERIFY_CONTRACT = _NODES / "node_verify_effect/contract.yaml"
RETRIEVAL_CONTRACT = _NODES / "node_agent_learning_retrieval_effect/contract.yaml"


def test_verify_effect_resolves_input_model_and_completes(tmp_path: Path) -> None:
    """node_verify_effect dispatches over the local bus (input_model resolved)."""
    runtime = RuntimeLocal(
        workflow_path=VERIFY_CONTRACT,
        state_root=tmp_path / "state",
        timeout=30,
    )
    result = runtime.run()

    assert result == EnumWorkflowResult.COMPLETED, (
        f"verify_effect did not complete: {result}"
    )
    assert runtime.exit_code == 0
    state = json.loads((tmp_path / "state" / "workflow_result.json").read_text())
    assert state["result"] == "completed"


def test_agent_learning_retrieval_dispatches_with_handler(tmp_path: Path) -> None:
    """node_agent_learning_retrieval_effect runs a real handler and degrades.

    With no Qdrant backend wired into the local container the handler returns an
    honest empty result rather than raising; the node completes over the bus.
    """
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps({"repo": "omnimarket"}))

    runtime = RuntimeLocal(
        workflow_path=RETRIEVAL_CONTRACT,
        state_root=tmp_path / "state",
        input_path=input_path,
        timeout=30,
    )
    result = runtime.run()

    assert result == EnumWorkflowResult.COMPLETED, (
        f"agent_learning_retrieval did not complete: {result}"
    )
    assert runtime.exit_code == 0


def test_agent_learning_handler_returns_empty_without_backend() -> None:
    """Direct handler proof: empty, honest result when no backend is injected."""
    import asyncio

    from omnimarket.nodes.node_agent_learning_retrieval_effect.handlers.handler_agent_learning_retrieval import (
        HandlerAgentLearningRetrieval,
    )
    from omnimarket.nodes.node_agent_learning_retrieval_effect.models.model_request import (
        ModelAgentLearningRetrievalRequest,
    )

    handler = HandlerAgentLearningRetrieval()
    response = asyncio.run(
        handler.handle(ModelAgentLearningRetrievalRequest(repo="omnimarket"))
    )

    assert response.matches == ()
    assert response.error_matches_count == 0
    assert response.context_matches_count == 0
    assert response.query_ms >= 0
