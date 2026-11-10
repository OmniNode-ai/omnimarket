# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1: the three context arms select what they name, and no other arm is served (OMN-23629)."""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from pydantic import ValidationError

from omnimarket.models.context_profile_resolve.model_context_profile_resolve_request import (
    ModelContextProfileResolveRequest,
)
from omnimarket.models.context_profile_resolve.model_context_profile_resolve_result import (
    EnumContextProfileResolveStatus,
)
from omnimarket.nodes.node_code_eval_context_arm_compute.models.model_code_eval_context_arm import (
    EnumContextArmDecision,
    EnumContextArmReason,
    ModelContextArmSpec,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)
from omnimarket.nodes.node_context_roi_compute.models.model_factor_arm import (
    EnumArmLabel,
)
from omnimarket.nodes.node_context_roi_compute.models.model_factor_matrix import (
    arm_by_label,
    build_code_eval_context_arms,
)

from .helpers import backend, built, handler, load_overlay, request

pytestmark = pytest.mark.unit

PROFILES_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "pack" / "profiles"
)


def test_every_fixture_arm_is_the_code_eval_arm_for_its_label() -> None:
    overlay = load_overlay()
    arms = build_code_eval_context_arms()
    assert {spec.arm_label for spec in overlay.context_arms.values()} == {
        EnumArmLabel.OFF,
        EnumArmLabel.EXEMPLAR_ONLY,
        EnumArmLabel.TIERED_PACK,
    }
    for spec in overlay.context_arms.values():
        assert spec.factor_arm() == arm_by_label(arms, spec.arm_label)


def test_a_pinned_tier_outside_the_arms_factors_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelContextArmSpec(
            profile="delegation_payload_exemplar",
            arm_label=EnumArmLabel.EXEMPLAR_ONLY,
            pinned_tiers=(EnumContextFactor.EXEMPLAR, EnumContextFactor.INVARIANTS),
            min_included_tiers=1,
        )


def test_task_alone_needs_no_pack() -> None:
    result = handler().handle(request("select", "task_alone"))
    assert result.decision is EnumContextArmDecision.NO_PACK_NEEDED
    assert result.reason is EnumContextArmReason.OK
    assert result.pack_request is None
    assert result.pack_budget_tokens is None
    assert result.token_estimate == 0
    assert result.token_estimate_basis == "none"


def test_task_plus_exemplar_requests_the_exemplar_profile() -> None:
    result = handler().handle(request("select", "task_plus_exemplar"))
    assert result.decision is EnumContextArmDecision.REQUEST_PACK
    assert result.reason is EnumContextArmReason.OK
    assert result.pack_request is not None
    assert result.pack_request.profile_name == "delegation_payload_exemplar"


def test_task_plus_pack_requests_the_local_profile() -> None:
    result = handler().handle(request("select", "task_plus_pack"))
    assert result.decision is EnumContextArmDecision.REQUEST_PACK
    assert result.reason is EnumContextArmReason.OK
    assert result.pack_request is not None
    assert result.pack_request.profile_name == "delegation_payload_local"


@pytest.mark.parametrize("operation", ["select", "judge"])
def test_an_arm_the_overlay_does_not_name_is_refused(operation: str) -> None:
    answer = (
        built(
            "delegation_payload_exemplar",
            tiers=[
                (EnumContextFactor.EXEMPLAR, EnumContextSourceStatus.RESOLVED, True, 17)
            ],
        )
        if operation == "judge"
        else None
    )
    result = handler().handle(
        request(operation, "omn23629_unnamed_arm", pack_answer=answer)
    )
    assert result.decision is EnumContextArmDecision.REFUSED
    assert result.reason is EnumContextArmReason.ARM_NOT_IN_OVERLAY
    assert result.pack_request is None


@pytest.mark.parametrize("archetype", [None, "unmapped_archetype"])
def test_a_task_with_neither_lane_kind_nor_mapped_archetype_is_refused(
    archetype: str | None,
) -> None:
    result = handler().handle(
        request("select", "task_plus_exemplar", node_archetype=archetype)
    )
    assert result.decision is EnumContextArmDecision.REFUSED
    assert result.reason is EnumContextArmReason.NO_LANE_KIND
    assert result.pack_request is None


@pytest.mark.parametrize(("composed", "ceiling"), [(1000, 1000), (2000, 1000)])
def test_no_room_under_the_ceiling_refuses(composed: int, ceiling: int) -> None:
    req = request("select", "task_plus_exemplar").model_copy(
        update={"backend": backend(composed=composed, ceiling=ceiling)}
    )
    result = handler().handle(req)
    assert result.decision is EnumContextArmDecision.REFUSED
    assert result.reason is EnumContextArmReason.NO_ROOM
    assert result.pack_request is None


@pytest.mark.parametrize(("room", "expected_budget"), [(500, 500), (100_000, 8000)])
def test_the_budget_is_the_overlay_budget_lowered_to_the_room(
    room: int, expected_budget: int
) -> None:
    overlay = load_overlay()
    req = request("select", "task_plus_exemplar").model_copy(
        update={"backend": backend(composed=1000, ceiling=1000 + room)}
    )
    result = handler().handle(req)
    assert result.decision is EnumContextArmDecision.REQUEST_PACK
    assert result.pack_request is not None
    assert result.pack_budget_tokens == expected_budget
    assert result.pack_request.token_budget_override == expected_budget
    assert result.token_estimate == expected_budget
    assert result.token_estimate_basis == "budget_ceiling"
    assert expected_budget <= overlay.pack_budget_tokens


def test_the_pack_request_carries_the_caller_scope() -> None:
    result = handler().handle(request("select", "task_plus_exemplar"))
    assert result.pack_request is not None
    scope = result.pack_request.caller_scope
    assert scope.repository == "omnimarket"
    assert scope.node_archetype == "compute"
    assert scope.lane_kind == "design-build"  # the overlay maps the compute archetype


def test_a_task_lane_kind_wins_over_the_archetype_map() -> None:
    result = handler().handle(
        request("select", "task_plus_exemplar", lane_kind="review_only")
    )
    assert result.pack_request is not None
    assert result.pack_request.caller_scope.lane_kind == "review_only"


@pytest.mark.usefixtures("promoted_context_profile_routing")
def test_every_profile_an_arm_names_resolves_under_the_real_profiles_dir() -> None:
    overlay = load_overlay()
    named = [
        spec.profile
        for spec in overlay.context_arms.values()
        if spec.profile is not None
    ]
    assert named, "positive control: at least one arm names a profile"
    for profile_name in named:
        assert (PROFILES_DIR / f"{profile_name}.yaml").is_file(), profile_name
        resolved = HandlerContextProfileResolver().handle(
            ModelContextProfileResolveRequest(profile_name=profile_name)
        )
        assert resolved.status is EnumContextProfileResolveStatus.OK, resolved.errors
        assert resolved.profile is not None
        assert resolved.profile_name == profile_name
