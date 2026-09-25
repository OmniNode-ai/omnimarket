# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consumer-side wire tests for ``ModelDelegateSkillRequest`` that hold at both steps (OMN-18931).

Step 1 (omnimarket#2840) made the model tolerate the wire key ``no_escalation``
without declaring it. Step 2 declares ``no_escalation: bool = False``: the key
is omitted from the payload when false, a true value requires a ``backend_id``
pin, and ``null`` is refused as not a boolean.

This file keeps only the consumer-side tests that still hold at step 2:
``backend_id`` and ``requested_timeout_seconds`` round-trip, an ordinary
request emits only declared keys, an explicit false is accepted from a dict and
from JSON, and a Wire Compatibility Gate style replay of every declared key
finds no ``extra_forbidden`` or ``missing`` error. The step-1-only tests (field
not declared, null accepted, true refused) are removed. Their step-2
successors live in ``tests/unit/delegation/test_omn18931_no_escalation_field.py``.

The file is kept, not deleted, because the merged OMN-18931 change-control
contract re-executes this exact path as the behaviour proof of omnimarket#2840
at every later OMN-18931 PR head.
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
# The key omnibase_infra#3951 publishes, spelled as a literal.
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


def test_an_explicit_false_is_accepted_and_dropped() -> None:
    request = ModelDelegateSkillRequest.model_validate(_payload(no_escalation=False))
    assert request.prompt == "exercise the declared dogfood fault route"


def test_an_ordinary_no_escalation_value_is_accepted_from_json() -> None:
    raw = json.dumps(_payload(no_escalation=False))
    request = ModelDelegateSkillRequest.model_validate_json(raw)
    assert request.source == "codex"


def test_the_gate_replay_of_a_step_2_producer_finds_no_wire_shape_error() -> None:
    """Replay the way the Wire Compatibility Gate does.

    The gate sends every emitted key with one placeholder value and grades only
    ``extra_forbidden`` and ``missing``. Against this model, the step-2 key set
    (the declared fields plus ``no_escalation``) must raise neither.
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
