# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The handler modules cleared of the event envelope stay clear.

The handler-shape validator of omnibase_core reports ``envelope_in_core`` for a
handler module that references the event envelope type. Each module listed here
has been cleared and must stay clear. The validator is run on each module as it
stands, so the rule and its name list stay the validator's own.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.validation.validator_handler_shape import ValidatorHandlerShape

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]

_CLEARED_MODULES: tuple[str, ...] = (
    "src/omnimarket/nodes/node_pr_review_fsm_reducer/handlers/handler_pr_review_fsm.py",
    "src/omnimarket/nodes/node_redeploy_fsm_reducer/handlers/handler_redeploy_fsm.py",
    "src/omnimarket/nodes/node_regression_test_orchestrator/handlers/handler_regression_test_orchestrator.py",
    "src/omnimarket/nodes/node_user_correction_observer_effect/handler_user_correction_observer.py",
)


@pytest.mark.parametrize("module", _CLEARED_MODULES)
def test_the_handler_module_names_no_event_envelope(module: str) -> None:
    path = _ROOT / module
    assert path.is_file()
    result = ValidatorHandlerShape().validate_file(path)
    assert [i.message for i in result.issues if i.code == "envelope_in_core"] == []
