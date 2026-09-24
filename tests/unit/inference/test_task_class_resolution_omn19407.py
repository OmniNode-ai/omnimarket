# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Task-class resolution lives beside the contract it reads (OMN-19407).

Before this change the evaluator lived in the ``onex`` CLI in omnibase_infra,
fed by mirror models and a raw-YAML loader of this contract, and every routing
test there ran against a hand-copied "production mirror" pinned to this
contract by two halves of one digest. The CLI now reads this authority through
the installed registry (the ``onex.contracts`` entry-point group) and calls
:meth:`ModelTaskClassAuthority.resolve_task_type`. So the routing table below
runs against the LIVE contract, and there is nothing to keep in step.

Every expectation in this file is either a property of the contract read from
the contract itself, or a routing outcome that was measured on a real run and
is cited where it is asserted.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml

from omnimarket.inference.task_class_authority import (
    EnumTaskTypeResolution,
    ModelTaskClassAuthority,
    TaskClassSelectionError,
    load_task_class_authority,
)

pytestmark = pytest.mark.unit


@pytest.fixture(name="live", scope="module")
def _live() -> ModelTaskClassAuthority:
    return load_task_class_authority()


def _authority(tmp_path: Path, document: dict[str, object]) -> ModelTaskClassAuthority:
    path = tmp_path / "task_class_contracts.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return load_task_class_authority(path)


def _public(
    priority: int, phrases: list[str], **selection: object
) -> dict[str, object]:
    return {
        "gateway_exposure": "public",
        "selection": {"priority": priority, "phrases": phrases, **selection},
    }


# ---------------------------------------------------------------------------
# The registry entry the CLI reads.
# ---------------------------------------------------------------------------


class TestTheRegistryEntry:
    def test_the_authority_is_advertised_in_the_contract_registry(self) -> None:
        """The CLI finds this contract by entry point, never by package path."""
        matches = [
            entry
            for entry in entry_points(group="onex.contracts")
            if entry.name == "task_class_authority"
        ]
        assert len(matches) == 1
        loaded = matches[0].load()()
        assert isinstance(loaded, ModelTaskClassAuthority)
        assert loaded.universe == load_task_class_authority().universe

    def test_the_live_contract_declares_its_fallback(
        self, live: ModelTaskClassAuthority
    ) -> None:
        """The fallback is a grading decision the contract owns, so it is declared."""
        assert live.selection_fallback is not None
        assert live.selection_fallback.task_class in live.public_task_classes

    def test_every_routable_class_declares_an_execution_budget(
        self, live: ModelTaskClassAuthority
    ) -> None:
        """The CLI derives its wall-clock deadline from this; no default exists."""
        for name in live.universe:
            assert live.execution_budget(name).task_class_timeout_ceiling_seconds > 0


# ---------------------------------------------------------------------------
# Explicit classes (folded from OMN-13966).
# ---------------------------------------------------------------------------


class TestExplicitClasses:
    def test_every_public_class_is_admitted_by_name(
        self, live: ModelTaskClassAuthority
    ) -> None:
        for name in live.public_task_classes:
            resolution = live.resolve_task_type("anything", explicit=name)
            assert resolution.task_type == name
            assert resolution.resolution is EnumTaskTypeResolution.EXPLICIT

    def test_every_routable_internal_class_is_admitted_by_name(
        self, live: ModelTaskClassAuthority
    ) -> None:
        """gateway_exposure governs the public Gateway, not a caller naming a class."""
        routable_internal = live.internal_task_classes - set(
            live.unroutable_task_classes
        )
        assert routable_internal, "positive control: the contract has internal classes"
        for name in routable_internal:
            resolution = live.resolve_task_type("anything", explicit=name)
            assert resolution.task_type == name
            assert "internal class" in resolution.reason

    def test_an_unroutable_class_is_refused_in_the_contract_words(
        self, live: ModelTaskClassAuthority
    ) -> None:
        assert live.unroutable_task_classes, "positive control: one class is declared"
        for name, declared in live.unroutable_task_classes.items():
            with pytest.raises(TaskClassSelectionError) as refused:
                live.resolve_task_type("anything", explicit=name)
            message = str(refused.value)
            assert declared.status.value in message
            assert declared.missing_capability in message
            assert declared.tracking in message
            assert " ".join(declared.reason.split()) in message
            assert "unknown" not in message

    def test_an_undeclared_class_is_refused_naming_every_declared_one(
        self, live: ModelTaskClassAuthority
    ) -> None:
        with pytest.raises(
            TaskClassSelectionError, match="unknown task type"
        ) as refused:
            live.resolve_task_type("anything", explicit="not_a_class")
        for name in live.universe:
            assert name in str(refused.value)


# ---------------------------------------------------------------------------
# The mechanism, against hand-written contracts.
# ---------------------------------------------------------------------------


class TestTheMechanism:
    def test_only_public_classes_are_selected_from_a_prompt(
        self, tmp_path: Path
    ) -> None:
        authority = _authority(
            tmp_path,
            {
                "task_classes": {
                    "open": _public(10, ["widget"]),
                    "hidden": {
                        "gateway_exposure": "internal",
                        "selection": {"priority": 99, "phrases": ["widget"]},
                    },
                },
            },
        )
        assert (
            authority.resolve_task_type("a widget", explicit=None).task_type == "open"
        )

    def test_word_boundaries(self, tmp_path: Path) -> None:
        """OMN-18305: "latest" contains "test" and must not match it."""
        authority = _authority(
            tmp_path,
            {
                "task_classes": {"test": _public(10, ["test"]), "doc": _public(1, [])},
                "selection_fallback": {"task_class": "doc", "rationale": "prose"},
            },
        )
        latest = authority.resolve_task_type("the latest window", explicit=None)
        assert latest.resolution is EnumTaskTypeResolution.FALLBACK
        assert authority.resolve_task_type("a test", explicit=None).task_type == "test"

    def test_shape_gates_the_phrase(self, tmp_path: Path) -> None:
        authority = _authority(
            tmp_path,
            {
                "task_classes": {
                    "short": _public(50, ["test"], max_words=5),
                    "long": _public(10, ["summary"], min_words=6),
                },
            },
        )
        long_prompt = "test " * 10 + "summary"
        assert (
            authority.resolve_task_type(long_prompt, explicit=None).task_type == "long"
        )

    def test_ties_resolve_by_priority_then_name(self, tmp_path: Path) -> None:
        authority = _authority(
            tmp_path,
            {
                "task_classes": {
                    "beta": _public(10, ["x"]),
                    "alpha": _public(10, ["x"]),
                    "gamma": _public(5, ["x"]),
                },
            },
        )
        resolution = authority.resolve_task_type("x", explicit=None)
        assert resolution.task_type == "alpha"
        assert resolution.resolution is EnumTaskTypeResolution.CONTRACT
        assert "'x'" in resolution.reason

    def test_an_unclaimed_prompt_without_a_declared_fallback_is_refused(
        self, tmp_path: Path
    ) -> None:
        authority = _authority(tmp_path, {"task_classes": {"a": _public(1, ["x"])}})
        with pytest.raises(TaskClassSelectionError, match="selection_fallback"):
            authority.resolve_task_type("nothing matches", explicit=None)

    def test_a_ceiling_that_reaches_the_port_wait_is_refused_at_load(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="less_than_equal"):
            _authority(
                tmp_path,
                {
                    "task_classes": {"a": _public(1, ["x"])},
                    "execution_budgets": {
                        "a": {
                            "task_class_timeout_ceiling_seconds": 300,
                            "terminal_delivery_margin_seconds": 60,
                        }
                    },
                },
            )

    def test_a_non_public_fallback_is_refused_at_load(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not a public task class"):
            _authority(
                tmp_path,
                {
                    "task_classes": {
                        "a": _public(1, ["x"]),
                        "b": {
                            "gateway_exposure": "internal",
                            "selection": {"priority": 0, "phrases": []},
                        },
                    },
                    "selection_fallback": {"task_class": "b", "rationale": "r"},
                },
            )


class TestTheQualifierRule:
    """OMN-18831's mechanism: a gated phrase needs a qualifier within the window."""

    @staticmethod
    def _gated(tmp_path: Path, within_words: int = 3) -> ModelTaskClassAuthority:
        return _authority(
            tmp_path,
            {
                "task_classes": {
                    "gated": _public(
                        50,
                        ["implement"],
                        qualified_phrases={
                            "within_words": within_words,
                            "phrases": ["write a"],
                            "qualifiers": ["parser", "unit test"],
                        },
                    ),
                    "plain": _public(10, ["widget"]),
                },
                "selection_fallback": {"task_class": "plain", "rationale": "r"},
            },
        )

    @pytest.mark.parametrize(
        ("prompt", "expected"),
        [
            ("write a parser please", "gated"),
            ("update the parser, then write a replacement", "gated"),
            ("write a note now, and later write a parser for it", "gated"),
            ("write a unit test for it", "gated"),
            ("implement the thing", "gated"),
            ("write a note to the team", "plain"),
            ("write a note that we will send once the parser lands", "plain"),
        ],
    )
    def test_the_window(self, tmp_path: Path, prompt: str, expected: str) -> None:
        resolution = self._gated(tmp_path).resolve_task_type(prompt, explicit=None)
        assert resolution.task_type == expected, resolution.reason

    def test_a_phrase_is_never_its_own_qualifier(self, tmp_path: Path) -> None:
        authority = _authority(
            tmp_path,
            {
                "task_classes": {
                    "gated": _public(
                        50,
                        [],
                        qualified_phrases={
                            "within_words": 4,
                            "phrases": ["write a parser"],
                            "qualifiers": ["parser"],
                        },
                    ),
                    "plain": _public(1, []),
                },
                "selection_fallback": {"task_class": "plain", "rationale": "r"},
            },
        )
        resolution = authority.resolve_task_type("write a parser", explicit=None)
        assert resolution.resolution is EnumTaskTypeResolution.FALLBACK


# ---------------------------------------------------------------------------
# The routing table, against the LIVE contract. Rows carried over from the
# retired omnibase_infra mirror suite; each cites the run that measured it.
# ---------------------------------------------------------------------------

_VERBATIM_OPENING = (
    "Write a GitHub PR body in markdown from these facts. "
    "No preamble, no commentary, output only the body."
)

_ROUTING_TABLE: tuple[tuple[str, str, str], ...] = (
    (_VERBATIM_OPENING, "document", "OMN-18831 run 21b33edf, verbatim"),
    (
        "Write an update for the operator covering last night's outage.",
        "document",
        "'write an' + ordinary noun",
    ),
    (
        "Create a short agenda for tomorrow's planning meeting with the team.",
        "document",
        "'create a' + ordinary noun",
    ),
    (
        "Generate a list of questions to ask the customer on the call.",
        "document",
        "'generate' + ordinary noun",
    ),
    (
        "The report's assertions are unsupported by the evidence it cites.",
        "document",
        "'assertions', English sense",
    ),
    (
        "List the test cases most likely to be forgotten for this rule.",
        "document",
        "OMN-19017 shortest form",
    ),
    ("the latest window", "document", "OMN-18305: 'latest' is not 'test'"),
    (
        "Add test cases to the pytest module for the trailing comma bug.",
        "test",
        "OMN-19017 qualified by pytest",
    ),
    (
        "Write a parser for the lane manifest file.",
        "code_generation",
        "'write a' qualified by parser",
    ),
    (
        "Build a CLI subcommand that drains the queue.",
        "code_generation",
        "'build a' qualified by cli",
    ),
    (
        "Implement the retry policy on the dispatch port.",
        "code_generation",
        "'implement' unqualified",
    ),
    ("Add assertions to the auth tests.", "test", "'assertions' qualified by tests"),
    (
        "Write a test for the task-class resolver.",
        "test",
        "test outranks code_generation",
    ),
)


@pytest.mark.parametrize(
    ("prompt", "expected", "why"),
    _ROUTING_TABLE,
    ids=[row[2] for row in _ROUTING_TABLE],
)
def test_the_live_contract_routes_each_measured_prompt(
    live: ModelTaskClassAuthority, prompt: str, expected: str, why: str
) -> None:
    resolution = live.resolve_task_type(prompt, explicit=None)
    assert resolution.task_type == expected, f"{why}: {resolution.reason}"
