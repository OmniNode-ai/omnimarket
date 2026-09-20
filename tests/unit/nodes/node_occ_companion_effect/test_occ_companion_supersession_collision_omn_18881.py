# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A supersession-binding collision parks with a legible reason (OMN-18881).

The live occurrence these tests encode. On 2026-09-20T06:39:08Z a hand-authored
companion (OCC#10524, branch ``jonah/omn-18595-occ-evidence-1722``) landed
beside an in-flight machine mint for ``omnibase_core#1722``. The compute plan
raised :class:`SupersessionCheckBindingError`; ``classify_mint_failure``
returned ``None``; the exception propagated raw to the auto-wired consume
boundary; and the boundary's sanitizer -- which replaces the WHOLE message when
it contains ``auth``, a substring of ``author`` -- wrote 166 dead letters whose
only reason was ``[REDACTED - potentially sensitive data]``.

The consequence was not local. ``onex.cmd.omnimarket.occ-companion-effect-requested.v1``
lost its consumer, so the backstop mint that normally rescues a declined
autobind stopped for every repository; ``omnibase_infra#3873``, opened at
06:42:49Z, was the first product PR left with no companion and a red
``occ-preflight / eligibility``.

What these tests pin is the disposition, not the refusal. The refusal is
correct and stays: a supersession must not carry a check that is not the
superseded item's own (OMN-15459 AC(d)). What was wrong is that a deterministic
refusal sat outside the taxonomy, and everything outside the taxonomy
propagates raw.

Deliberately NOT tested here, because both would hide this rather than fix it:
widening the consumer's poll interval, and relaxing the container healthcheck's
degraded policy.
"""

from __future__ import annotations

import pytest

from omnimarket.enums.enum_mint_failure_class import EnumMintFailureClass
from omnimarket.enums.enum_mint_failure_disposition import EnumMintFailureDisposition
from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    SupersessionCheckBindingError,
)
from omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect import (
    _CONTRACT_PATH,
    _MINT_RETRY_POLICY,
)
from omnimarket.nodes.node_occ_companion_effect.mint_retry_policy import (
    OccCompanionMintParkedError,
    classify_mint_failure,
    load_mint_retry_policy,
    run_mint_with_policy,
)

pytestmark = pytest.mark.unit

# The live refusal text, from handler_occ_companion_compute's raise site. It
# contains "author" -- the substring that costs the raw message its legibility
# at the boundary -- which is the whole reason this shape is reproduced here
# rather than stubbed with a short string.
_LIVE_COLLISION_MESSAGE = (
    "refusing to author OMN-18595's supersession cohort for PR #1722: "
    "['dod-occ-diff-derived-behavior-proof:test_passes', "
    "'dod-occ-evidence-companion:test_passes'] all rebind to "
    "'gh pr view 1722'. A replacement's check_value must discriminate the "
    "item it supersedes."
)


def _collision() -> SupersessionCheckBindingError:
    """The OCC#10524 collision shape, message included."""
    return SupersessionCheckBindingError(_LIVE_COLLISION_MESSAGE)


def test_the_collision_is_inside_the_taxonomy() -> None:
    """The RED control for this whole ticket: before the fix this returned None."""
    assert (
        classify_mint_failure(_collision())
        is EnumMintFailureClass.EVIDENCE_BINDING_COLLISION
    )


def test_a_plain_value_error_is_still_outside_the_taxonomy() -> None:
    """The arm is the specific refusal, not every ValueError.

    ``SupersessionCheckBindingError`` subclasses ``ValueError``, so an
    over-broad arm would swallow genuine compute-plan defects into a
    preserve-and-replay path where they would be replayed forever.
    """
    assert classify_mint_failure(ValueError("companion plan is malformed")) is None


def test_the_contract_declares_the_collision_disposition_as_park() -> None:
    """Contract-declared, not a Python default. A retry reproduces it exactly."""
    policy = load_mint_retry_policy(_CONTRACT_PATH)
    assert (
        policy.disposition_for(EnumMintFailureClass.EVIDENCE_BINDING_COLLISION)
        is EnumMintFailureDisposition.PARK
    )


@pytest.mark.asyncio
async def test_the_collision_is_handled_exactly_once_and_never_retried() -> None:
    """One delivery, one handling. A retry would spend the poll interval.

    This is the property that keeps the consumer in its group: the handler
    returns control to the poll loop after a single attempt instead of
    occupying it, so a single hand-authored collision cannot evict the group
    the way it did on 2026-09-20.
    """
    attempts = 0

    async def _mint() -> str:
        nonlocal attempts
        attempts += 1
        raise _collision()

    with pytest.raises(OccCompanionMintParkedError) as caught:
        await run_mint_with_policy(
            _mint,
            policy=_MINT_RETRY_POLICY,
            repo="OmniNode-ai/omnibase_core",
            pr_number=1722,
        )

    assert attempts == 1, (
        "a deterministic collision must be handled once; "
        f"it was attempted {attempts} times"
    )
    assert caught.value.failure_class is EnumMintFailureClass.EVIDENCE_BINDING_COLLISION
    assert caught.value.attempts == 1


@pytest.mark.asyncio
async def test_the_underlying_refusal_is_preserved_as_the_cause() -> None:
    """Parking must not destroy the diagnosis it replaces."""

    async def _mint() -> str:
        raise _collision()

    with pytest.raises(OccCompanionMintParkedError) as caught:
        await run_mint_with_policy(
            _mint,
            policy=_MINT_RETRY_POLICY,
            repo="OmniNode-ai/omnibase_core",
            pr_number=1722,
        )

    cause = caught.value.__cause__
    assert isinstance(cause, SupersessionCheckBindingError)
    assert "1722" in str(cause)


def test_the_raw_collision_reason_is_destroyed_by_the_boundary_sanitizer() -> None:
    """The control that proves the fix is worth making.

    This is the live dev-lane condition: 166 dead letters, every one of them
    reading only the redaction marker, because the refusal names an *author*.
    """
    from omnibase_infra.utils.util_error_sanitization import sanitize_error_message

    assert "REDACTED" in sanitize_error_message(_collision())


def test_the_parked_collision_reason_survives_the_sanitizer() -> None:
    """After parking, the dead letter names the PR, the class and the remedy."""
    from omnibase_infra.utils.util_error_sanitization import sanitize_error_message

    parked = OccCompanionMintParkedError(
        repo="OmniNode-ai/omnibase_core",
        pr_number=1722,
        failure_class=EnumMintFailureClass.EVIDENCE_BINDING_COLLISION,
        attempts=1,
        max_attempts=3,
    )
    sanitized = sanitize_error_message(parked)
    assert "REDACTED" not in sanitized
    assert "evidence_binding_collision" in sanitized
    assert "OmniNode-ai/omnibase_core#1722" in sanitized
    assert "replayable" in sanitized
