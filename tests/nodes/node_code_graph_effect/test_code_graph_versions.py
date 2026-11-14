# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Versions of the graph: as-of lookups, the nearest stored version, retention (OMN-23648, AC2 and AC3)."""

from __future__ import annotations

from datetime import datetime

import pytest

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
    ADDS,
    BASE,
    REPO,
    ask,
    at,
    version,
)

pytestmark = pytest.mark.unit

ADDED = "src/example_core/added.py"
SYMBOL = "added_symbol"


def _store(retention: int = 3) -> InMemoryVersionStore:
    """c1 and c2 without the added symbol, c3 with it."""
    store = InMemoryVersionStore(retention_versions=retention)
    store.put(version("c1", BASE, at(1)))
    store.put(version("c2", {**BASE, "src/example_core/extra.py": "X = 1\n"}, at(5)))
    store.put(version("c3", {**BASE, **ADDS}, at(9)))
    return store


def _lookup(store: InMemoryVersionStore, **dated: datetime) -> CodeGraphLookup:
    return CodeGraphLookup(
        store,
        InMemoryCommitClock({(REPO, commit): when for commit, when in dated.items()}),
    )


def assert_version_answers(lookup: CodeGraphLookup, *, fault: EnumLookupFault) -> None:
    """The head holds the added symbol; as of c2 the symbol is not found."""
    head = lookup.handle(ask(EnumCodeGraphQuestion.DEFINITION, symbol=SYMBOL))
    assert head.status == "found"
    assert [m.path for m in head.matches] == [ADDED]
    earlier = lookup.handle(
        ask(EnumCodeGraphQuestion.DEFINITION, as_of="c2", fault=fault, symbol=SYMBOL)
    )
    assert earlier.status == "not-found"
    assert earlier.answered_from == "c2"
    assert earlier.matches == ()


def test_a_lookup_as_of_a_stored_commit_answers_from_that_version() -> None:
    assert_version_answers(_lookup(_store()), fault=EnumLookupFault.NONE)


def test_a_lookup_without_a_commit_answers_from_the_head() -> None:
    result = _lookup(_store()).handle(
        ask(EnumCodeGraphQuestion.DEFINITION, symbol=SYMBOL)
    )
    assert result.answered_from == "c3"


def test_break_answering_from_the_head_when_asked_as_of_a_commit_turns_the_version_test_red() -> (
    None
):
    lookup = _lookup(_store())
    broken = lookup.handle(
        ask(
            EnumCodeGraphQuestion.DEFINITION,
            as_of="c2",
            fault=EnumLookupFault.ANSWER_FROM_HEAD,
            symbol=SYMBOL,
        )
    )
    assert broken.answered_from == "c3"
    with pytest.raises(AssertionError):
        assert_version_answers(lookup, fault=EnumLookupFault.ANSWER_FROM_HEAD)
    # The control: the same helper passes without the break.
    assert_version_answers(lookup, fault=EnumLookupFault.NONE)


def _unstored(result: ModelCodeGraphLookupResult) -> None:
    assert result.status == "not-found"
    assert result.answered_from is None
    assert result.matches == ()


def test_a_commit_with_no_stored_version_names_the_nearest_stored_version() -> None:
    lookup = _lookup(_store(), unstored=at(6))
    result = lookup.handle(
        ask(EnumCodeGraphQuestion.DEFINITION, as_of="unstored", symbol=SYMBOL)
    )
    _unstored(result)
    assert result.nearest is not None
    assert (result.nearest.commit, result.nearest.ordering) == ("c2", "by-commit-time")
    assert result.nearest.committed_at == at(5)


def test_the_nearest_version_of_an_equal_gap_is_the_earlier_one() -> None:
    result = _lookup(_store(), unstored=at(7)).handle(
        ask(EnumCodeGraphQuestion.DEFINITION, as_of="unstored", symbol=SYMBOL)
    )
    assert result.nearest is not None
    assert result.nearest.commit == "c2"


def test_an_undatable_commit_names_the_newest_version_and_says_it_is_unresolved() -> (
    None
):
    result = _lookup(_store()).handle(
        ask(EnumCodeGraphQuestion.DEFINITION, as_of="never-seen", symbol=SYMBOL)
    )
    _unstored(result)
    assert result.nearest is not None
    assert (result.nearest.commit, result.nearest.ordering) == ("c3", "unresolved")


def test_versions_past_the_retention_are_pruned_oldest_first() -> None:
    store = InMemoryVersionStore(retention_versions=2)
    results = [
        store.put(version(commit, BASE, at(day)))
        for commit, day in (("c1", 1), ("c2", 5), ("c3", 9))
    ]
    assert results == [(True, ()), (True, ()), (True, ("c1",))]
    assert [stored.commit for stored in store.versions(REPO)] == ["c2", "c3"]
    assert store.get(REPO, "c1") is None
    pruned = _lookup(store, c1=at(1)).handle(
        ask(EnumCodeGraphQuestion.DEFINITION, as_of="c1", symbol=SYMBOL)
    )
    _unstored(pruned)
    assert pruned.nearest is not None
    assert pruned.nearest.commit == "c2"


def test_storing_a_commit_twice_keeps_one_version() -> None:
    store = InMemoryVersionStore(retention_versions=3)
    assert store.put(version("c1", BASE, at(1))) == (True, ())
    assert store.put(version("c1", BASE, at(1))) == (False, ())
    assert len(store.versions(REPO)) == 1


def test_a_retention_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        InMemoryVersionStore(retention_versions=0)
