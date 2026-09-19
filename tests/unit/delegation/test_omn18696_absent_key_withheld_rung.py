# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18696 second pass: an ABSENT key is named, not silently skipped.

WHAT THE FIRST PASS LEFT OPEN, AS MEASURED

The first pass gave the local path two typed, non-retryable credential
refusals at the EFFECT boundary: ``credential_rejected`` for a key the provider
turned down, ``credential_absent`` for a declared reference that resolves to
nothing. Both were proven there against a local stub provider driving the real
handler, and both hold.

Only one of them is reachable from an ordinary run. Measured on this Mac
2026-09-19 through the sanctioned CLI wrapper, one command run twice with
nothing changed but the local store:

    key registered (an invalid value)
        ladder climbed to cheap_frontier, escalation_count 1,
        attempts[3].failure_class == "provider_auth_failed",
        credential_refusal.reason == "credential_rejected",
        receipt terminal_failure_cause == "auth_failed", exit 1

    same reference deleted
        three local attempts, escalation_count 0,
        credential_refusal null, no line anywhere naming a credential, exit 1

The asymmetry is the defect and it is structural, not a missing branch. A
backend whose declared credential does not resolve is not ROUTABLE
(``_backend_routable`` -> ``_backend_secret_available``), so the ladder never
selects it, so the effect boundary is never asked and its ``CREDENTIAL_ABSENT``
refusal cannot fire. The customer who has registered NOTHING is told strictly
less than the one who has registered something WRONG.

WHAT THIS CHANGE DOES, AND DELIBERATELY DOES NOT DO

It does not make the unkeyed backend routable. A local-first machine holding no
cloud credentials must keep working on its local tier, and turning a graceful
skip into a hard refusal would break every such run. The skip stays; it stops
being silent. ``credential_withheld_rung`` is a QUERY that asks the SAME
selection what it would have chosen had the credential resolved, and the two
FAILED terminals attach a typed ``CREDENTIAL_ABSENT`` refusal naming that rung.

WHY EVERY NEGATIVE HERE CARRIES A POSITIVE CONTROL

"Reported nothing because nothing was withheld" and "reported nothing because
the query is broken" are the same passing test. Each ``is None`` assertion
below is paired with an input that must produce a rung, so a query stubbed to
return ``None`` fails the pair rather than passing half of it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.models.delegation.credential_withheld_rung import (
    ModelCredentialWithheldRung,
)
from omnimarket.models.delegation.local_credential_refusal import (
    EnumLocalCredentialRefusalReason,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_module,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)

pytestmark = pytest.mark.unit


# The reference the shipped contract declares for the cheap_frontier rung. Named
# as a constant so a repoint of that backend fails these tests loudly instead of
# quietly testing a tier that no longer exists.
OPENROUTER_REF = "llm.openrouter.api_key"
GLM_REF = "llm.glm.api_key"
# Every reference the cheap_cloud tier's backends declare. Withholding one of
# them proves nothing: the tier declares siblings and stays selectable through
# another, which is itself pinned below.
CHEAP_CLOUD_REFS = frozenset({GLM_REF, "llm.gemini.api_key", "llm.vertex.access_token"})


@pytest.fixture
def unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[frozenset[str] | set[str]], None]]:
    """Make a named set of credential references resolve to nothing.

    Patches the single resolution seam the routing authority reads
    (``api_key_ref_available``) rather than the eligibility predicate above it,
    so ``_backend_secret_available`` and ``_backend_routable`` -- the code under
    test -- keep running for real. Every reference NOT named stays resolvable,
    because a fake that refuses everything would also withhold the local tier,
    which declares no credential at all, and the test would then pass while
    describing a ladder nobody has.
    """

    def _apply(missing: frozenset[str] | set[str]) -> None:
        def fake_available(
            ref: str | None, *, env_var_fallback: str | None = None
        ) -> bool:
            if ref is None:
                # A backend declaring no credential is always satisfied. The
                # local rungs are in this branch; getting it wrong would make
                # every assertion below name the wrong tier.
                return True
            return ref not in missing

        monkeypatch.setattr(routing, "api_key_ref_available", fake_available)

    return _apply


def test_the_cheapest_rung_withheld_for_an_absent_key_is_named(
    unresolvable: Callable[[frozenset[str] | set[str]], None],
) -> None:
    """AC: an absent key produces a typed fact naming the rung and the reference.

    This is the case the live 2026-09-19 measurement produced and reported
    nothing for.
    """
    unresolvable({OPENROUTER_REF})

    rung = routing.credential_withheld_rung("document")

    assert rung is not None, (
        "the cheap_frontier rung declares a credential that does not resolve, "
        "so it is withheld and must be named"
    )
    assert isinstance(rung, ModelCredentialWithheldRung)
    assert rung.tier == "cheap_frontier"
    assert rung.credential_ref == OPENROUTER_REF
    assert rung.endpoint_url
    assert rung.model_id


def test_positive_control_nothing_is_withheld_when_every_key_resolves(
    unresolvable: Callable[[frozenset[str] | set[str]], None],
) -> None:
    """The control for the test above: same call, nothing missing, no rung.

    Without this, a query hardcoded to return the cheap_frontier rung passes the
    previous test.
    """
    unresolvable(set())

    assert routing.credential_withheld_rung("document") is None


def test_a_tier_with_a_credentialed_sibling_is_not_withheld(
    unresolvable: Callable[[frozenset[str] | set[str]], None],
) -> None:
    """One missing reference does not withhold a tier that declares siblings.

    ``cheap_cloud`` declares several backends for ``research``. Losing the GLM
    reference alone leaves the tier selectable through another, so nothing is
    withheld and nothing is claimed -- the query reports a TIER the ladder
    cannot reach, never a backend it merely did not pick. Found by this test
    failing against an earlier revision that asserted the opposite.
    """
    unresolvable({GLM_REF})

    assert routing.credential_withheld_rung("research") is None


def test_the_named_rung_follows_the_references_that_are_actually_missing(
    unresolvable: Callable[[frozenset[str] | set[str]], None],
) -> None:
    """Withholding a DIFFERENT tier's references names that tier instead.

    The second control on the query: its answer tracks the input rather than
    being a fixed tier. ``research`` declares no cheap_frontier rung in its
    closed tier_order at all, so this answer cannot collapse onto the previous
    test's by accident.
    """
    unresolvable(CHEAP_CLOUD_REFS)

    rung = routing.credential_withheld_rung("research")

    assert rung is not None
    assert rung.tier == "cheap_cloud"
    assert rung.tier != "cheap_frontier"
    assert rung.credential_ref in CHEAP_CLOUD_REFS


def test_a_rung_declined_for_a_quota_state_is_not_reported_as_a_credential(
    monkeypatch: pytest.MonkeyPatch,
    unresolvable: Callable[[frozenset[str] | set[str]], None],
) -> None:
    """The counterfactual relaxes the credential term ONLY.

    A rung whose provider declared its quota exhausted is unselectable for a
    reason no key fixes. Reporting it here would send a customer to register a
    credential they already hold, so the relaxed probe must keep every other
    eligibility term in force. Every credential resolves in this test, so the
    ONLY way a rung could be reported is if relaxation leaked past its term.
    """
    unresolvable(set())
    monkeypatch.setattr(
        routing,
        "quota_domain_disabled",
        lambda _endpoint_url, now=None: "quota exhausted until tomorrow",  # noqa: ARG005
    )

    assert routing.credential_withheld_rung("document") is None


def test_quota_and_credential_together_are_not_reported_as_a_credential(
    monkeypatch: pytest.MonkeyPatch,
    unresolvable: Callable[[frozenset[str] | set[str]], None],
) -> None:
    """The sharp case: BOTH terms fail, so a key alone would not unlock the rung.

    This is the assertion that makes the design decision load-bearing rather
    than incidental. Relaxing the credential term still leaves the quota term
    refusing, the counterfactual selection still returns nothing, and the rung
    is correctly NOT claimed as a credential problem -- because registering a
    key would not have made it usable.
    """
    unresolvable({OPENROUTER_REF, GLM_REF})
    monkeypatch.setattr(
        routing,
        "quota_domain_disabled",
        lambda _endpoint_url, now=None: "quota exhausted until tomorrow",  # noqa: ARG005
    )

    assert routing.credential_withheld_rung("document") is None


def test_an_unknown_task_class_reports_nothing_rather_than_guessing(
    unresolvable: Callable[[frozenset[str] | set[str]], None],
) -> None:
    """No declared tier_order means no declared ladder to report a hole in."""
    unresolvable({OPENROUTER_REF})

    assert routing.credential_withheld_rung("omn18696-no-such-task-class") is None


def test_the_refusal_payload_names_the_reference_and_carries_no_value(
    unresolvable: Callable[[frozenset[str] | set[str]], None],
) -> None:
    """The port turns the withheld rung into the SAME typed refusal shape.

    Distinctness is the point of AC2: this payload must read
    ``credential_absent`` and map to ``provider_credential_missing``, never the
    ``credential_rejected`` / ``provider_auth_failed`` pair the first pass
    already returns for a key the provider turned down.
    """
    unresolvable({OPENROUTER_REF})
    correlation = uuid.uuid4()

    payload = port_module.LocalDelegationDispatchPort()._withheld_credential_refusal(
        task_type="document",
        correlation_id=correlation,
    )

    assert "credential_refusal" in payload
    refusal = payload["credential_refusal"]
    assert isinstance(refusal, dict)
    assert refusal["reason"] == EnumLocalCredentialRefusalReason.CREDENTIAL_ABSENT.value
    assert refusal["reason"] != (
        EnumLocalCredentialRefusalReason.CREDENTIAL_REJECTED.value
    )
    assert refusal["credential_ref"] == OPENROUTER_REF
    assert refusal["correlation_id"] == str(correlation)
    # The reference NAME is carried; a value never is. There is no value to
    # leak in this test by construction -- the point of asserting it is that a
    # later field addition that carried one fails here.
    serialized = repr(refusal)
    assert "sk-" not in serialized


def test_the_refusal_maps_to_the_credential_missing_failure_class(
    unresolvable: Callable[[frozenset[str] | set[str]], None],
) -> None:
    """The reason's class is the non-retryable one the first pass introduced."""
    unresolvable({OPENROUTER_REF})

    rung = routing.credential_withheld_rung("document")
    assert rung is not None

    from omnimarket.models.delegation.local_credential_refusal import (
        ModelLocalCredentialRefusal,
    )

    refusal = ModelLocalCredentialRefusal(
        reason=EnumLocalCredentialRefusalReason.CREDENTIAL_ABSENT,
        credential_ref=rung.credential_ref,
        backend_ref=rung.endpoint_url,
        model_id=rung.model_id,
        correlation_id=str(uuid.uuid4()),
    )
    assert (
        refusal.failure_class is EnumDelegationFailureClass.PROVIDER_CREDENTIAL_MISSING
    )
    assert refusal.retryable is False


def test_positive_control_the_port_omits_the_key_when_nothing_is_withheld(
    unresolvable: Callable[[frozenset[str] | set[str]], None],
) -> None:
    """Absence of the key is the signal, so it must be ABSENT, not null.

    Paired control for the payload test above: the same call on a healthy
    ladder returns an empty mapping, so ``**`` splices nothing into the
    terminal and every non-credential terminal is byte-identical to before.
    """
    unresolvable(set())

    payload = port_module.LocalDelegationDispatchPort()._withheld_credential_refusal(
        task_type="document",
        correlation_id=uuid.uuid4(),
    )

    assert payload == {}
    assert "credential_refusal" not in payload
