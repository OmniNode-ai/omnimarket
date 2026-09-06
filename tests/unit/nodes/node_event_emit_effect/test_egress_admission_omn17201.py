# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17201: a record this transform has processed must be able to cross the trust boundary.

RED-first. The premise both halves of the pipeline were built on is that the
emit seam stamps a state the egress gate admits:

* ``omnibase_infra`` ``node_bus_forwarder_effect/contract.yaml`` widens
  ``mirror_topics.outbound`` to the two governed hook classes behind
  ``egress_redaction`` and says so in terms -- "until the upstream seam
  deploys and starts stamping, these two topics admit NOTHING", i.e. once it
  does stamp, they cross.
* That node's ``models/model_gateway_egress_redaction.py`` refuses ``raw``
  **structurally**, not by configuration: "it is ArtifactStore's default state
  (OMN-13152) ... so a policy that admitted it would read exactly like a
  working one while gating nothing."

Those two clauses are only consistent if ``redact_capture`` never stamps
``raw``. It does today, and that is the defect this file pins.

Measured, 2026-09-06, against the record captured off the live lane (the
fixture ``omnibase_infra`` OMN-17981 read with ``rpk topic consume
onex.evt.omniclaude.tool-executed.v1 -p 0 -o 34710 -n 1``): every field the
live hook emits on that topic is declared ``capture_verbatim`` by
``contracts/capture_redaction.yaml``, nothing is hashed, dropped or shaped,
no secret pattern fires -- so ``state`` never leaves its ``RAW`` initial
value and the record is stamped ``redaction_state: "raw"``. The gate then
refuses it by construction. The whole outbound leg for that topic is
therefore closed even after it is wired, which matches the live disposition
recorded on OMN-17201: 61 outbound lines on ``tool-executed``, zero
acknowledged, against a control topic that delivers.

The fix is a floor, not a widening. ``EnumArtifactRedactionState.REDACTED``
is defined in ``omnibase_core`` as "a redaction transform has been applied;
the stored bytes are sanitized" -- which is exactly and only what is true of
every record leaving this function: its fields were each resolved against a
declared class, an undeclared one was hashed by the fail-closed default, and
the secret scrub ran over everything that survived verbatim. ``raw`` means
*no posture was applied*, and after this transform that is never the case.
Nothing here touches ``governed_topics`` or ``admitted_states``; the gate is
byte-unchanged and still refuses ``raw``.
"""

from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_event_emit_effect.redaction import (
    EnumRedactionState,
    redact_capture,
)

TOOL_TOPIC = "onex.evt.omniclaude.tool-executed.v1"
PROMPT_TOPIC = "onex.evt.omniclaude.prompt-submitted.v1"

# The egress gate's admission policy, mirrored from
# omnibase_infra ``node_bus_forwarder_effect/contract.yaml``
# (``config.gateway_forwarder.egress_redaction``). Mirrored rather than
# imported for the same reason that contract mirrors the core enum: this is a
# wire-level fact about another repo's declared policy, and a test that
# imported it could not fail when the two drift.
GATEWAY_STATE_FIELD = "redaction_state"
GATEWAY_ADMITTED_STATES = frozenset({"redacted", "restricted", "secret_detected"})
GATEWAY_NEVER_ADMITTED = frozenset({"raw"})


def _gateway_admits(payload: dict[str, object]) -> bool:
    """``ModelGatewayEgressRedaction.admits``, mirrored exactly.

    A missing field, a non-string value and an unadmitted state are the same
    answer -- an indeterminate posture at a trust boundary is a refusal.
    """
    state = payload.get(GATEWAY_STATE_FIELD)
    if not isinstance(state, str):
        return False
    return state in GATEWAY_ADMITTED_STATES


# ---------------------------------------------------------------------------
# The captured record (AC: a record read off the lane, not one invented here)
# ---------------------------------------------------------------------------
# Verbatim from the ``omnibase_infra`` OMN-17981 fixture, which was read
# read-only off the stability lane on 2026-09-06 with ``rpk topic consume``.
# The only substitution there and here is the live agent-session UUID, which
# is replaced by a fixed placeholder of identical form.
_SESSION_UUID = "00000000-0000-4000-8000-000000000001"
CAPTURED_TOOL_EXECUTED: dict[str, object] = json.loads(
    '{"duration_ms": 361, "hook_source": "post_tool_use", "interrupted": false, '
    f'"session_id": "{_SESSION_UUID}", '
    '"tool_name": "Bash", "working_directory": "omni_home", '
    f'"correlation_id": "{_SESSION_UUID}", '
    '"causation_id": null, '
    '"emitted_at": "2026-09-06T07:47:38.731279+00:00", '
    f'"entity_id": "{_SESSION_UUID}", '
    '"schema_version": "1.0.0"}'
)

# The live prompt-submitted shape, same capture. Included as the positive
# control: it already crosses today (``prompt_preview`` is capture_shape_only,
# which sets the state), so it proves the floor below is not what makes a
# record admissible -- the contract is.
CAPTURED_PROMPT_SUBMITTED: dict[str, object] = {
    "hook_source": "user_prompt_submit",
    "prompt_length": 42,
    "session_id": _SESSION_UUID,
    "working_directory": "omni_home",
    "prompt_preview": "please read the ledger and report",
}


@pytest.mark.unit
def test_the_captured_tool_executed_record_crosses_the_egress_gate() -> None:
    """The RED case: today this record is stamped ``raw`` and refused."""
    out = redact_capture(dict(CAPTURED_TOOL_EXECUTED), TOOL_TOPIC)
    assert out[GATEWAY_STATE_FIELD] not in GATEWAY_NEVER_ADMITTED, (
        "the gate refuses `raw` structurally, so a `raw` stamp closes this "
        "topic's outbound leg permanently"
    )
    assert _gateway_admits(out)


@pytest.mark.unit
def test_the_captured_prompt_submitted_record_crosses_the_egress_gate() -> None:
    """Positive control: this one already crossed before the floor."""
    out = redact_capture(dict(CAPTURED_PROMPT_SUBMITTED), PROMPT_TOPIC)
    assert _gateway_admits(out)


@pytest.mark.unit
@pytest.mark.parametrize("topic", [TOOL_TOPIC, PROMPT_TOPIC])
def test_raw_is_not_a_reachable_output_of_this_transform(topic: str) -> None:
    """``raw`` means no posture was applied; after this function one always was."""
    for payload in ({}, {"session_id": "s-1"}, dict(CAPTURED_TOOL_EXECUTED)):
        out = redact_capture(dict(payload), topic)
        assert out[GATEWAY_STATE_FIELD] != EnumRedactionState.RAW.value


@pytest.mark.unit
def test_the_floor_does_not_weaken_the_classes() -> None:
    """The OMN-17154 incident shape still carries no plaintext across the boundary.

    A floor that made every record look ``redacted`` while letting a secret
    through would be worse than the bug it replaces, so this is asserted here
    rather than assumed from the OMN-17209 corpus. Under the production
    contract ``command`` and ``tool_output`` are undeclared on this topic, so
    the fail-closed default hashes them before the scrub ever sees them --
    the state is ``redacted`` because the redaction is what happened, and
    that is the state the gate admits.
    """
    out = redact_capture(
        {
            "tool_name": "Bash",
            "command": "valkey-cli -h omninode-valkey config get requirepass",
            "tool_output": '1) "requirepass"\n2) "S3cr3t-Valkey-Pw-2026"',
            "session_id": "s-1",
        },
        TOOL_TOPIC,
    )
    rendered = json.dumps(out)
    assert "S3cr3t-Valkey-Pw-2026" not in rendered
    assert "requirepass" not in rendered
    assert str(out["command"]).startswith("sha256:")
    assert str(out["tool_output"]).startswith("sha256:")
    assert _gateway_admits(out)


@pytest.mark.unit
def test_the_floor_does_not_weaken_the_secret_scrub() -> None:
    """A secret reaching a VERBATIM field still escalates past the floor.

    ``working_directory`` is one of the fields the contract declares
    ``capture_verbatim``, so it is the field where the scrub -- not the
    class -- is the only control. The 2026-08-19 evening incident shape is
    used verbatim.
    """
    out = redact_capture(
        {
            "session_id": "s-1",
            "tool_name": "Bash",
            "working_directory": "postgresql://onex:hunter2@db.internal:5432/onex",
        },
        TOOL_TOPIC,
    )
    rendered = json.dumps(out)
    assert "hunter2" not in rendered
    assert out[GATEWAY_STATE_FIELD] == EnumRedactionState.SECRET_DETECTED.value
    assert _gateway_admits(out)


@pytest.mark.unit
def test_an_unclassified_field_is_still_hashed_under_the_floor() -> None:
    """The fail-closed default is what the state is attesting to; it must still fire."""
    out = redact_capture(
        {"session_id": "s-1", "a_field_nobody_declared": "plaintext-value"},
        TOOL_TOPIC,
    )
    assert out["a_field_nobody_declared"] != "plaintext-value"
    assert str(out["a_field_nobody_declared"]).startswith("sha256:")
