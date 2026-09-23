# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`requested_timeout_seconds` must not reach a consumer that predates it (OMN-19131).

`requested_timeout_seconds` was added to `ModelDelegateSkillRequest` so the
`onex delegate --timeout` flag stops building a payload the request model
forbids. It landed WITHOUT `exclude_if`, which is the second instance of the
rollout class OMN-18852 already cost a live dev-lane outage.

The mechanism is a ROLLOUT property, not a modelling mistake:

* `RuntimeLocal` publishes the command record as
  `model_payload.model_dump_json().encode("utf-8")` at its publish seam
  (`omnibase_core/runtime/runtime_local.py`), so every field the model
  serialises reaches the wire;
* without `exclude_if` the field serialises as `"requested_timeout_seconds":
  null` on EVERY delegation -- including every caller that never passed
  `--timeout` and has no interest in the field at all;
* the model declares `extra="forbid"`;
* the producer advances in SECONDS (the dispatch venv reconciles to the
  producing commit) while a consumer advances in MINUTES TO HOURS (a runtime
  baked into an image), so the null is emitted to consumers that declare no
  such field and refuse the record as publisher-malformed.

Positive control taken on the lab before this fix was written, 2026-09-22, by
importing each container's OWN copy of this model rather than reasoning about
version strings: the `.201` `omnibase-infra` dev lane's `omninode-runtime` and
`omninode-runtime-effects` both report `has_field=True`, while the
`omnibase-infra-stability-test` lane's pair both report `has_field=False
extra=forbid`. A record carrying the null key is therefore accepted by one
live lane and refused by another, at the same moment, from the same producer.

**Why the OMN-18852 guard did not catch this.** That module's
`_PRE_OMN18852_ACCEPTED_FIELDS` is derived as
`ModelDelegateSkillRequest.model_fields.keys() - {"published_at"}`, so it
agrees with any field the model gains afterwards -- exactly the hazard its own
docstring warns about, applied to every field but the one it names. The
emitted keyset here is therefore spelled as a LITERAL, so the next optional
field added to this contract has to make a deliberate decision about the wire
instead of inheriting one.

The rule: **a new optional field on a wire contract whose consumers forbid
extras either lands consumer-first, or is excluded when unset.**
"""

from __future__ import annotations

import json

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)

# What a default-built delegation request is allowed to put on the wire.
#
# Spelled as a literal ON PURPOSE. A set derived from the model under test
# ratifies whatever the model does next, which is how `requested_timeout_seconds`
# reached the wire with the OMN-18852 guard already in the tree and green.
#
# Adding a name here is a decision that every delegation consumer on every lane
# already declares that field. If it does not, give the field
# `exclude_if=lambda value: value is None` instead and leave this set alone.
_WIRE_KEYS_A_DEFAULT_REQUEST_MAY_EMIT = frozenset(
    {
        "acceptance_criteria",
        "backend_id",
        "codex_sandbox_mode",
        "correlation_id",
        "cwd",
        "max_tokens",
        "metadata",
        "prompt",
        "quality_contract_mode",
        "recipient",
        "response_contract",
        "response_format",
        "session_id",
        "source",
        "source_file_path",
        "system_prompt",
        "task_type",
        "temperature",
        "tenant_id",
        "wait",
        "working_directory",
    }
)


def _minimal_request(**overrides: object) -> ModelDelegateSkillRequest:
    """Build a request the way a caller that never passed `--timeout` does."""
    payload: dict[str, object] = {
        "prompt": "summarise a kafka consumer group in two sentences",
        "task_type": "document",
        "source": "claude-code",
    }
    payload.update(overrides)
    return ModelDelegateSkillRequest(**payload)  # type: ignore[arg-type]


@pytest.mark.unit
def test_default_request_does_not_serialise_requested_timeout_seconds() -> None:
    """The regression itself: a caller that passed no timeout must emit no key.

    RED before the fix -- the dump carried `"requested_timeout_seconds": null`.
    """
    wire = json.loads(_minimal_request().model_dump_json())

    assert "requested_timeout_seconds" not in wire, (
        "OMN-19131: a request with no requested timeout serialised "
        "`requested_timeout_seconds`, so every consumer predating the field "
        "refuses the record as publisher-malformed -- measured live on the "
        "`.201` stability-test lane, whose runtime pair declares no such "
        f"field and forbids extras. Emitted keys: {sorted(wire)}"
    )


@pytest.mark.unit
def test_the_emitted_wire_keyset_is_exactly_the_declared_one() -> None:
    """The ratchet, against a literal rather than against the model itself.

    A new optional field that forgets `exclude_if` turns this red and names
    itself, which is the only signal that arrives before a lane refuses
    records.
    """
    emitted = set(json.loads(_minimal_request().model_dump_json()))

    undeclared = emitted - _WIRE_KEYS_A_DEFAULT_REQUEST_MAY_EMIT
    assert not undeclared, (
        f"OMN-19131: a default-built request emits {sorted(undeclared)}, which "
        "no consumer predating that field declares and every one of them "
        "forbids as an extra. Either give the field "
        "`exclude_if=lambda value: value is None`, or land it consumer-first "
        "and add its name to _WIRE_KEYS_A_DEFAULT_REQUEST_MAY_EMIT with the "
        "evidence that every lane already carries it."
    )
    vanished = _WIRE_KEYS_A_DEFAULT_REQUEST_MAY_EMIT - emitted
    assert not vanished, (
        f"OMN-19131: {sorted(vanished)} stopped being emitted. Dropping a key "
        "an existing consumer requires is the same rollout hazard pointed the "
        "other way; remove it from the declared set in the same change that "
        "proves no consumer reads it."
    )


@pytest.mark.unit
def test_a_requested_timeout_is_still_serialised_when_the_caller_sets_one() -> None:
    """Excluding when unset must not make `--timeout` unusable.

    The positive control. Without it, deleting the field entirely would pass
    both tests above, and `onex delegate --timeout` would be refused again --
    the OMN-19131 defect restored by its own fix.
    """
    wire = json.loads(_minimal_request(requested_timeout_seconds=240).model_dump_json())

    assert wire.get("requested_timeout_seconds") == 240, (
        "OMN-19131: a request that DID ask for a timeout dropped the field, so "
        "the handler resolves the task-class ceiling instead and `--timeout` "
        "silently means nothing. The exclusion has over-reached."
    )
    assert ModelDelegateSkillRequest(**wire).requested_timeout_seconds == 240


@pytest.mark.unit
def test_an_omitted_requested_timeout_reads_back_as_none() -> None:
    """A consumer carrying THIS model still reads a request with no timeout.

    `None` is what makes the handler fall back to the task-class execution
    ceiling; it must never read back as a zero-second deadline.
    """
    wire = json.loads(_minimal_request().model_dump_json())

    assert ModelDelegateSkillRequest(**wire).requested_timeout_seconds is None


@pytest.mark.unit
def test_a_zero_or_negative_requested_timeout_is_still_refused() -> None:
    """The exclusion must not weaken the bound it sits beside.

    A zero-second deadline is not a request for the default; it is a deadline
    that can never be met, and the model refuses it rather than resolving it
    to the ceiling.
    """
    for refused in (0, -1):
        with pytest.raises(ValueError, match="greater than or equal to 1"):
            _minimal_request(requested_timeout_seconds=refused)
