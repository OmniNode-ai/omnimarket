# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration proof for node_plan_to_tickets (OMN-13679, WS-5).

Variant A (COMPUTE, direct in-process handler call). ``HandlerPlanToTickets``
parses markdown plan content into structured ticket entries, classifies the
section structure (Task vs Phase headings), extracts inter-entry dependencies,
and rejects cyclic dependency graphs. The handler is pure — it accepts the plan
text directly via ``ModelPlanToTicketsRequest`` (no filesystem read on this
path), so the test feeds synthetic plan strings and asserts typed result fields.

Param axes: task sections, phase sections, dependency links, the § heading edge
case (no parseable structure → error), and a NEGATIVE CONTROL cyclic plan that
must surface a "Circular dependency" finding.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_plan_to_tickets.handlers.handler_plan_to_tickets import (
    HandlerPlanToTickets,
    ModelPlanToTicketsRequest,
    ModelPlanToTicketsResult,
)

_TASK_PLAN = """# Widget Epic

## Task 1: Build the widget
Implement the core widget logic.

## Task 2: Test the widget
Add unit and integration tests.
"""

_PHASE_PLAN = """# Phase Epic

## Phase 1: Design
Write the design doc.

## Phase 2: Implement
Implement the design.
"""

_DEP_PLAN = """# Dependency Epic

## Task 1: Foundation
Lay the groundwork.

## Task 2: Build on it
Dependencies: Task 1
Build the feature on top of the foundation.
"""

# § headings (section-sign) are NOT recognised structure → error path.
_SECTION_SIGN_PLAN = """# Bad Structure Epic

§ 1 Some heading
free-form prose with no ## Task or ## Phase markers
"""

_CYCLE_PLAN = """# Cyclic Epic

## Task 1: First
Dependencies: Task 2
First task body.

## Task 2: Second
Dependencies: Task 1
Second task body.
"""

_CASES = [
    pytest.param(
        _TASK_PLAN,
        {
            "status": "parsed",
            "structure_type": "task_sections",
            "epic_title": "Widget Epic",
            "entry_count": 2,
        },
        id="task-sections",
    ),
    pytest.param(
        _PHASE_PLAN,
        {
            "status": "parsed",
            "structure_type": "phase_sections",
            "epic_title": "Phase Epic",
            "entry_count": 2,
        },
        id="phase-sections",
    ),
    pytest.param(
        _DEP_PLAN,
        {
            "status": "parsed",
            "structure_type": "task_sections",
            "entry_count": 2,
            "dependency_on_p2": ["P1"],
        },
        id="dependency-links",
    ),
    pytest.param(
        _SECTION_SIGN_PLAN,
        {
            "status": "error",
            "has_validation_error_substr": "No valid structure found",
        },
        id="negative-no-structure",
    ),
    pytest.param(
        _CYCLE_PLAN,
        {
            "status": "error",
            "has_validation_error_substr": "Circular dependency",
        },
        id="negative-cyclic-dependency",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(("plan_content", "expected"), _CASES)
def test_plan_to_tickets_multiparam(
    plan_content: str, expected: dict[str, object]
) -> None:
    result = HandlerPlanToTickets().handle(
        ModelPlanToTicketsRequest(plan_content=plan_content)
    )

    assert isinstance(result, ModelPlanToTicketsResult)
    assert result.status == expected["status"]

    if "structure_type" in expected:
        assert result.structure_type == expected["structure_type"]
    if "epic_title" in expected:
        assert result.epic_title == expected["epic_title"]
    if "entry_count" in expected:
        assert result.entry_count == expected["entry_count"]
        assert len(result.entries) == expected["entry_count"]

    if "dependency_on_p2" in expected:
        # The second task declares a dependency on the first (P1).
        p2 = next(e for e in result.entries if e.entry_id == "P2")
        assert p2.dependencies == expected["dependency_on_p2"]

    if "has_validation_error_substr" in expected:
        # NEGATIVE CONTROL: malformed/cyclic plan must emit a structured finding.
        assert result.validation_errors, "expected a validation error"
        assert any(
            str(expected["has_validation_error_substr"]) in err
            for err in result.validation_errors
        ), result.validation_errors
    else:
        assert result.validation_errors == []


@pytest.mark.integration
def test_empty_section_body_is_a_finding() -> None:
    """A heading with no body must produce a per-entry validation error."""
    plan = "# Epic\n\n## Task 1: Empty\n\n## Task 2: Has body\nreal content\n"
    result = HandlerPlanToTickets().handle(ModelPlanToTicketsRequest(plan_content=plan))
    assert result.status == "error"
    assert any("has no content" in err for err in result.validation_errors)
