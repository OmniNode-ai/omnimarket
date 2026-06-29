# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_onboarding.

WS-5 Wave 7 (OMN-13681). COMPUTE archetype -> Variant A: the handler is invoked
in-process and the typed result dict is asserted. The test drives the REAL
``load_canonical_graph`` + ``resolve_policy`` capability-dependency resolver in
``dry_run`` (verify-only) mode, which is deterministic and side-effect-free.

The non-dry-run path executes live verification probes (asyncio.run +
environment checks) and is intentionally out of scope here — exercising the real
planner across policies/capabilities is the multi-param surface that matters.

Param axes (>=3 distinct sets + a negative control):
  * policy "standalone_quickstart" -> resolves the core-install chain.
  * policy "new_employee" -> resolves a strictly larger plan (superset chain).
  * explicit target_capabilities -> resolver honors the requested capability.
  * skip_steps -> the named step is excluded from the resolved plan.
  * unknown policy -> ValueError (NEGATIVE CONTROL: a bad fixture raises).
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_onboarding.handlers.handler_onboarding import (
    HandlerOnboarding,
)
from omnimarket.nodes.node_onboarding.models.model_onboarding_start_command import (
    ModelOnboardingStartCommand,
)

# (case_id, command_kwargs, expect)
_CASES: list[tuple[str, dict[str, object], dict[str, object]]] = [
    (
        "policy-standalone-quickstart",
        {"policy_name": "standalone_quickstart", "dry_run": True},
        {"must_include": ["check_python", "install_uv", "install_core"]},
    ),
    (
        "policy-new-employee-superset",
        {"policy_name": "new_employee", "dry_run": True},
        {"must_include": ["check_python", "install_core"], "min_steps": 4},
    ),
    (
        "explicit-target-capabilities",
        {"target_capabilities": ["core_installed"], "dry_run": True},
        {"must_include": ["install_core"]},
    ),
    (
        "skip-steps-excludes-step",
        {
            "policy_name": "standalone_quickstart",
            "skip_steps": ["install_uv"],
            "dry_run": True,
        },
        {"must_exclude": ["install_uv"]},
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("case_id", "command_kwargs", "expect"),
    _CASES,
    ids=[c[0] for c in _CASES],
)
def test_onboarding_dry_run_resolution(
    case_id: str,
    command_kwargs: dict[str, object],
    expect: dict[str, object],
) -> None:
    result = HandlerOnboarding().handle(ModelOnboardingStartCommand(**command_kwargs))

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["execution_mode"] == "verify-only"
    assert result["completed_steps"] == 0

    steps = result["resolved_steps"]
    assert isinstance(steps, list)
    assert result["total_steps"] == len(steps)
    assert len(steps) >= 1

    # produced_capabilities_per_step must cover every resolved step.
    assert set(result["produced_capabilities_per_step"].keys()) == set(steps)

    for required in expect.get("must_include", []):
        assert required in steps, f"{case_id}: expected step {required!r} in {steps}"
    for excluded in expect.get("must_exclude", []):
        assert excluded not in steps, f"{case_id}: step {excluded!r} should be skipped"
    if "min_steps" in expect:
        assert len(steps) >= expect["min_steps"]


def test_new_employee_plan_is_superset_of_quickstart() -> None:
    """Cross-case structural truth: the broader policy resolves a superset chain."""
    handler = HandlerOnboarding()
    quickstart = handler.handle(
        ModelOnboardingStartCommand(policy_name="standalone_quickstart", dry_run=True)
    )
    new_employee = handler.handle(
        ModelOnboardingStartCommand(policy_name="new_employee", dry_run=True)
    )
    assert set(quickstart["resolved_steps"]).issubset(
        set(new_employee["resolved_steps"])
    )
    assert len(new_employee["resolved_steps"]) > len(quickstart["resolved_steps"])


@pytest.mark.integration
def test_onboarding_unknown_policy_raises() -> None:
    """NEGATIVE CONTROL: an unknown policy name must raise, not silently no-op."""
    with pytest.raises(ValueError, match="Unknown policy"):
        HandlerOnboarding().handle(
            ModelOnboardingStartCommand(policy_name="does_not_exist", dry_run=True)
        )
