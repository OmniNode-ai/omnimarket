# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The five lookup questions over a fixture graph, and the same-shape break (OMN-23648, AC1 and AC2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.model_code_graph_lookup import (
    EnumCodeGraphQuestion,
    EnumLookupFault,
    ModelCodeGraphLookupResult,
)
from omnimarket.nodes.node_code_graph_effect.adapters.version_stores import (
    InMemoryCommitClock,
    InMemoryVersionStore,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_lookup import (
    CodeGraphLookup,
)
from tests.nodes.node_code_graph_effect.lookup_support import (
    BASE,
    CLI,
    CORRELATION,
    HANDLER,
    LOOKALIKE,
    MIRROR,
    MODELS,
    REPO,
    TEST,
    ask,
    version,
)

pytestmark = pytest.mark.unit

Question = EnumCodeGraphQuestion


def _lookup() -> CodeGraphLookup:
    store = InMemoryVersionStore(retention_versions=3)
    store.put(version("c1", BASE))
    return CodeGraphLookup(store, InMemoryCommitClock({}))


def _paths(result: ModelCodeGraphLookupResult) -> list[str]:
    return [match.path for match in result.matches]


def assert_same_shape_answer(result: ModelCodeGraphLookupResult) -> None:
    """Thing's shape is shared by Mirror (another name) and not by the Thing of another shape."""
    assert result.status == "found"
    assert [(m.path, m.name) for m in result.matches] == [(MIRROR, "Mirror")]


def test_definition_names_every_file_that_defines_the_symbol() -> None:
    result = _lookup().handle(ask(Question.DEFINITION, symbol="Thing"))
    assert result.status == "found"
    assert [(m.path, m.line, m.detail) for m in result.matches] == [
        (LOOKALIKE, 1, "class"),
        (MODELS, 4, "class"),
    ]
    assert result.answered_from == "c1"
    assert result.correlation_id == CORRELATION


def test_definition_finds_a_function_and_answers_not_found_for_an_unknown_symbol() -> (
    None
):
    found = _lookup().handle(ask(Question.DEFINITION, symbol="handle"))
    assert [(m.path, m.detail) for m in found.matches] == [(HANDLER, "function")]
    missing = _lookup().handle(ask(Question.DEFINITION, symbol="NoSuchThing"))
    assert missing.status == "not-found"
    assert missing.matches == ()


def test_importers_names_the_files_that_import_a_module() -> None:
    result = _lookup().handle(ask(Question.IMPORTERS, path=MODELS))
    assert _paths(result) == [HANDLER]
    assert _lookup().handle(ask(Question.IMPORTERS, path=CLI)).status == "not-found"


def test_reach_follows_importers_through_to_tests() -> None:
    result = _lookup().handle(ask(Question.REACH, paths=(MODELS,)))
    assert [(m.path, m.detail) for m in result.matches] == [
        (HANDLER, "depth 1"),
        (CLI, "depth 2"),
        (TEST, "depth 2"),
    ]


def test_exemplar_ranks_the_nodes_of_an_archetype_by_the_hint() -> None:
    result = _lookup().handle(ask(Question.EXEMPLAR, archetype="effect", hint="beta"))
    assert [m.name for m in result.matches] == ["node_beta_effect", "node_alpha_effect"]
    compute = _lookup().handle(ask(Question.EXEMPLAR, archetype="compute"))
    assert [m.name for m in compute.matches] == ["node_gamma_compute"]
    assert (
        _lookup().handle(ask(Question.EXEMPLAR, archetype="reducer")).status
        == "not-found"
    )


def test_same_shape_finds_a_model_with_a_different_name() -> None:
    assert_same_shape_answer(_lookup().handle(ask(Question.SAME_SHAPE, symbol="Thing")))


def test_a_question_without_its_argument_is_refused_at_the_boundary() -> None:
    with pytest.raises(ValidationError):
        ask(Question.REACH)
    with pytest.raises(ValidationError):
        ask(Question.IMPORTERS)


def test_break_matching_shapes_by_class_name_turns_the_same_shape_test_red() -> None:
    broken = _lookup().handle(
        ask(
            Question.SAME_SHAPE,
            symbol="Thing",
            fault=EnumLookupFault.MATCH_SHAPES_BY_NAME,
        )
    )
    assert broken.matches == ()
    with pytest.raises(AssertionError):
        assert_same_shape_answer(broken)
    # The control: the same helper passes without the break.
    assert_same_shape_answer(_lookup().handle(ask(Question.SAME_SHAPE, symbol="Thing")))


def test_no_stored_version_for_the_repository_is_a_typed_not_found() -> None:
    empty = CodeGraphLookup(InMemoryVersionStore(1), InMemoryCommitClock({}))
    result = empty.handle(ask(Question.DEFINITION, symbol="Thing"))
    assert (result.status, result.answered_from, result.nearest) == (
        "not-found",
        None,
        None,
    )
    assert result.repo == REPO
