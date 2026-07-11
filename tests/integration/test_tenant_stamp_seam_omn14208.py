# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-boundary seam test for the OMN-14208 tenant-stamp -> consumer boundary.

OMN-14360 (OMN-14208 Path A). The OMN-14208 near-miss was two individually-green
PRs that were a silent 100% runtime no-op: an infra-side producer and a
market-side consumer whose seams were never driven by a single test. This test
drives the REAL cross-repo boundary — the exact ``payload["tenant_id"]`` shape
an infra tenant-stamp emits, validated against the actual market-side consumer
model ``ModelDelegateSkillRequest`` — so a seam mismatch fails loudly here
instead of silently at runtime.

It deliberately does NOT use ``_FakeDispatchEngine`` or two independent unit
suites. It reconstructs each producer's EXACT emitted payload shape (the infra
stamp symbols are not importable here: they live on unreleased infra branches
#2254/#2252, and infra->market is a forbidden dependency direction, so the
consumer repo owns this test) and feeds it to the real
``ModelDelegateSkillRequest.model_validate`` under its production
``ConfigDict(frozen=True, extra="forbid")``.

Two producers write ``payload["tenant_id"]`` today, and they DISAGREE:

  * #2254 auto-wiring stamp (handler_wiring._stamp_tenant_id_from_topic_prefix):
    emits ``{**payload, "tenant_id": <slug>}`` — a SLUG string ("acme").
  * #2252 gateway stamp (handler_consume_inbound.consume_inbound):
    emits ``{**payload, "tenant_id": str(identity.tenant_id), "tenant_slug":
    identity.tenant_slug}`` — where ``identity.tenant_id`` is a UUID and the
    slug lives in a SEPARATE ``tenant_slug`` key.

The consumer (``ModelDelegateSkillRequest.tenant_id: str | None``) documents its
intent inline: "a named tenant identifier (slug), NOT a UUID." So the #2254
shape is seam-matched and the #2252 shape is doubly wrong (extra ``tenant_slug``
key under ``extra="forbid"`` + UUID-vs-slug semantic collision). The canonical
wire shape + tenant_id semantics are unresolved; reconciliation is tracked by
OMN-14367. This test PROVES the #2254 seam is closed and GATES the #2252 gap so
it cannot regress into a silent no-op.
"""

from __future__ import annotations

import re
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)

# SYNC with the canonical infra runtime stamp regex
# ``omnibase_infra.runtime.auto_wiring.handler_wiring._TENANT_WIRE_PREFIX_RE``
# (OMN-14349). Reconstructed here (not imported) because the symbol lives on an
# unreleased infra branch and infra->market is a forbidden dependency edge.
_TENANT_WIRE_PREFIX_RE = re.compile(r"^tenant-([a-z][a-z0-9-]{1,61}[a-z0-9])\.")

_PREFIXED_TOPIC = "tenant-acme.onex.cmd.omnimarket.delegate-skill.v1"
_BARE_TOPIC = "onex.cmd.omnimarket.delegate-skill.v1"


def _raw_delegate_payload() -> dict[str, object]:
    """A minimal valid delegate-skill request payload, pre-stamp."""
    return {
        "prompt": "summarize the changelog",
        "task_type": "summarization",
        "source": "claude-code",
    }


def _stamp_2254(topic: str, payload: dict[str, object]) -> dict[str, object]:
    """Reconstruct the #2254 auto-wiring stamp emitted shape EXACTLY.

    SYNC: ``handler_wiring._stamp_tenant_id_from_topic_prefix``. Overwrites
    ``payload["tenant_id"]`` with the slug from a ``tenant-<slug>.`` wire prefix;
    a topic with no matching prefix is left completely untouched.
    """
    match = _TENANT_WIRE_PREFIX_RE.match(topic)
    if match is None:
        return dict(payload)
    return {**payload, "tenant_id": match.group(1)}


def _stamp_2252_gateway(
    payload: dict[str, object], tenant_uuid: UUID, tenant_slug: str
) -> dict[str, object]:
    """Reconstruct the #2252 gateway consume_inbound emitted shape EXACTLY.

    SYNC: ``handler_consume_inbound.consume_inbound`` ``verified_payload``.
    Stamps BOTH ``tenant_id`` (the canonical UUID, stringified) AND a separate
    ``tenant_slug`` (the slug), overwriting any payload-supplied values.
    """
    return {
        **payload,
        "tenant_id": str(tenant_uuid),
        "tenant_slug": tenant_slug,
    }


# ---------------------------------------------------------------------------
# #2254 seam — CLOSED (the OMN-14208 proof)
# ---------------------------------------------------------------------------


def test_2254_stamp_shape_validates_against_consumer() -> None:
    """The #2254 stamp's emitted payload validates and yields the slug.

    This is the load-bearing OMN-14208 proof: the infra producer and the market
    consumer are seam-matched field-by-field. ``model_validate`` must NOT raise
    under ``extra="forbid"`` (a consumer missing the field would make
    ``tenant_id`` an extra key), and the verified slug must survive.
    """
    stamped = _stamp_2254(_PREFIXED_TOPIC, _raw_delegate_payload())
    req = ModelDelegateSkillRequest.model_validate(stamped)
    assert req.tenant_id == "acme"


def test_bare_topic_leaves_tenant_id_none() -> None:
    """A bare (unprefixed) topic is left unstamped — tenant_id stays None."""
    stamped = _stamp_2254(_BARE_TOPIC, _raw_delegate_payload())
    assert "tenant_id" not in stamped
    req = ModelDelegateSkillRequest.model_validate(stamped)
    assert req.tenant_id is None


def test_2254_stamp_overwrites_client_supplied_tenant_id() -> None:
    """The config-bound stamp always wins over a self-reported tenant_id."""
    raw = _raw_delegate_payload()
    raw["tenant_id"] = "attacker-tenant"
    stamped = _stamp_2254(_PREFIXED_TOPIC, raw)
    req = ModelDelegateSkillRequest.model_validate(stamped)
    assert req.tenant_id == "acme"


# ---------------------------------------------------------------------------
# #2254 vs #2252 — semantic divergence (deterministic evidence)
# ---------------------------------------------------------------------------


def test_producers_disagree_on_tenant_id_meaning() -> None:
    """Pin the semantic collision the reconciliation ticket (OMN-14367) settles.

    Both producers write ``payload["tenant_id"]`` but with different KINDS of
    value for the same tenant: #2254 writes the slug, #2252 writes the canonical
    UUID (with the slug relegated to a separate ``tenant_slug`` key). This is a
    worse mismatch than the extra field — widening the consumer to accept
    ``tenant_slug`` would silently mask it.
    """
    raw = _raw_delegate_payload()
    tenant_uuid = uuid4()

    shape_2254 = _stamp_2254(_PREFIXED_TOPIC, raw)
    shape_2252 = _stamp_2252_gateway(raw, tenant_uuid, "acme")

    assert shape_2254["tenant_id"] == "acme"  # slug
    assert shape_2252["tenant_id"] == str(tenant_uuid)  # UUID string — different kind
    assert shape_2252["tenant_id"] != shape_2254["tenant_id"]
    assert shape_2252["tenant_slug"] == "acme"  # slug lives here in #2252
    assert "tenant_slug" not in shape_2254  # #2254 emits no tenant_slug


# ---------------------------------------------------------------------------
# #2252 seam — KNOWN GAP (gated, not silently accepted)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=ValidationError,
    reason=(
        "OMN-14367: #2252 gateway stamp emits tenant_id=UUID + extra tenant_slug; "
        "consumer+#2254 mean slug. Trips green when the canonical shape is "
        "reconciled."
    ),
)
def test_2252_gateway_shape_currently_rejected() -> None:
    """The #2252 gateway shape does not (yet) satisfy the consumer contract.

    No extra assertions in the body: the ValidationError itself is the expected
    (xfail) outcome, so nothing here can mask a semantic regression. When the
    canonical shape is reconciled and ``model_validate`` stops raising, this
    xpasses and strict-xfail fails the run — the signal to update the test with
    the reconciled expectations.
    """
    stamped = _stamp_2252_gateway(_raw_delegate_payload(), uuid4(), "acme")
    ModelDelegateSkillRequest.model_validate(stamped)
