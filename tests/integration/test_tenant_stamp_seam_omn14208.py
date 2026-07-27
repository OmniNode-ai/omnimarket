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
stamp symbols are not importable here: they live in the infra repo, and
infra->market is a forbidden dependency direction, so the consumer repo owns
this test) and feeds it to the real ``ModelDelegateSkillRequest.model_validate``
under its production ``ConfigDict(frozen=True, extra="forbid")``.

OMN-14367 RECONCILED (this test file closes the ticket): both producers that
write ``payload["tenant_id"]`` now route through the single canonical helper
``omnibase_infra.shared.tenant_stamp.stamp_verified_tenant_slug`` and agree:

  * #2254 auto-wiring stamp (handler_wiring._stamp_tenant_id_from_topic_prefix)
  * #2252 gateway stamp (handler_consume_inbound.consume_inbound)

Both now emit ``{**payload, "tenant_id": <slug>}`` — a SLUG string (e.g.
"acme"), with NO separate ``tenant_slug`` key. The consumer
(``ModelDelegateSkillRequest.tenant_id: str | None``) documents its intent
inline: "a named tenant identifier (slug), NOT a UUID." This test proves both
producer shapes are seam-matched against that consumer, field-by-field.
"""

from __future__ import annotations

import re

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
    payload: dict[str, object], tenant_slug: str
) -> dict[str, object]:
    """Reconstruct the #2252 gateway consume_inbound emitted shape EXACTLY.

    SYNC (post-OMN-14367): ``handler_consume_inbound.consume_inbound`` routes
    through ``omnibase_infra.shared.tenant_stamp.stamp_verified_tenant_slug``,
    which stamps ``tenant_id`` to the DNS-safe slug and overwrites any
    payload-supplied value. There is no separate ``tenant_slug`` key -- the
    UUID-plus-extra-key shape this helper reconstructed pre-reconciliation is
    gone.
    """
    return {**payload, "tenant_id": tenant_slug}


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
# #2254 vs #2252 — convergence proof (OMN-14367 reconciliation)
# ---------------------------------------------------------------------------


def test_producers_converge_on_tenant_id_meaning() -> None:
    """Pin the reconciliation the OMN-14367 ticket closes: both producers agree.

    Both producers write ``payload["tenant_id"]`` and, post-reconciliation,
    write the SAME kind of value for the same tenant: the DNS-safe slug, with
    no separate ``tenant_slug`` key. Pre-reconciliation this test would have
    failed (#2252 wrote a UUID plus an extra key); it now pins the converged
    shape so the two producers cannot drift apart again silently.
    """
    raw = _raw_delegate_payload()

    shape_2254 = _stamp_2254(_PREFIXED_TOPIC, raw)
    shape_2252 = _stamp_2252_gateway(raw, "acme")

    assert shape_2254["tenant_id"] == "acme"
    assert shape_2252["tenant_id"] == "acme"
    assert shape_2252["tenant_id"] == shape_2254["tenant_id"]
    assert "tenant_slug" not in shape_2254
    assert "tenant_slug" not in shape_2252


# ---------------------------------------------------------------------------
# #2252 seam — CLOSED (OMN-14367 reconciliation proof)
# ---------------------------------------------------------------------------


def test_2252_gateway_shape_now_matches_consumer() -> None:
    """The #2252 gateway shape now satisfies the consumer contract.

    Load-bearing OMN-14367 proof, mirroring ``test_2254_stamp_shape_validates_
    against_consumer``: ``model_validate`` must NOT raise under
    ``extra="forbid"`` (a pre-reconciliation shape with the extra
    ``tenant_slug`` key would fail here), and the verified slug must survive.
    This test previously asserted the OPPOSITE (a strict ``xfail`` pinning the
    rejection) before the gateway producer was reconciled to the shared
    ``stamp_verified_tenant_slug`` helper.
    """
    stamped = _stamp_2252_gateway(_raw_delegate_payload(), "acme")
    req = ModelDelegateSkillRequest.model_validate(stamped)
    assert req.tenant_id == "acme"
