# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: every pack command is pinned to the base commit, and the exemplar is never the answer (OMN-23629)."""

from __future__ import annotations

import importlib

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus

from omnimarket.nodes.node_code_eval_context_arm_compute.models.model_code_eval_context_arm import (
    EnumContextArmDecision,
    EnumContextArmReason,
)

from .helpers import (
    EXEMPLAR_SHA,
    EXEMPLAR_WHEN,
    built,
    handler,
    load_overlay,
    request,
)

pytestmark = pytest.mark.unit

HANDLER_MODULE = (
    "omnimarket.nodes.node_code_eval_context_arm_compute.handlers"
    ".handler_code_eval_context_arm"
)
EXEMPLAR_TIER = (EnumContextFactor.EXEMPLAR, EnumContextSourceStatus.RESOLVED, True, 17)
BASE_TIMES = ("2026-11-01T00:00:00Z", "2026-09-15T12:30:00Z")
PACK_ARMS = ("task_plus_exemplar", "task_plus_pack")


@pytest.mark.parametrize("arm", PACK_ARMS)
@pytest.mark.parametrize("base_commit_time", BASE_TIMES)
def test_every_pack_request_pins_exactly_the_arm_tiers_at_the_base_commit_time(
    arm: str, base_commit_time: str
) -> None:
    overlay = load_overlay()
    result = handler().handle(request("select", arm, base_commit_time=base_commit_time))
    assert result.decision is EnumContextArmDecision.REQUEST_PACK
    assert result.pack_request is not None
    pinned = overlay.context_arms[arm].pinned_tiers
    assert set(result.pack_request.tier_revisions) == set(pinned)
    assert set(result.pack_request.tier_revisions.values()) == {base_commit_time}


def check_the_exemplar_is_never_the_task_answer() -> None:
    """The failing control: a pack that carries the task's own answer never satisfies."""
    task_answer = handler().handle(
        request(
            "judge",
            "task_plus_exemplar",
            pack_answer=built(
                "delegation_payload_exemplar",
                tiers=[EXEMPLAR_TIER],
                exemplar_sha="b" * 12,  # the task's merge commit starts with twelve b's
            ),
        )
    )
    assert task_answer.decision is EnumContextArmDecision.NOT_SATISFIED
    assert task_answer.reason is EnumContextArmReason.EXEMPLAR_IS_TASK_MERGE_COMMIT
    assert task_answer.unscorable_context is True
    assert task_answer.exemplar_ref == "b" * 12

    after_base = handler().handle(
        request(
            "judge",
            "task_plus_exemplar",
            pack_answer=built(
                "delegation_payload_exemplar",
                tiers=[EXEMPLAR_TIER],
                exemplar_when="2026-11-02T00:00:00+00:00",
            ),
        )
    )
    assert after_base.decision is EnumContextArmDecision.NOT_SATISFIED
    assert after_base.reason is EnumContextArmReason.EXEMPLAR_AFTER_BASE
    assert after_base.unscorable_context is True

    unverifiable = handler().handle(
        request(
            "judge",
            "task_plus_exemplar",
            pack_answer=built(
                "delegation_payload_exemplar",
                tiers=[EXEMPLAR_TIER],
                exemplar_body=(
                    "[exemplar: document_excerpt] omnimarket#1@aaaaaaaaaaaa\n"
                    "feat: x (#1)"
                ),
            ),
        )
    )
    assert unverifiable.decision is EnumContextArmDecision.NOT_SATISFIED
    assert unverifiable.reason is EnumContextArmReason.EXEMPLAR_NOT_VERIFIABLE
    assert unverifiable.unscorable_context is True


def test_the_exemplar_is_never_the_task_answer() -> None:
    """Positive control: the no-leak assertions hold green."""
    check_the_exemplar_is_never_the_task_answer()


def test_injected_break_accepting_any_exemplar_line_turns_the_control_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: any exemplar block verifies as one fixed merge before the base."""
    module = importlib.import_module(HANDLER_MODULE)
    monkeypatch.setattr(
        module, "_exemplar_match", lambda _pack_answer: (EXEMPLAR_WHEN, EXEMPLAR_SHA)
    )
    with pytest.raises(AssertionError):
        check_the_exemplar_is_never_the_task_answer()


def test_an_exemplar_before_the_base_with_another_sha_satisfies() -> None:
    result = handler().handle(
        request(
            "judge",
            "task_plus_exemplar",
            pack_answer=built(
                "delegation_payload_exemplar",
                tiers=[EXEMPLAR_TIER],
                exemplar_sha="c" * 12,
            ),
        )
    )
    assert result.decision is EnumContextArmDecision.SATISFIED
    assert result.reason is EnumContextArmReason.OK
    assert result.unscorable_context is False
    assert result.exemplar_ref == "c" * 12
