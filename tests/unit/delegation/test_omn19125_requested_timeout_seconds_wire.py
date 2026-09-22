# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The consumer must accept the timeout the producer already sends (OMN-19125).

``omnibase_infra``'s ``onex delegate`` writes ``requested_timeout_seconds``
into the delegate input payload whenever ``--timeout`` is passed
(``src/omnibase_infra/cli/cli_delegate.py:1194-1195``). This model declares
``extra="forbid"`` and, before this change, declared no such field -- so
``RuntimeLocal._build_initial_payload`` refused the payload before the handler
was ever constructed, and the caller was told only that the receipt carried no
resolvable delegation terminal.

Verbatim from the capture log of run
``592e83f2-74bb-426b-9055-0387bb58275b`` on 2026-09-21::

    RuntimeLocal: Input payload at .onex_state/tmp/delegate-input-....json does
    not validate against ...ModelDelegateSkillRequest: 1 validation error
    requested_timeout_seconds
      Extra inputs are not permitted [type=extra_forbidden, input_value=300,
      input_type=int]

Measured blast radius (lane ``delegate-venv-discriminator``, across all 714
delegate capture logs in ``.onex_state``): 37 runs carried this validation
error and 37 of 37 produced no receipt. Every delegation issued with
``--timeout`` on 2026-09-21 failed; every one that omitted it was unaffected.

This is the CONSUMER half of the consumer-first rule the sibling module
``test_omn18852_published_at_excluded_when_unset`` states: a new optional field
on a wire contract whose consumers forbid extras either lands consumer-first,
or is excluded when unset. This field does BOTH -- it lands here before any
producer change is required, and it is excluded from serialisation when unset
so this model can itself be produced into a consumer that predates it.

The ``extra="forbid"`` posture is deliberately kept. These tests carry a
positive control for it, because a fix that accepted the field by loosening
the model would pass every other test here while removing the guard that makes
an unknown key a refusal rather than a silent drop.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)

# The payload the CLI actually wrote on the refused runs, reproduced key for
# key rather than paraphrased. ``correlation_id`` is written explicitly by the
# CLI (OMN-14397) and is part of the shape the runtime validated.
_CLI_PAYLOAD_WITH_TIMEOUT: dict[str, object] = {
    "prompt": "Reply with exactly the word READY",
    "task_type": "reasoning",
    "source": "claude-code",
    "correlation_id": "f0575167-a1bb-4513-be2d-1a8122929b6d",
    "requested_timeout_seconds": 300,
}


def _cli_payload(**overrides: object) -> dict[str, object]:
    payload = dict(_CLI_PAYLOAD_WITH_TIMEOUT)
    payload.update(overrides)
    return payload


@pytest.mark.unit
def test_the_refused_cli_payload_now_validates() -> None:
    """The reproduction itself, at the layer that refused it.

    RED before this change: ``ValidationError`` naming
    ``requested_timeout_seconds`` as ``extra_forbidden``.
    """
    request = ModelDelegateSkillRequest(**_cli_payload())  # type: ignore[arg-type]

    assert request.requested_timeout_seconds == 300


@pytest.mark.unit
def test_a_payload_without_the_field_validates_unchanged() -> None:
    """No existing caller changes shape, and the default is NOT MEASURED.

    ``None`` means the caller stated no deadline, so the contract-declared
    handler budget governs alone -- it never means a zero-second deadline.
    """
    payload = _cli_payload()
    del payload["requested_timeout_seconds"]

    request = ModelDelegateSkillRequest(**payload)  # type: ignore[arg-type]

    assert request.requested_timeout_seconds is None


@pytest.mark.unit
def test_an_unknown_field_is_still_refused() -> None:
    """Positive control for ``extra="forbid"``.

    Without this, a fix that simply stopped forbidding extras would satisfy
    every other test in this module while deleting the guard. The guard is
    what makes a producer/consumer field-set disagreement a loud refusal
    rather than a silently dropped parameter -- which is the strictly worse
    failure, because a dropped ``--timeout`` looks like it worked.
    """
    with pytest.raises(ValidationError) as refusal:
        ModelDelegateSkillRequest(  # type: ignore[arg-type]
            **_cli_payload(requested_timeout_secondz=300)
        )

    assert "requested_timeout_secondz" in str(refusal.value)
    assert "extra_forbidden" in str(refusal.value)


@pytest.mark.unit
def test_an_unset_timeout_is_omitted_from_the_wire() -> None:
    """This model is a producer too, so the OMN-18852 rollout rule applies.

    A consumer baked into an image that predates this field forbids extras and
    declares none. Serialising ``"requested_timeout_seconds": null`` would
    refuse every record at that consumer -- which is exactly the outage
    OMN-18852 measured on 2026-09-19 for ``published_at``.
    """
    payload = _cli_payload()
    del payload["requested_timeout_seconds"]

    wire = json.loads(
        ModelDelegateSkillRequest(**payload).model_dump_json()  # type: ignore[arg-type]
    )

    assert "requested_timeout_seconds" not in wire, (
        "OMN-19125: an unset requested_timeout_seconds serialised anyway, so a "
        "consumer predating this field refuses the record as "
        f"publisher-malformed. Emitted keys: {sorted(wire)}"
    )


@pytest.mark.unit
def test_a_set_timeout_survives_the_round_trip() -> None:
    """The exclusion must not make the field unusable when it IS set."""
    wire = json.loads(
        ModelDelegateSkillRequest(**_cli_payload()).model_dump_json()  # type: ignore[arg-type]
    )

    assert wire["requested_timeout_seconds"] == 300
    assert ModelDelegateSkillRequest(**wire).requested_timeout_seconds == 300


@pytest.mark.unit
@pytest.mark.parametrize("bad", [0, -1, -300])
def test_a_non_positive_timeout_is_refused(bad: int) -> None:
    """A deadline that has already passed is a caller error, not a fast path.

    Accepting ``0`` would make every such request terminate as a timeout
    before any provider was called, which reads downstream exactly like a
    provider outage.
    """
    with pytest.raises(ValidationError):
        ModelDelegateSkillRequest(**_cli_payload(requested_timeout_seconds=bad))  # type: ignore[arg-type]
