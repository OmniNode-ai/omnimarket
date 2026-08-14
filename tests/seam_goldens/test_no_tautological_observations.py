# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Meta-guard: a seam golden may never observe its own declaration (OMN-16004).

``HandlerSeamMatch`` classifies an edge ``REGENERABLE`` when legs 2 and 3 —
observed producer vs declared, observed consumer vs declared — are both
explicitly green. That verdict is only worth something if the observed inputs
are independent of the declared ones.

The first cut of these goldens broke that. All nine registry-match call sites
were written as::

    verdict = run_registry_match(
        edge_id=...,
        declared_producer=declared_producer,
        declared_consumer=declared_consumer,
        observed_producer=declared_producer,   # <-- the same object
        observed_consumer=declared_consumer,   # <-- the same object
    )

which reduces both legs to ``x == x``. ``REGENERABLE`` was therefore guaranteed
the moment leg 1 passed, and was insensitive to every byte the golden drove.
Nine goldens asserted a property none of them tested.

Two layers now prevent that from coming back, and this module is the outer one:

* ``harness.run_registry_match`` refuses an ``observed_*`` argument that is the
  same OBJECT as a ``declared_*`` argument — a runtime guard, checked on every
  call, which also covers the case of a helper aliasing them out of sight.
* This module reads every golden's SOURCE and rejects the syntactic pattern
  directly, so the defect is caught even in a call that never executes (a
  skipped test, an unreached branch, a new golden added but not yet wired).

Value equality is deliberately NOT banned by either layer. A genuinely observed
projection that happens to equal the declared one is exactly what a healthy
seam looks like; that equality is the finding. What is banned is the observation
never having been made.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.seam_goldens.manifest import (
    EnumSeamObservationClass,
    ModelSliceEdge,
    load_slice_manifest,
)

pytestmark = pytest.mark.unit

_GOLDEN_DIR = Path(__file__).parent

_DECLARED_KEYWORDS = ("declared_producer", "declared_consumer")
_OBSERVED_KEYWORDS = ("observed_producer", "observed_consumer")

#: This module quotes the banned pattern in its own docstring, so it must not
#: scan itself — the finding would be its own explanation.
_SELF = Path(__file__).name


def _golden_sources() -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in _GOLDEN_DIR.glob("*.py")
            if path.name not in {"__init__.py", _SELF}
        )
    )


def _registry_match_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_registry_match"
    ]


def _keyword_source_names(call: ast.Call) -> dict[str, str | None]:
    """Map each seam-match keyword to the bare identifier it was passed, if any.

    ``None`` where the argument is a constructed expression rather than a bare
    name — which is the shape every honest observation takes, because it is
    built by an ``observed_projection_from_*`` call.
    """

    resolved: dict[str, str | None] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            continue
        if keyword.arg not in (*_DECLARED_KEYWORDS, *_OBSERVED_KEYWORDS):
            continue
        value = keyword.value
        resolved[keyword.arg] = value.id if isinstance(value, ast.Name) else None
    return resolved


@pytest.mark.parametrize(
    "source_path", _golden_sources(), ids=lambda path: str(path.name)
)
def test_no_golden_passes_a_declared_projection_as_an_observation(
    source_path: Path,
) -> None:
    """The regression guard for the exact defect that shipped.

    Fails on any ``run_registry_match`` call where an ``observed_*`` argument
    is the same identifier as a ``declared_*`` argument in that same call.
    """

    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    violations: list[str] = []
    for call in _registry_match_calls(tree):
        names = _keyword_source_names(call)
        declared_names = {
            keyword: names[keyword]
            for keyword in _DECLARED_KEYWORDS
            if names.get(keyword) is not None
        }
        for observed_keyword in _OBSERVED_KEYWORDS:
            observed_name = names.get(observed_keyword)
            if observed_name is None:
                continue
            for declared_keyword, declared_name in declared_names.items():
                if observed_name == declared_name:
                    violations.append(
                        f"{source_path.name}:{call.lineno}: "
                        f"{observed_keyword}={observed_name} is the same name as "
                        f"{declared_keyword}"
                    )

    assert not violations, (
        "a seam golden is observing its own declaration, which makes the "
        "observed-vs-declared leg a self-comparison and REGENERABLE "
        "unconditional:\n  " + "\n  ".join(violations)
    )


@pytest.mark.parametrize(
    "source_path", _golden_sources(), ids=lambda path: str(path.name)
)
def test_every_observation_is_built_by_an_observation_constructor(
    source_path: Path,
) -> None:
    """Close the near-miss the identity check alone would let through.

    Aliasing the declaration into a second variable
    (``observed = declared_producer``) defeats a pure name comparison at the
    call site while still being the same object. Requiring every non-``None``
    observation to be a direct ``observed_projection_from_*`` call removes the
    whole class: an observation must be CONSTRUCTED from a driven artifact, and
    the constructors are the only things that do that.
    """

    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    violations: list[str] = []
    for call in _registry_match_calls(tree):
        for keyword in call.keywords:
            if keyword.arg not in _OBSERVED_KEYWORDS:
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and value.value is None:
                continue
            is_constructor_call = (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id.startswith("observed_projection_from_")
            )
            if not is_constructor_call:
                violations.append(
                    f"{source_path.name}:{call.lineno}: {keyword.arg} is not a "
                    f"direct observed_projection_from_* call"
                )

    assert not violations, (
        "every observed projection must be built from a driven artifact by an "
        "observed_projection_from_* constructor (or be None for an "
        "unobservable side):\n  " + "\n  ".join(violations)
    )


def _edges_by_class(
    observation_class: EnumSeamObservationClass,
) -> tuple[ModelSliceEdge, ...]:
    return load_slice_manifest().by_observation_class(observation_class)


class TestManifestAndGoldensAgreeOnWhatIsObservable:
    """The manifest's entitlement must match what the goldens actually assert."""

    def test_regenerable_edges_require_both_sides_reachable(self) -> None:
        for edge in _edges_by_class(EnumSeamObservationClass.REGENERABLE):
            assert edge.producer_symbol_reachable, edge.edge_id
            assert edge.consumer_symbol_reachable, edge.edge_id

    def test_shape_only_edges_have_exactly_one_unreachable_side(self) -> None:
        """A SHAPE_ONLY row must name a real closure boundary, not a shrug.

        Both sides reachable means the golden simply did not do the work, and
        both sides unreachable means there is no seam to compare at all — that
        is ``NOT_CLAIMED``. Exactly one is the honest SHAPE_ONLY case.
        """

        for edge in _edges_by_class(EnumSeamObservationClass.SHAPE_ONLY):
            reachable = (edge.producer_symbol_reachable, edge.consumer_symbol_reachable)
            assert reachable.count(True) == 1, (
                f"{edge.edge_id}: SHAPE_ONLY records "
                f"producer_symbol_reachable={reachable[0]}, "
                f"consumer_symbol_reachable={reachable[1]}"
            )

    def test_every_non_regenerable_edge_states_why(self) -> None:
        for observation_class in (
            EnumSeamObservationClass.SHAPE_ONLY,
            EnumSeamObservationClass.NOT_CLAIMED,
        ):
            for edge in _edges_by_class(observation_class):
                assert edge.observation_note, edge.edge_id

    def test_the_slice_still_contains_a_regenerable_edge(self) -> None:
        """Guard the opposite failure: downgrading everything to stay green.

        The honest fix for a tautological REGENERABLE is a real observation
        wherever one is possible — not a blanket SHAPE_ONLY. If every edge ends
        up downgraded, the goldens have stopped proving anything and this fails.
        """

        assert _edges_by_class(EnumSeamObservationClass.REGENERABLE)

    def test_only_registry_match_edges_claim_an_observation_class(self) -> None:
        """NOT_CLAIMED edges must not quietly acquire a match assertion.

        Checked against the golden sources: a module whose only edge is
        NOT_CLAIMED has no business calling ``run_registry_match`` with
        observations, because there is nothing coherent to observe against.
        """

        not_claimed_modules = {
            edge.golden_module
            for edge in _edges_by_class(EnumSeamObservationClass.NOT_CLAIMED)
        }
        claimed_modules = {
            edge.golden_module
            for edge in load_slice_manifest().included()
            if edge.observation_class is not EnumSeamObservationClass.NOT_CLAIMED
        }

        for module in not_claimed_modules - claimed_modules:
            assert module is not None
            source = (_GOLDEN_DIR.parents[1] / module).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=module)
            observing_calls = [
                call
                for call in _registry_match_calls(tree)
                if any(
                    keyword.arg in _OBSERVED_KEYWORDS
                    and not (
                        isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is None
                    )
                    for keyword in call.keywords
                )
            ]
            assert not observing_calls, (
                f"{module}: covers only NOT_CLAIMED edges but supplies observed "
                f"projections at line(s) "
                f"{[call.lineno for call in observing_calls]}"
            )
