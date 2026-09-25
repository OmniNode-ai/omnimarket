# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegate-skill request is released as a ``no_escalation`` consumer first (OMN-18931).

omnibase_infra's dogfood fault-route port (omnibase_infra#3951) publishes a
pinned request with ``backend_id``, ``requested_timeout_seconds`` and
``no_escalation: true``. The first two are already declared on
``ModelDelegateSkillRequest``. ``no_escalation`` is not, and the model forbids
extras, so every such record is refused at the decode boundary.

Declaring the field in one step is refused by the Wire Compatibility Gate
(``scripts/ci/check_wire_compatibility.py``): it replays the maximal key set
through the LAST RELEASED model, and that model forbids the key. The gate's own
remedy is the release order, so this is step 1: the model TOLERATES the key
without declaring it, and the release that carries this becomes the consumer
that lets step 2 declare the field.

Tolerating is not the same as dropping everything. ``false`` (or ``null``) is
the ordinary route, so it is dropped. ``true`` asks for a policy this release
cannot honour, and dropping it would turn a one-hop fault control into an
ordinary escalating delegation without anyone seeing it. So ``true`` is refused
by name. The gate grades only ``extra_forbidden`` and ``missing``, so a named
refusal still counts as a consumer that decodes the key.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)

pytestmark = pytest.mark.unit

# Spelled here, not imported, so the gate's grading rule is pinned by this
# test rather than by whatever the probe module says today.
_WIRE_SHAPE_ERROR_TYPES = frozenset({"extra_forbidden", "missing"})
_PROBE_PLACEHOLDER = "__wire_compat_probe__"
# The key omnibase_infra#3951 publishes. A literal, so this module collects
# against a tree that predates the tolerance and fails on behaviour, not import.
NO_ESCALATION_WIRE_KEY = "no_escalation"


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "prompt": "exercise the declared dogfood fault route",
        "task_type": "document",
        "source": "codex",
    }
    payload.update(overrides)
    return payload


def test_backend_id_round_trips_through_the_wire() -> None:
    """Positive control: the pin is already a declared field."""
    published = ModelDelegateSkillRequest.model_validate(
        _payload(backend_id="dogfood-fault-429")
    )
    consumed = ModelDelegateSkillRequest.model_validate_json(
        published.model_dump_json()
    )
    assert consumed.backend_id == "dogfood-fault-429"


def test_requested_timeout_seconds_round_trips_through_the_wire() -> None:
    """Positive control: the timeout is already a declared field."""
    published = ModelDelegateSkillRequest.model_validate(
        _payload(requested_timeout_seconds=240)
    )
    consumed = ModelDelegateSkillRequest.model_validate_json(
        published.model_dump_json()
    )
    assert consumed.requested_timeout_seconds == 240


def test_an_unset_timeout_and_pin_add_no_key_an_old_consumer_lacks() -> None:
    """An ordinary request emits exactly the keys the model declares, no more."""
    wire = json.loads(
        ModelDelegateSkillRequest.model_validate(_payload()).model_dump_json()
    )
    assert set(wire) <= set(ModelDelegateSkillRequest.model_fields)
    assert "requested_timeout_seconds" not in wire


def test_the_step_1_model_does_not_emit_no_escalation() -> None:
    """Step 1 declares no field, so no producer on this release can emit the key.

    Declaring it here is what the gate refuses against the last release.
    """
    assert NO_ESCALATION_WIRE_KEY not in ModelDelegateSkillRequest.model_fields
    request = ModelDelegateSkillRequest.model_validate(
        _payload(no_escalation=False, backend_id="dogfood-fault-429")
    )
    assert NO_ESCALATION_WIRE_KEY not in json.loads(request.model_dump_json())


@pytest.mark.parametrize("value", [False, None])
def test_an_ordinary_no_escalation_value_is_accepted_and_dropped(
    value: object,
) -> None:
    request = ModelDelegateSkillRequest.model_validate(_payload(no_escalation=value))
    assert request.prompt == "exercise the declared dogfood fault route"


def test_an_ordinary_no_escalation_value_is_accepted_from_json() -> None:
    raw = json.dumps(_payload(no_escalation=False))
    request = ModelDelegateSkillRequest.model_validate_json(raw)
    assert request.source == "codex"


def test_true_is_refused_by_name_not_dropped() -> None:
    """A true value asks for a policy this release cannot honour."""
    with pytest.raises(ValidationError) as caught:
        ModelDelegateSkillRequest.model_validate(
            _payload(
                backend_id="dogfood-fault-429",
                requested_timeout_seconds=240,
                no_escalation=True,
            )
        )
    errors = caught.value.errors()
    assert [error["type"] for error in errors] == ["value_error"]
    assert NO_ESCALATION_WIRE_KEY in str(errors[0]["msg"])


def test_the_gate_replay_of_a_step_2_producer_finds_no_wire_shape_error() -> None:
    """Replay the way the Wire Compatibility Gate will, once step 2 declares the field.

    The gate sends every emitted key with one placeholder value and grades only
    ``extra_forbidden`` and ``missing``. Against this model, the step-2 key set
    (today's fields plus ``no_escalation``) must raise neither.
    """
    keys = [*ModelDelegateSkillRequest.model_fields, NO_ESCALATION_WIRE_KEY]
    try:
        ModelDelegateSkillRequest.model_validate(
            dict.fromkeys(keys, _PROBE_PLACEHOLDER)
        )
    except ValidationError as exc:
        shape_errors = [
            error for error in exc.errors() if error["type"] in _WIRE_SHAPE_ERROR_TYPES
        ]
    else:
        shape_errors = []
    assert shape_errors == []
