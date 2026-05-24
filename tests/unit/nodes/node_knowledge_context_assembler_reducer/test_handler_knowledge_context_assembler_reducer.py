# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for HandlerKnowledgeContextAssemblerReducer.

Pure reducer tests — no I/O. Verifies:
  1. Accumulate fragments from multiple backends
  2. Materialize produces ModelKnowledgeContextBundle
  3. Partial responses produce valid bundle
  4. Idempotent accumulation (duplicate fragment_source ignored)
  5. Status transitions: COMPLETE / PARTIAL / DEGRADED
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_knowledge_context_assembler_reducer.handlers.handler_knowledge_context_assembler_reducer import (
    HandlerKnowledgeContextAssemblerReducer,
)
from omnimarket.nodes.node_knowledge_context_assembler_reducer.models.model_knowledge_context_bundle import (
    EnumBundleStatus,
)
from omnimarket.nodes.node_knowledge_context_assembler_reducer.models.model_knowledge_context_fragment import (
    EnumFragmentSource,
    ModelKnowledgeContextFragment,
)
from omnimarket.nodes.node_knowledge_context_assembler_reducer.models.model_knowledge_context_state import (
    ModelKnowledgeContextState,
)

CORR_ID = str(uuid4())


def _make_state(
    expected_count: int = 3,
    correlation_id: str = CORR_ID,
) -> ModelKnowledgeContextState:
    return ModelKnowledgeContextState(
        correlation_id=correlation_id,
        expected_count=expected_count,
    )


def _make_fragment(
    source: EnumFragmentSource = EnumFragmentSource.CODEBASE_INTELLIGENCE,
    content: dict | None = None,
    correlation_id: str = CORR_ID,
    error: str | None = None,
) -> ModelKnowledgeContextFragment:
    return ModelKnowledgeContextFragment(
        fragment_source=source,
        content=content or {},
        correlation_id=correlation_id,
        error=error,
    )


# ---------------------------------------------------------------------------
# accumulate()
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_accumulate_single_fragment_not_complete() -> None:
    handler = HandlerKnowledgeContextAssemblerReducer()
    state = _make_state(expected_count=3)
    fragment = _make_fragment(EnumFragmentSource.CODEBASE_INTELLIGENCE)

    new_state = handler.accumulate(state, fragment)

    assert len(new_state.fragments) == 1
    assert new_state.completed is False


@pytest.mark.unit
def test_accumulate_all_fragments_marks_completed() -> None:
    handler = HandlerKnowledgeContextAssemblerReducer()
    state = _make_state(expected_count=3)

    state = handler.accumulate(
        state, _make_fragment(EnumFragmentSource.CODEBASE_INTELLIGENCE)
    )
    state = handler.accumulate(
        state, _make_fragment(EnumFragmentSource.ANTIPATTERN_MATCH)
    )
    state = handler.accumulate(
        state, _make_fragment(EnumFragmentSource.AGENT_LEARNING_RETRIEVAL)
    )

    assert len(state.fragments) == 3
    assert state.completed is True


@pytest.mark.unit
def test_accumulate_duplicate_source_ignored() -> None:
    """Duplicate fragment from the same source is discarded (idempotent)."""
    handler = HandlerKnowledgeContextAssemblerReducer()
    state = _make_state(expected_count=3)

    state = handler.accumulate(
        state, _make_fragment(EnumFragmentSource.CODEBASE_INTELLIGENCE)
    )
    state = handler.accumulate(
        state, _make_fragment(EnumFragmentSource.CODEBASE_INTELLIGENCE)
    )

    assert len(state.fragments) == 1
    assert state.completed is False


@pytest.mark.unit
def test_accumulate_wrong_correlation_id_ignored() -> None:
    handler = HandlerKnowledgeContextAssemblerReducer()
    state = _make_state(expected_count=1)
    wrong = _make_fragment(
        EnumFragmentSource.CODEBASE_INTELLIGENCE, correlation_id="other-id"
    )

    new_state = handler.accumulate(state, wrong)

    assert len(new_state.fragments) == 0
    assert new_state.completed is False


@pytest.mark.unit
def test_accumulate_after_completed_is_noop() -> None:
    handler = HandlerKnowledgeContextAssemblerReducer()
    state = _make_state(expected_count=1)
    state = handler.accumulate(
        state, _make_fragment(EnumFragmentSource.CODEBASE_INTELLIGENCE)
    )
    assert state.completed is True

    state_after = handler.accumulate(
        state, _make_fragment(EnumFragmentSource.ANTIPATTERN_MATCH)
    )

    assert len(state_after.fragments) == 1
    assert state_after.completed is True


# ---------------------------------------------------------------------------
# materialize() — returns None when incomplete
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_materialize_returns_none_when_incomplete() -> None:
    handler = HandlerKnowledgeContextAssemblerReducer()
    state = _make_state(expected_count=3)
    state = handler.accumulate(
        state, _make_fragment(EnumFragmentSource.CODEBASE_INTELLIGENCE)
    )

    result = handler.materialize(state)

    assert result is None


# ---------------------------------------------------------------------------
# materialize() — bundle status
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_materialize_all_ok_fragments_produces_complete_status() -> None:
    handler = HandlerKnowledgeContextAssemblerReducer()
    state = _make_state(expected_count=3)
    for source in (
        EnumFragmentSource.CODEBASE_INTELLIGENCE,
        EnumFragmentSource.ANTIPATTERN_MATCH,
        EnumFragmentSource.AGENT_LEARNING_RETRIEVAL,
    ):
        state = handler.accumulate(state, _make_fragment(source))

    bundle = handler.materialize(state)

    assert bundle is not None
    assert bundle.status == EnumBundleStatus.COMPLETE


@pytest.mark.unit
def test_materialize_some_error_fragments_produces_partial_status() -> None:
    handler = HandlerKnowledgeContextAssemblerReducer()
    state = _make_state(expected_count=3)
    state = handler.accumulate(
        state,
        _make_fragment(EnumFragmentSource.CODEBASE_INTELLIGENCE, content={"ok": True}),
    )
    state = handler.accumulate(
        state, _make_fragment(EnumFragmentSource.ANTIPATTERN_MATCH, error="timeout")
    )
    state = handler.accumulate(
        state, _make_fragment(EnumFragmentSource.AGENT_LEARNING_RETRIEVAL, error="conn")
    )

    bundle = handler.materialize(state)

    assert bundle is not None
    assert bundle.status == EnumBundleStatus.PARTIAL


@pytest.mark.unit
def test_materialize_all_error_fragments_produces_degraded_status() -> None:
    handler = HandlerKnowledgeContextAssemblerReducer()
    state = _make_state(expected_count=2)
    state = handler.accumulate(
        state, _make_fragment(EnumFragmentSource.CODEBASE_INTELLIGENCE, error="fail")
    )
    state = handler.accumulate(
        state, _make_fragment(EnumFragmentSource.ANTIPATTERN_MATCH, error="fail")
    )

    bundle = handler.materialize(state)

    assert bundle is not None
    assert bundle.status == EnumBundleStatus.DEGRADED


# ---------------------------------------------------------------------------
# materialize() — bundle contents
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_materialize_bundle_contains_all_fragment_sources() -> None:
    handler = HandlerKnowledgeContextAssemblerReducer()
    state = _make_state(expected_count=2)
    state = handler.accumulate(
        state,
        _make_fragment(
            EnumFragmentSource.CODEBASE_INTELLIGENCE, content={"summary": "test repo"}
        ),
    )
    state = handler.accumulate(
        state,
        _make_fragment(
            EnumFragmentSource.ANTIPATTERN_MATCH, content={"patterns": ["N+1"]}
        ),
    )

    bundle = handler.materialize(state)

    assert bundle is not None
    sources = {f.fragment_source for f in bundle.fragments}
    assert EnumFragmentSource.CODEBASE_INTELLIGENCE in sources
    assert EnumFragmentSource.ANTIPATTERN_MATCH in sources


@pytest.mark.unit
def test_materialize_correlation_id_preserved() -> None:
    handler = HandlerKnowledgeContextAssemblerReducer()
    state = _make_state(expected_count=1)
    state = handler.accumulate(
        state, _make_fragment(EnumFragmentSource.CODEBASE_INTELLIGENCE)
    )

    bundle = handler.materialize(state)

    assert bundle is not None
    assert bundle.correlation_id == CORR_ID


@pytest.mark.unit
def test_materialize_fragment_count_matches() -> None:
    handler = HandlerKnowledgeContextAssemblerReducer()
    state = _make_state(expected_count=2)
    state = handler.accumulate(
        state, _make_fragment(EnumFragmentSource.CODEBASE_INTELLIGENCE)
    )
    state = handler.accumulate(
        state, _make_fragment(EnumFragmentSource.ANTIPATTERN_MATCH)
    )

    bundle = handler.materialize(state)

    assert bundle is not None
    assert bundle.fragment_count == 2
    assert len(bundle.fragments) == 2


# ---------------------------------------------------------------------------
# partial response: 2 of 4 backends respond (L3)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_partial_2_of_4_l3_produces_valid_bundle() -> None:
    """2 of 4 L3 backends responding produces a valid PARTIAL bundle."""
    handler = HandlerKnowledgeContextAssemblerReducer()
    # L3 has 4 backends: codebase, antipattern, learning, arch_graph
    state = _make_state(expected_count=4)
    state = handler.accumulate(
        state,
        _make_fragment(
            EnumFragmentSource.CODEBASE_INTELLIGENCE, content={"summary": "ok"}
        ),
    )
    state = handler.accumulate(
        state,
        _make_fragment(
            EnumFragmentSource.AGENT_LEARNING_RETRIEVAL, content={"learnings": ["x"]}
        ),
    )
    state = handler.accumulate(
        state, _make_fragment(EnumFragmentSource.ANTIPATTERN_MATCH, error="timeout")
    )
    state = handler.accumulate(
        state, _make_fragment(EnumFragmentSource.ARCHITECTURE_GRAPH, error="bolt down")
    )

    bundle = handler.materialize(state)

    assert bundle is not None
    assert bundle.status == EnumBundleStatus.PARTIAL
    assert bundle.fragment_count == 4
