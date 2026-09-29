# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18928: every current terminal producer states operation and content facts."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from omnibase_core.enums.enum_delegation_operational_outcome import (
    EnumDelegationOperationalOutcome,
)

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as workflow_module,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    _operational_outcome_for_inference_failure,
)

pytestmark = pytest.mark.unit


def _terminal_construction_sites() -> list[ast.Call]:
    source = Path(inspect.getsourcefile(workflow_module) or "").read_text(
        encoding="utf-8"
    )
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "TerminalEmissionInputs"
    ]


def test_terminal_producer_has_multiple_paths() -> None:
    """Positive control: field-presence checks must cover real producers."""
    assert len(_terminal_construction_sites()) > 1


@pytest.mark.parametrize("field", ["operational_outcome", "content_verdict"])
def test_every_terminal_producer_emits_both_new_facts(field: str) -> None:
    """Neither fact may become an optional omission at a later terminal site."""
    missing = [
        call.lineno
        for call in _terminal_construction_sites()
        if field not in {keyword.arg for keyword in call.keywords if keyword.arg}
    ]

    assert not missing, (
        f"TerminalEmissionInputs omits {field!r} at line(s) {missing}; all new "
        "delegation terminals must distinguish operation from final content."
    )


@pytest.mark.parametrize(
    ("failure_class", "outcome"),
    [
        (
            EnumDelegationFailureClass.RATE_LIMITED,
            EnumDelegationOperationalOutcome.PROVIDER_QUOTA,
        ),
        (
            EnumDelegationFailureClass.MODEL_UNAVAILABLE,
            EnumDelegationOperationalOutcome.PROVIDER_UNAVAILABLE,
        ),
        (
            EnumDelegationFailureClass.TIMEOUT,
            EnumDelegationOperationalOutcome.TIMEOUT,
        ),
        (
            EnumDelegationFailureClass.RUNTIME_RESTART_DURING_DELEGATION,
            EnumDelegationOperationalOutcome.CANCELLED,
        ),
    ],
)
def test_structured_inference_failure_classes_keep_their_operational_meaning(
    failure_class: EnumDelegationFailureClass,
    outcome: EnumDelegationOperationalOutcome,
) -> None:
    """Terminal classification consumes the existing enum, never reason text."""
    assert _operational_outcome_for_inference_failure(failure_class) is outcome
