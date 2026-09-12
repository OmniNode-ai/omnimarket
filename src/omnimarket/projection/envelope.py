"""ONEX envelope unwrapping -- matches omnidash TypeScript parseMessage() exactly."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelProjectionEnvelopeMetadata,
)

logger = logging.getLogger(__name__)


def unwrap_envelope(raw_bytes: bytes) -> dict[str, Any] | None:
    """Parse a Kafka message value and unwrap ONEX envelope.

    Replicates the omnidash read-model-consumer.ts parseMessage() logic:
    - { payload: { ... } } -> use payload, attach _envelope
    - { data: { ... } } -> use data, attach _envelope, _event_type, _correlation_id
    - Otherwise use raw parsed object
    """
    try:
        raw = json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    if not isinstance(raw, dict):
        return None

    # Unwrap payload envelope
    if isinstance(raw.get("payload"), dict):
        result = dict(raw["payload"])
        result["_envelope"] = raw
        return result

    # Unwrap data envelope (if data is a dict, not a list)
    data = raw.get("data")
    if isinstance(data, dict):
        result = dict(data)
        result["_envelope"] = raw
        result["_event_type"] = raw.get("event_type")
        result["_correlation_id"] = raw.get("correlation_id")
        return result

    return raw


# The keys ``unwrap_envelope`` above ADDS to the payload it returns, PLUS
# ``_envelope_id`` -- a key the PRODUCER stamps directly onto the wire
# ``payload``/``data`` object before publish (it is not added by
# ``unwrap_envelope`` itself, but it survives that function's payload-copy
# branch untouched, exactly as ``_envelope`` does). None of the four are
# fields any producer intends a payload MODEL to see.
#
# OMN-16831. Every wire model in the delegation family is declared
# ``extra="forbid"``, so handing one of them the dict this module returns is a
# guaranteed ``ValidationError`` -- ``_envelope`` / ``Extra inputs are not
# permitted``. That is not a hypothetical: it rejected 53 of the 82 records on
# ``onex.dlq.omnimarket.projection-delegation-malformed.v1`` between
# 2026-08-26T12:40:01.508Z and 2026-09-07T11:28:53.769Z, including both source
# events of the two terminal delegations that produced zero
# ``delegation_events`` rows on onex-dev. The offset was committed each time, so
# the loss was silent.
#
# OMN-18214. ``_envelope_id`` is the same class of defect, found the same way:
# a fresh ``quality-gate-result.v1`` event DLQ'd on onex-dev staging run
# 34687273545 with ``ValidationError`` naming ``_envelope_id`` as the extra
# field. This is NOT the OMN-16249 seam (``handler_shim.RUNTIME_INJECTED_KEYS``,
# the omnibase_infra runtime auto-wiring's ``handle()`` dispatch, which already
# names ``_envelope_id``) -- ``DelegationProjectionRunner`` is a standalone
# Kafka consumer on the ``unwrap_envelope``/``strip_runner_injected_keys``
# seam, and that allowlist had not been widened for this key.
#
# The keys are still injected/left in place rather than dropped at the source:
# the delegate-skill terminal path reads ``_envelope`` for its
# envelope-timestamp fallback (``model_delegate_skill_terminal_projection
# ._payload_with_envelope_timestamp``), and the LLM-cost backfill reads
# ``_event_type``/``_correlation_id``. Stripping them in ``unwrap_envelope``
# would break those readers. Stripping them at each typed-model construction is
# the seam that is correct for both kinds of consumer.
RUNNER_INJECTED_KEYS: Final[frozenset[str]] = frozenset(
    {"_envelope", "_event_type", "_correlation_id", "_envelope_id"}
)


def strip_runner_injected_keys(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``data`` without the keys :func:`unwrap_envelope` added.

    Call this immediately before constructing an ``extra="forbid"`` wire model
    from a runner-delivered payload. It removes exactly the three keys this
    module injects and nothing else -- an unexpected field that a *producer*
    actually put on the wire still fails validation, which is the behaviour
    ``extra="forbid"`` exists for.
    """
    return {
        key: value for key, value in data.items() if key not in RUNNER_INJECTED_KEYS
    }


def envelope_tenant_identity(data: Mapping[str, Any]) -> str | None:
    """Return the tenant identity the PRODUCER recorded on this event's envelope.

    OMN-17422. ``ModelEventEnvelope.tenant_id`` (omnibase_core) is the canonical
    envelope-side tenant stamp -- the same field
    ``omnibase_infra.shared.tenant_stamp`` and the runtime's
    ``tenant_scoped_ingress`` wiring write, declared there as "the tenant
    DIMENSION -- which tenant this event belongs to, recorded at write time".
    :func:`unwrap_envelope` hands the whole raw wire message back under
    ``_envelope``, so for a payload model that carries no tenant field of its
    own (``ModelQualityGateResult`` is ``extra="forbid"`` and has none) this is
    the ONLY producer-recorded attribution available to a projection writer.

    It has to be read here rather than re-derived, because a writer under
    ``FORCE ROW LEVEL SECURITY`` cannot discover a row's tenant by reading:
    with ``app.tenant_id`` unset the policy predicate is NULL and an
    RLS-covered ``SELECT`` returns zero rows, indistinguishable from an empty
    table. Attribution is producer-recorded or it does not exist (OMN-16831 /
    OMN-17627).

    Returns ``None`` -- never a default, never an invented identity -- when the
    envelope is absent, is not a mapping, or recorded no tenant. The caller
    decides what an unattributed event means for its own table; this function
    only reports what the producer wrote.
    """
    envelope = data.get("_envelope")
    if not isinstance(envelope, Mapping):
        return None
    tenant_id = envelope.get("tenant_id")
    if isinstance(tenant_id, str) and tenant_id.strip():
        return tenant_id.strip()
    return None


def envelope_event_timestamp(data: Mapping[str, Any]) -> datetime | None:
    """Return the event time the PRODUCER recorded on this event's envelope.

    OMN-15583. ``ModelEventEnvelope.envelope_timestamp`` (omnibase_core) is the
    canonical envelope-side event time -- "Envelope creation timestamp (UTC)",
    stamped by the producer at publish. For a payload model that carries no
    time field of its own it is the ONLY authoritative event time a projection
    writer can see, exactly as :func:`envelope_tenant_identity` is the only
    authoritative attribution.

    ``ModelQualityGateResult`` is one such model: ``extra="forbid"`` with no
    ``timestamp`` / ``evaluated_at`` / ``completed_at`` field, so a
    quality-gate-result projection that wants the event time has to read it
    here. The alternative the ``delegation_events`` write path was relying on
    -- omit the column and let the deployed schema default it -- is wrong
    twice: ``DEFAULT NOW()`` records the WRITE time, not the event time, and a
    warm table whose ``timestamp`` column predates migration 0007 has no
    default at all (``ADD COLUMN IF NOT EXISTS`` no-ops on an existing column,
    the OMN-15376 drift class), so the write raises ``null value in column
    "timestamp" ... violates not-null constraint`` (SQLSTATE 23502) instead.

    Returns ``None`` -- never ``now()``, never an invented time -- when the
    envelope is absent, is not a mapping, or recorded no timestamp. The caller
    decides what an un-timed event means for its own table; this function only
    reports what the producer wrote.

    The parse goes through :class:`ModelProjectionEnvelopeMetadata`, the same
    ``extra="ignore"`` typed reader ``model_delegate_skill_terminal_projection
    ._payload_with_envelope_timestamp`` already uses for this exact field, so
    the two envelope-time readers cannot drift on what a valid envelope time is.
    """
    envelope = data.get("_envelope")
    if not isinstance(envelope, Mapping):
        return None
    return ModelProjectionEnvelopeMetadata.model_validate(envelope).envelope_timestamp
