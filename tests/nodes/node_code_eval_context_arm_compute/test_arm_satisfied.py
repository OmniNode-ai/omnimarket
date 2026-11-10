# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2: what satisfies a pack arm and what never does (OMN-23629).

An answer whose exemplar tier is unavailable is unscorable, never a pack that quietly
scores the task alone; the failing control holds those assertions, and the injected
break "accept any pack answer with status ok" turns it red.
"""

from __future__ import annotations

import importlib

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus

from omnimarket.models.context_pack_orchestrator import (
    EnumContextPackOrchestratorStatus,
)
from omnimarket.nodes.node_code_eval_context_arm_compute.models.model_code_eval_context_arm import (
    EnumContextArmDecision,
    EnumContextArmReason,
)

from .helpers import (
    EXEMPLAR_SHA,
    FINAL_PACK_HASH,
    built,
    handler,
    request,
)

pytestmark = pytest.mark.unit

HANDLER_MODULE = (
    "omnimarket.nodes.node_code_eval_context_arm_compute.handlers"
    ".handler_code_eval_context_arm"
)
F = EnumContextFactor
RESOLVED = EnumContextSourceStatus.RESOLVED
UNAVAILABLE = EnumContextSourceStatus.UNAVAILABLE
EXEMPLAR_TIER = (F.EXEMPLAR, RESOLVED, True, 17)


def check_unavailable_exemplar_is_not_satisfied() -> None:
    """The failing control: an unavailable exemplar tier satisfies no arm that requires it."""
    exemplar_arm = handler().handle(
        request(
            "judge",
            "task_plus_exemplar",
            pack_answer=built(
                "delegation_payload_exemplar",
                tiers=[(F.EXEMPLAR, UNAVAILABLE, False, 0)],
            ),
        )
    )
    assert exemplar_arm.decision is EnumContextArmDecision.NOT_SATISFIED
    assert exemplar_arm.reason is EnumContextArmReason.REQUIRED_TIER_MISSING
    assert exemplar_arm.unscorable_context is True

    pack_arm = handler().handle(
        request(
            "judge",
            "task_plus_pack",
            pack_answer=built(
                "delegation_payload_local",
                tiers=[
                    (F.INVARIANTS, RESOLVED, True, 5),
                    (F.CANONICAL_SHAPES, RESOLVED, True, 7),
                    (F.EXEMPLAR, UNAVAILABLE, False, 0),
                ],
            ),
        )
    )
    assert pack_arm.decision is EnumContextArmDecision.NOT_SATISFIED
    assert pack_arm.reason is EnumContextArmReason.REQUIRED_TIER_MISSING
    assert pack_arm.unscorable_context is True


def test_an_unavailable_exemplar_does_not_satisfy_either_pack_arm() -> None:
    """Positive control: the unavailable-exemplar assertions hold green."""
    check_unavailable_exemplar_is_not_satisfied()


def test_injected_break_accepting_any_ok_pack_answer_turns_the_control_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: no required tier is ever missing, so any ok answer satisfies."""
    module = importlib.import_module(HANDLER_MODULE)
    monkeypatch.setattr(module, "_required_tiers_missing", lambda _spec, _included: ())
    with pytest.raises(AssertionError):
        check_unavailable_exemplar_is_not_satisfied()


def test_the_exemplar_arm_is_satisfied_by_an_included_exemplar_tier() -> None:
    result = handler().handle(
        request(
            "judge",
            "task_plus_exemplar",
            pack_answer=built("delegation_payload_exemplar", tiers=[EXEMPLAR_TIER]),
        )
    )
    assert result.decision is EnumContextArmDecision.SATISFIED
    assert result.reason is EnumContextArmReason.OK
    assert result.unscorable_context is False
    assert result.token_estimate == 17
    assert result.token_estimate_basis == "included_tiers"
    assert result.included_tiers == ("exemplar",)
    assert result.unresolved_tiers == ()
    assert result.excluded_by_budget == ()
    assert result.final_pack_hash == FINAL_PACK_HASH
    assert result.exemplar_ref == EXEMPLAR_SHA


def test_the_pack_arm_is_satisfied_by_the_full_pack() -> None:
    result = handler().handle(
        request(
            "judge",
            "task_plus_pack",
            pack_answer=built(
                "delegation_payload_local",
                tiers=[
                    (F.INVARIANTS, RESOLVED, True, 5),
                    (F.CANONICAL_SHAPES, RESOLVED, True, 7),
                    EXEMPLAR_TIER,
                    (F.ARCHITECTURE_PATTERNS, UNAVAILABLE, False, 0),
                ],
                excluded_by_budget=(F.ARCHITECTURE_PATTERNS,),
            ),
        )
    )
    assert result.decision is EnumContextArmDecision.SATISFIED
    assert result.reason is EnumContextArmReason.OK
    assert result.token_estimate == 5 + 7 + 17
    assert result.token_estimate_basis == "included_tiers"
    assert result.included_tiers == ("invariants", "canonical_shapes", "exemplar")
    assert result.unresolved_tiers == ("architecture_patterns",)
    assert result.excluded_by_budget == ("architecture_patterns",)
    assert result.final_pack_hash == FINAL_PACK_HASH
    assert result.exemplar_ref == EXEMPLAR_SHA


def test_the_pack_arm_refuses_a_one_tier_pack() -> None:
    result = handler().handle(
        request(
            "judge",
            "task_plus_pack",
            pack_answer=built("delegation_payload_local", tiers=[EXEMPLAR_TIER]),
        )
    )
    assert result.decision is EnumContextArmDecision.NOT_SATISFIED
    assert result.reason is EnumContextArmReason.TOO_FEW_TIERS
    assert result.unscorable_context is True


def test_a_pack_built_for_another_profile_does_not_satisfy() -> None:
    result = handler().handle(
        request(
            "judge",
            "task_plus_exemplar",
            pack_answer=built("delegation_payload_local", tiers=[EXEMPLAR_TIER]),
        )
    )
    assert result.decision is EnumContextArmDecision.NOT_SATISFIED
    assert result.reason is EnumContextArmReason.PROFILE_MISMATCH
    assert result.unscorable_context is True


@pytest.mark.parametrize("arm", ["task_plus_exemplar", "task_plus_pack"])
def test_a_pack_arm_without_an_answer_is_not_satisfied(arm: str) -> None:
    result = handler().handle(request("judge", arm))
    assert result.decision is EnumContextArmDecision.NOT_SATISFIED
    assert result.reason is EnumContextArmReason.PACK_NOT_ANSWERED
    assert result.unscorable_context is True
    assert result.token_estimate == 0
    assert result.token_estimate_basis == "none"
    assert result.final_pack_hash is None


def test_a_failed_pack_build_does_not_satisfy() -> None:
    result = handler().handle(
        request(
            "judge",
            "task_plus_exemplar",
            pack_answer=built(
                "delegation_payload_exemplar",
                status=EnumContextPackOrchestratorStatus.FAILED,
                failure_class=EnumContextPackFailure.PROFILE_NOT_FOUND,
            ),
        )
    )
    assert result.decision is EnumContextArmDecision.NOT_SATISFIED
    assert result.reason is EnumContextArmReason.PACK_FAILED
    assert result.unscorable_context is True


def test_task_alone_is_satisfied_without_a_pack() -> None:
    result = handler().handle(request("judge", "task_alone"))
    assert result.decision is EnumContextArmDecision.SATISFIED
    assert result.reason is EnumContextArmReason.OK
    assert result.unscorable_context is False
    assert result.token_estimate == 0
    assert result.token_estimate_basis == "none"
    assert result.final_pack_hash is None
    assert result.exemplar_ref is None


def test_task_alone_with_a_pack_answer_is_not_satisfied() -> None:
    result = handler().handle(
        request(
            "judge",
            "task_alone",
            pack_answer=built("delegation_payload_local", tiers=[EXEMPLAR_TIER]),
        )
    )
    assert result.decision is EnumContextArmDecision.NOT_SATISFIED
    assert result.reason is EnumContextArmReason.UNEXPECTED_PACK
    assert result.unscorable_context is True
    assert result.included_tiers == ("exemplar",)


def test_a_pack_the_ceiling_cannot_hold_is_refused() -> None:
    result = handler().handle(
        request(
            "judge",
            "task_plus_exemplar",
            pack_answer=built(
                "delegation_payload_exemplar",
                tiers=[(F.EXEMPLAR, RESOLVED, True, 8000)],
            ),
        )
    )
    assert result.decision is EnumContextArmDecision.REFUSED
    assert result.reason is EnumContextArmReason.OVER_INPUT_CEILING
    assert result.unscorable_context is False
    assert result.token_estimate == 8000
    assert result.token_estimate_basis == "included_tiers"
    assert result.exemplar_ref == EXEMPLAR_SHA
