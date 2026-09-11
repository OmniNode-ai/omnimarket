# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Client-side read models for the gateway's workflow endpoints (OMN-16967).

These mirror the gateway's own response models (``onex-api``
``models/model_workflow_envelope.py`` and ``models/model_workflow_receipt.py``)
field for field. They are re-declared rather than imported because the gateway
service is not a package this repo depends on, and a customer CLI must not
require the server's source tree to parse the server's answers.

``extra="ignore"``, deliberately, and the ONE place this repo's default
``extra="forbid"`` is the wrong choice. These are RESPONSE models for a client
that ships to customer laptops and is upgraded on the customer's schedule. With
``forbid``, the next additive field on the server would break every installed
copy of the CLI at once — a server-side improvement becoming a client-side
outage. Requests keep ``forbid`` on the server, which is where strictness
protects something (the gateway refusing a caller-supplied routing field).

Absent-vs-null is preserved rather than smoothed over: ``result_content`` is
genuinely nullable (a terminal shape that carried no content), and the CLI
reports "no content" as a distinct outcome from "content was empty".
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict

__all__ = [
    "ModelCloudDelegationAck",
    "ModelCloudDelegationReceipt",
    "ModelCloudDelegationStatus",
]

_RESPONSE_CONFIG = ConfigDict(frozen=True, extra="ignore", from_attributes=True)


class ModelCloudDelegationAck(BaseModel):
    """``POST /v1/workflows`` -> 202. The workflow now exists."""

    model_config = _RESPONSE_CONFIG

    workflow_id: uuid.UUID
    envelope_id: uuid.UUID
    correlation_id: uuid.UUID
    workflow_type: str
    status: str


class ModelCloudDelegationStatus(BaseModel):
    """``GET /v1/workflows/{id}/status`` — metadata only, by design.

    The generated output is NOT on this response; retrieving it requires the
    receipt call. That asymmetry is the gateway's, and it is preserved here
    rather than papered over, because it is why the CLI always fetches a
    receipt instead of stopping at a ``completed`` status.
    """

    model_config = _RESPONSE_CONFIG

    workflow_id: uuid.UUID
    workflow_type: str
    status: str
    envelope_id: uuid.UUID
    correlation_id: uuid.UUID
    command_topic: str
    submitted_at: dt.datetime
    updated_at: dt.datetime
    terminal_model_used: str | None = None
    terminal_total_tokens: int | None = None
    terminal_latency_ms: int | None = None
    # OMN-18196: the run's provenance, as the gateway renders it. See the
    # receipt below for the full note; the same defaulting discipline applies
    # here, and for the same reason.
    route: str | None = None
    provider: str | None = None
    credential_source: str | None = None
    # OMN-17372: WHY a `failed` status failed. Before these, a tenant with no
    # registered provider key -- refused by name, with a code, before any
    # provider was contacted -- and a tenant hitting a provider outage got
    # byte-identical answers here. `terminal_failure_code` is the field to
    # route on; it is a stable server contract. All four default to None so an
    # older gateway, or a success, parses unchanged.
    terminal_failure_class: str | None = None
    terminal_failure_code: str | None = None
    terminal_failure_reason: str | None = None
    terminal_remediation: str | None = None


class ModelCloudDelegationReceipt(BaseModel):
    """``GET /v1/workflows/{id}/receipt`` — the signed, terminal record.

    Carries both the customer's work product (``result_content``) and the hash
    chain that makes the run auditable. The CLI writes this to disk verbatim:
    a receipt paraphrased by a client is not a receipt.
    """

    model_config = _RESPONSE_CONFIG

    workflow_id: uuid.UUID
    tenant_id: uuid.UUID
    correlation_id: uuid.UUID
    workflow_type: str
    status: str
    submitted_at: dt.datetime
    completed_at: dt.datetime
    terminal_model_used: str
    terminal_total_tokens: int
    terminal_latency_ms: int
    result_content: str | None
    # OMN-18196 / OMN-18079: the provenance of the call that answered.
    #
    # ``credential_source`` is the one fact that distinguishes a run on the
    # customer's OWN registered provider key from one served by a house
    # credential: ``customer_key``, ``house``, or ``none`` for a call that ran
    # with no credential attached. Axiom 9 forbids a customer route binding a
    # house credential, and a model name cannot witness that -- the same model
    # is reachable on both -- so the gateway stamps this from the resolution
    # the effect boundary actually performed.
    #
    # Typed as ``str`` rather than an enum ON PURPOSE, here and only here. This
    # is a client read model for a CLI installed on customer laptops and
    # upgraded on the customer's schedule. A closed enum would make the next
    # value the server learns to emit a parse failure on every installed copy
    # at once, which is the same server-improvement-becomes-client-outage this
    # module's ``extra="ignore"`` exists to prevent. Callers compare against
    # the three known strings and treat anything else as unrecognised.
    #
    # All three default to None so a receipt from a gateway that predates them
    # still parses: a client that refuses to read an older server's receipt
    # turns a missing explanation into no receipt at all. Same discipline as
    # the four ``terminal_failure_*`` fields below.
    route: str | None = None
    provider: str | None = None
    credential_source: str | None = None
    # OMN-17372, same four as on the status above. Defaulted rather than
    # required: a receipt fetched from a gateway that predates them must still
    # parse -- a client that refuses to read an older server's receipt turns a
    # missing explanation into no receipt at all.
    terminal_failure_class: str | None = None
    terminal_failure_code: str | None = None
    terminal_failure_reason: str | None = None
    terminal_remediation: str | None = None
    event_count: int
    projection_row_hash: str
    terminal_event_hash: str
    verifier: str
