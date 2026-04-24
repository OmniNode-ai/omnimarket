# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Table-driven tests for is_pipeline_touching_pr (OMN-9577).

Covers each decision input (path, label, contract flag), each in isolation and
in combination, plus the negative cases and priority ordering. The classifier
is a pure function; no fixtures or mocks are needed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pytest

from omnimarket.classifiers.pipeline_touch import is_pipeline_touching_pr
from omnimarket.enums.enum_pipeline_touch_reason import EnumPipelineTouchReason


@dataclass(frozen=True)
class _Case:
    name: str
    changed_files: Sequence[str]
    ticket_labels: Sequence[str]
    contract_touches_pipeline: bool
    expected_is_touching: bool
    expected_reason: EnumPipelineTouchReason
    expected_matched_paths: Sequence[str] = ()
    expected_matched_labels: Sequence[str] = ()


_CASES: tuple[_Case, ...] = (
    _Case(
        name="empty_inputs_is_not_touching",
        changed_files=(),
        ticket_labels=(),
        contract_touches_pipeline=False,
        expected_is_touching=False,
        expected_reason=EnumPipelineTouchReason.NONE,
    ),
    _Case(
        name="migrations_path_matches",
        changed_files=("migrations/001_init.sql",),
        ticket_labels=(),
        contract_touches_pipeline=False,
        expected_is_touching=True,
        expected_reason=EnumPipelineTouchReason.FILE_PATH_MATCH,
        expected_matched_paths=("migrations/001_init.sql",),
    ),
    _Case(
        name="projections_path_matches",
        changed_files=("projections/pr_state.py",),
        ticket_labels=(),
        contract_touches_pipeline=False,
        expected_is_touching=True,
        expected_reason=EnumPipelineTouchReason.FILE_PATH_MATCH,
        expected_matched_paths=("projections/pr_state.py",),
    ),
    _Case(
        name="kafka_path_matches",
        changed_files=("kafka/topics.py",),
        ticket_labels=(),
        contract_touches_pipeline=False,
        expected_is_touching=True,
        expected_reason=EnumPipelineTouchReason.FILE_PATH_MATCH,
        expected_matched_paths=("kafka/topics.py",),
    ),
    _Case(
        name="handlers_path_matches",
        changed_files=("handlers/handler_x.py",),
        ticket_labels=(),
        contract_touches_pipeline=False,
        expected_is_touching=True,
        expected_reason=EnumPipelineTouchReason.FILE_PATH_MATCH,
        expected_matched_paths=("handlers/handler_x.py",),
    ),
    _Case(
        name="nested_pipeline_path_matches",
        changed_files=("src/omnimarket/handlers/handler_foo.py",),
        ticket_labels=(),
        contract_touches_pipeline=False,
        expected_is_touching=True,
        expected_reason=EnumPipelineTouchReason.FILE_PATH_MATCH,
        expected_matched_paths=("src/omnimarket/handlers/handler_foo.py",),
    ),
    _Case(
        name="leading_slash_path_matches",
        changed_files=("/migrations/002.sql",),
        ticket_labels=(),
        contract_touches_pipeline=False,
        expected_is_touching=True,
        expected_reason=EnumPipelineTouchReason.FILE_PATH_MATCH,
        expected_matched_paths=("/migrations/002.sql",),
    ),
    _Case(
        name="unrelated_path_does_not_match",
        changed_files=("docs/README.md", "src/omnimarket/enums/enum_foo.py"),
        ticket_labels=(),
        contract_touches_pipeline=False,
        expected_is_touching=False,
        expected_reason=EnumPipelineTouchReason.NONE,
    ),
    _Case(
        name="ticket_label_pipeline_matches",
        changed_files=("docs/README.md",),
        ticket_labels=("pipeline",),
        contract_touches_pipeline=False,
        expected_is_touching=True,
        expected_reason=EnumPipelineTouchReason.TICKET_LABEL_MATCH,
        expected_matched_labels=("pipeline",),
    ),
    _Case(
        name="ticket_label_migration_case_insensitive",
        changed_files=("docs/README.md",),
        ticket_labels=("Migration",),
        contract_touches_pipeline=False,
        expected_is_touching=True,
        expected_reason=EnumPipelineTouchReason.TICKET_LABEL_MATCH,
        expected_matched_labels=("Migration",),
    ),
    _Case(
        name="ticket_label_projection_matches",
        changed_files=("docs/README.md",),
        ticket_labels=("bug", "projection"),
        contract_touches_pipeline=False,
        expected_is_touching=True,
        expected_reason=EnumPipelineTouchReason.TICKET_LABEL_MATCH,
        expected_matched_labels=("projection",),
    ),
    _Case(
        name="irrelevant_label_does_not_match",
        changed_files=("docs/README.md",),
        ticket_labels=("bug", "chore"),
        contract_touches_pipeline=False,
        expected_is_touching=False,
        expected_reason=EnumPipelineTouchReason.NONE,
    ),
    _Case(
        name="contract_flag_only",
        changed_files=("docs/README.md",),
        ticket_labels=(),
        contract_touches_pipeline=True,
        expected_is_touching=True,
        expected_reason=EnumPipelineTouchReason.CONTRACT_DECLARATION,
    ),
    _Case(
        name="path_beats_label_and_contract",
        changed_files=("migrations/003.sql", "docs/README.md"),
        ticket_labels=("pipeline",),
        contract_touches_pipeline=True,
        expected_is_touching=True,
        expected_reason=EnumPipelineTouchReason.FILE_PATH_MATCH,
        expected_matched_paths=("migrations/003.sql",),
        expected_matched_labels=("pipeline",),
    ),
    _Case(
        name="label_beats_contract_when_no_path",
        changed_files=("docs/README.md",),
        ticket_labels=("pipeline",),
        contract_touches_pipeline=True,
        expected_is_touching=True,
        expected_reason=EnumPipelineTouchReason.TICKET_LABEL_MATCH,
        expected_matched_labels=("pipeline",),
    ),
    _Case(
        name="multiple_path_matches_preserved",
        changed_files=(
            "migrations/004.sql",
            "kafka/topics.py",
            "docs/foo.md",
        ),
        ticket_labels=(),
        contract_touches_pipeline=False,
        expected_is_touching=True,
        expected_reason=EnumPipelineTouchReason.FILE_PATH_MATCH,
        expected_matched_paths=("migrations/004.sql", "kafka/topics.py"),
    ),
)


@pytest.mark.unit
@pytest.mark.parametrize("case", _CASES, ids=lambda c: c.name)
def test_is_pipeline_touching_pr_table(case: _Case) -> None:
    result = is_pipeline_touching_pr(
        changed_files=case.changed_files,
        ticket_labels=case.ticket_labels,
        contract_touches_pipeline=case.contract_touches_pipeline,
    )

    assert result.is_pipeline_touching is case.expected_is_touching
    assert result.reason is case.expected_reason
    assert result.matched_paths == tuple(case.expected_matched_paths)
    assert result.matched_labels == tuple(case.expected_matched_labels)
    assert result.contract_flag is case.contract_touches_pipeline


@pytest.mark.unit
def test_default_ticket_labels_and_contract_flag_are_safe() -> None:
    """Positional-only path input with defaulted label/contract args still works."""
    result = is_pipeline_touching_pr(("migrations/005.sql",))
    assert result.is_pipeline_touching is True
    assert result.reason is EnumPipelineTouchReason.FILE_PATH_MATCH
    assert result.matched_labels == ()
    assert result.contract_flag is False


@pytest.mark.unit
def test_classification_model_is_frozen() -> None:
    """The result model must be immutable so receipts cannot be mutated in flight."""
    from pydantic import ValidationError

    result = is_pipeline_touching_pr(("migrations/006.sql",))
    with pytest.raises(ValidationError):
        result.is_pipeline_touching = False  # type: ignore[misc]
