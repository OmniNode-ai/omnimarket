# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for replay evaluation compute (OMN-21111)."""

from __future__ import annotations

import importlib
import tomllib
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from omnimarket.nodes.node_context_replay_eval_compute.handlers import (
    HandlerAnswerComparison,
    HandlerReviewRowSizing,
    HandlerRulingPresence,
)
from omnimarket.nodes.node_context_replay_eval_compute.models import (
    EnumPresence,
    EnumSizingVerdict,
    ModelAnswerComparisonRequest,
    ModelReviewRowSizingRequest,
    ModelRulingPresenceRequest,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
NODE_NAME = "node_context_replay_eval_compute"
PACKAGE = f"omnimarket.nodes.{NODE_NAME}"
NODE = ROOT / "src/omnimarket/nodes" / NODE_NAME
COMMAND_TOPIC = "onex.cmd.omniintelligence.context-replay-eval-requested.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.context-replay-eval-completed.v1"


def test_golden_chain_presence_answers_capacity_and_review_rows() -> None:
    """Synthetic packages and caller-graded answers reproduce all TA.9 aggregates."""
    presence = HandlerRulingPresence().handle(
        ModelRulingPresenceRequest.model_validate(
            {
                "cases": [
                    {
                        "case_id": "case-a",
                        "candidate_id": "candidate-a",
                        "package_text": "Retain the synthetic queue decision.",
                        "expected": [
                            {
                                "statement_id": "ruling-a",
                                "quote": "synthetic queue decision",
                            }
                        ],
                    },
                    {
                        "case_id": "case-b",
                        "candidate_id": "candidate-b",
                        "package_text": "Retain the synthetic retry budget.",
                        "expected": [
                            {
                                "statement_id": "ruling-b",
                                "required_spans": ["synthetic", "retry budget"],
                            }
                        ],
                    },
                ]
            }
        )
    )
    assert presence.cases_total == presence.cases_passed == 2
    assert presence.all_present
    assert presence.falsifiers == ()
    assert all(case.passed for case in presence.cases)
    assert all(
        statement.presence is EnumPresence.PRESENT
        for case in presence.cases
        for statement in case.statements
    )

    answers = HandlerAnswerComparison().handle(
        ModelAnswerComparisonRequest.model_validate(
            {
                "runs": [
                    {
                        "run_id": run_id,
                        "cases": [
                            {
                                "case_id": case.case_id,
                                "candidate_id": case.candidate_id,
                                "package_answer": "MATCH"
                                if case.passed
                                else "MISMATCH",
                                "native_answer": "MATCH"
                                if case.case_id == "case-a"
                                else "UNAVAILABLE",
                            }
                            for case in presence.cases
                        ],
                    }
                    for run_id in ("run-1", "run-2")
                ]
            }
        )
    )
    # Numeric superiority cannot establish Q2 when a native answer is absent.
    assert not answers.next_action_met
    assert answers.determinism_checked
    assert answers.candidate_ids_identical
    assert answers.candidate_id_mismatches == ()
    assert answers.falsifiers == (
        "run run-1: answers unavailable: package 0, native 1",
        "run run-2: answers unavailable: package 0, native 1",
    )
    assert [run.run_id for run in answers.runs] == ["run-1", "run-2"]
    for run in answers.runs:
        assert run.cases == run.package_matches == 2
        assert run.native_matches == run.native_unavailable == 1
        assert run.package_unavailable == 0
        assert not run.package_at_least_native

    carried = [
        {
            "prompt_id": "p-new",
            "captured_at": "2025-01-01T00:02:00Z",
            "token_estimate": 20,
        },
        {
            "prompt_id": "p-old",
            "captured_at": "2025-01-01T00:01:00Z",
            "token_estimate": 20,
        },
    ]
    common = {
        "session_id": "session-a",
        "other_required_tokens": {"RULINGS": 60},
        "operator_words_overhead_tokens": 10,
        "carried_prompts": carried,
    }
    request = ModelReviewRowSizingRequest.model_validate(
        {
            "budget_tokens": 100,
            "boundaries": [
                common | {"boundary_id": "b1", "boundary_at": "2025-01-01T00:10:00Z"},
                common
                | {
                    "boundary_id": "b2",
                    "boundary_at": "2025-01-01T00:20:00Z",
                    "other_required_tokens": {"RULINGS": 80},
                },
                {
                    "session_id": "session-b",
                    "boundary_id": "b3",
                    "boundary_at": "2025-01-01T00:30:00Z",
                    "other_required_tokens": {"RULINGS": 20},
                },
                common
                | {
                    "boundary_id": "positive-control",
                    "boundary_at": "2025-01-01T00:40:00Z",
                    "control": True,
                    "budget_override_tokens": 50,
                },
            ],
        }
    )
    sizing = HandlerReviewRowSizing().handle(request)
    assert sizing.boundaries_total == 3
    assert sizing.exceeding == 2
    assert sizing.fallback_share == 0.6667
    assert sizing.does_not_fit == 0
    assert sizing.does_not_fit_share == 0.0
    assert sizing.control_boundaries == sizing.control_exceeding == 1
    assert sizing.zero_fallback_has_control
    assert sizing.falsifiers == ()
    assert all(boundary.replay_reproduces for boundary in sizing.boundaries)
    assert [boundary.verdict for boundary in sizing.boundaries] == [
        EnumSizingVerdict.FITS_AFTER_REVIEW,
        EnumSizingVerdict.FITS_AFTER_REVIEW,
        EnumSizingVerdict.FITS_AS_IS,
        EnumSizingVerdict.DOES_NOT_FIT,
    ]
    assert [boundary.operator_words_tokens for boundary in sizing.boundaries] == [
        50,
        50,
        0,
        50,
    ]
    assert [boundary.required_tokens for boundary in sizing.boundaries] == [
        110,
        130,
        20,
        110,
    ]
    assert [boundary.budget_tokens for boundary in sizing.boundaries] == [
        100,
        100,
        100,
        50,
    ]
    assert [boundary.exceeds_budget for boundary in sizing.boundaries] == [
        True,
        True,
        False,
        True,
    ]
    assert [boundary.named_prompt_ids for boundary in sizing.boundaries] == [
        ("p-old",),
        ("p-old", "p-new"),
        (),
        (),
    ]
    assert [boundary.prompts_to_name for boundary in sizing.boundaries] == [
        1,
        2,
        0,
        None,
    ]
    assert [boundary.tokens_to_name for boundary in sizing.boundaries] == [
        20,
        40,
        0,
        None,
    ]
    assert [
        boundary.required_tokens_after_review for boundary in sizing.boundaries
    ] == [90, 90, 20, None]
    assert [session.model_dump() for session in sizing.sessions] == [
        {
            "session_id": "session-a",
            "boundaries": 2,
            "exceeding": 2,
            "does_not_fit": 0,
            "review_rows": 2,
        },
        {
            "session_id": "session-b",
            "boundaries": 1,
            "exceeding": 0,
            "does_not_fit": 0,
            "review_rows": 0,
        },
    ]
    assert HandlerReviewRowSizing().handle(request) == sizing


def test_error_chain_malformed_request_is_refused() -> None:
    """An empty expected-ruling set is refused before scoring."""
    with pytest.raises(ValidationError, match="expected"):
        ModelRulingPresenceRequest.model_validate(
            {
                "cases": [
                    {"case_id": "case-a", "package_text": "synthetic", "expected": []}
                ]
            }
        )


def test_contract_loads_and_declares_its_topics() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["name"] == "context_replay_eval_compute"
    assert contract["node_type"] == "compute"
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [COMPLETED_TOPIC]
    assert contract["terminal_event"] == COMPLETED_TOPIC
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["externally_consumed_topics"] == [COMPLETED_TOPIC]
    assert contract["metadata"] == {"ticket": "OMN-21111", "transport_type": "kafka"}
    assert contract["descriptor"] == {
        "node_archetype": "compute",
        "purity": "pure",
        "idempotent": True,
        "side_effects": [],
        "runtime_profiles": ["main"],
    }
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert [route["operation"] for route in routing["handlers"]] == [
        "score_ruling_presence",
        "compare_answers",
        "size_review_rows",
    ]


@pytest.mark.parametrize(
    ("operation", "handler_name", "input_name", "output_name"),
    [
        (
            "score_ruling_presence",
            "HandlerRulingPresence",
            "ModelRulingPresenceRequest",
            "ModelRulingPresenceResult",
        ),
        (
            "compare_answers",
            "HandlerAnswerComparison",
            "ModelAnswerComparisonRequest",
            "ModelAnswerComparisonResult",
        ),
        (
            "size_review_rows",
            "HandlerReviewRowSizing",
            "ModelReviewRowSizingRequest",
            "ModelReviewRowSizingResult",
        ),
    ],
)
def test_contract_handlers_and_models_resolve(
    operation: str, handler_name: str, input_name: str, output_name: str
) -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    route = next(
        route
        for route in contract["handler_routing"]["handlers"]
        if route["operation"] == operation
    )
    declaration = route["handler"]
    assert declaration["name"] == handler_name
    assert declaration["module"].startswith(f"{PACKAGE}.handlers.")
    handler_class = getattr(
        importlib.import_module(declaration["module"]), handler_name
    )
    assert handler_class.__module__ == declaration["module"]
    assert callable(handler_class().handle)
    for key, expected in (("input_model", input_name), ("output_model", output_name)):
        model_path = route[key]
        assert model_path == f"{PACKAGE}.models.model_replay_eval.{expected}"
        module_name, class_name = model_path.rsplit(".", 1)
        assert issubclass(
            getattr(importlib.import_module(module_name), class_name), BaseModel
        )


def test_entry_point_registered_and_loads() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    metadata = yaml.safe_load((NODE / "metadata.yaml").read_text(encoding="utf-8"))
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert project["project"]["entry-points"]["onex.nodes"][NODE_NAME] == PACKAGE
    assert metadata["entry_points"]["onex.nodes"][NODE_NAME] == PACKAGE
    assert "entry_points" not in contract
    registered = {entry.name: entry for entry in entry_points(group="onex.nodes")}
    assert registered[NODE_NAME].value == PACKAGE
    assert registered[NODE_NAME].load() is importlib.import_module(PACKAGE)
    assert metadata["pack"] == "core"
    assert metadata["node_role"] == "compute"
    assert metadata["display_name"] == "Context Replay Eval Compute"
    assert metadata["tags"] == [
        "compute",
        "context",
        "compaction",
        "replay",
        "evaluation",
    ]
    assert metadata["capabilities"]["side_effect_class"] == "none"
    assert all(
        metadata["capabilities"][key] is False
        for key in (
            "requires_network",
            "requires_repo",
            "requires_secrets",
            "requires_docker",
        )
    )
