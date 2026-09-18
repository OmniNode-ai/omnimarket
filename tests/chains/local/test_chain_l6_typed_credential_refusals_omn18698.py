# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""L6 chain pair: a wrong or absent key is a typed refusal (OMN-18698).

Row L6 of the local-path MVP (`beta/GOAL.md`, ticket OMN-18696): *wrong key
and absent key produce typed refusals on the local path*.

The pair
--------
``test_golden_chain_...``
    A resolvable key completes, and the terminal carries NO credential
    refusal. This is the control the three error chains below are read
    against: without it, "a refusal was attached" would be satisfied by a rig
    that could never succeed at all.

``test_error_chain_rejected_key_...`` / ``test_error_chain_absent_value_...``
    The two credential conditions. Each must surface a typed refusal naming
    the reference, the backend and the remediation, and each must terminate
    the ladder rather than climb it.

``test_error_chain_outage_...``
    The positive control for the two above, and the third of OMN-18696's
    three codes. An unreachable provider is NOT a credential fact: it carries
    no refusal, it stays retryable, and it DOES climb. Without this test, a
    classifier broken to call everything a credential refusal -- or a ladder
    broken to climb on nothing -- would leave the two "did not climb"
    assertions green.

Why there are two rungs
-----------------------
"The ladder did not climb" is only a claim if a climb was available. Every
test here configures a second, keyless rung at ``cheap_cloud``, the next tier
in ``code_generation``'s declared order, behind its own provider. The
credential cases must leave that second provider untouched; the outage case
must reach it.

Falsifier (OMN-18698 AC2): removing ``PROVIDER_CREDENTIAL_MISSING`` from the
port's non-retryable set turns the absent-value chain red at its
``attempts == 1`` assertion; collapsing the 401 classification back into
``MODEL_UNAVAILABLE`` turns the rejected-key chain red at both its failure
class and its no-climb assertion.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.inference.local_byok_credential_adapter import (
    LOCAL_CREDENTIAL_TABLE,
    register_local_byok_credential,
)
from omnimarket.models.delegation.local_credential_refusal import (
    EnumLocalCredentialRefusalReason,
    ModelLocalCredentialRefusal,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)
from omnimarket.routing import delegation_backend_resolution
from omnimarket.routing.routing_tiers_path import resolve_routing_tiers_path
from tests.chains.local.harness import (
    BYOK_BACKEND_ID,
    PROVIDER_SLUG,
    TASK_TYPE,
    LocalProviderStub,
    house_openrouter_rung,
    local_byok_catalogue,
    no_ambient_provider_credentials,
    run_local_delegation,
    use_local_store,
)

pytestmark = [pytest.mark.unit, pytest.mark.local_chain]

_CUSTOMER_KEY_VALUE = "sk-or-omn18698-l6-customer-value"

#: The model every stand-in next rung serves.
_NEXT_RUNG_MODEL_ID = "omn18698-keyless-model"

#: The tier ``code_generation`` escalates to from ``cheap_frontier``. Its
#: declared order is local -> cheap_frontier -> cheap_cloud -> claude.
_NEXT_TIER = "cheap_cloud"


def declared_next_tier_backend_ids() -> tuple[str, ...]:
    """Backend ids ``routing_tiers.yaml`` declares at the next tier for this task.

    Read from the same file the routing authority reads, rather than typed
    out here, because the escalation step resolves the NEXT backend by the id
    the tier declares -- not from whatever the test happened to inject. A rung
    under any other id is unresolvable and the ladder exhausts instead of
    climbing, which would silently disarm the positive control below.
    """
    document = yaml.safe_load(resolve_routing_tiers_path().read_text())
    ids: list[str] = []
    for tier in document["tiers"]:
        if tier["name"] != _NEXT_TIER:
            continue
        for model in tier.get("models", []):
            if TASK_TYPE in (model.get("use_for") or []):
                ids.append(str(model["backend_id"]))
    assert ids, (
        f"{_NEXT_TIER!r} declares no backend serving {TASK_TYPE!r}; the "
        "positive control below cannot distinguish a ladder that refused "
        "from one that had nowhere to climb"
    )
    return tuple(ids)


@pytest.fixture
def next_rung_provider() -> Iterator[LocalProviderStub]:
    """The provider behind every second rung. Untouched unless the ladder climbs."""
    stub = LocalProviderStub(
        model_id=_NEXT_RUNG_MODEL_ID, content="print('from the next rung')\n"
    )
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


def _two_rung_ladder(
    monkeypatch: pytest.MonkeyPatch,
    *,
    first_rung: dict[str, Any],
    next_rung_url: str,
) -> None:
    """Install ``first_rung`` at ``cheap_frontier`` and keyless rungs above it.

    One stand-in rung per backend id the next tier declares, all behind the
    same provider, so the control holds whichever one the declaration-order
    tiebreak prefers today.
    """
    rungs: list[dict[str, Any]] = [deepcopy(first_rung)]
    for backend_id in declared_next_tier_backend_ids():
        rungs.append(
            {
                "backend_id": backend_id,
                "provider": "omn18698-stand-in",
                "endpoint_url": next_rung_url,
                "model_name": _NEXT_RUNG_MODEL_ID,
                "tier": _NEXT_TIER,
                "timeout_ms": 30000,
                "max_tokens": 4096,
                "capabilities": [TASK_TYPE, "test"],
            }
        )
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: deepcopy(rungs),
    )


def _empty_the_stored_value(db_path: Path, customer_ref: str) -> None:
    """Leave the reference registered and remove the value behind it."""
    conn = sqlite3.connect(str(db_path))
    try:
        updated = conn.execute(
            f"UPDATE {LOCAL_CREDENTIAL_TABLE} SET secret_value = '' "
            "WHERE secret_ref = ?",
            (customer_ref,),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    assert updated == 1


def _assert_refusal_names_what_the_customer_needs(
    response: ModelDelegateSkillResponse,
    *,
    reason: EnumLocalCredentialRefusalReason,
    credential_ref: str,
    backend_ref: str,
) -> ModelLocalCredentialRefusal:
    """Every refusal names the reference, the backend and the action."""
    refusal = response.credential_refusal
    assert refusal is not None, (
        "the terminal must carry the typed refusal, not only prose"
    )
    assert refusal.reason is reason
    assert refusal.credential_ref == credential_ref
    assert refusal.backend_ref == backend_ref
    assert refusal.remediation
    assert refusal.remediation in refusal.message
    assert credential_ref in refusal.message
    assert refusal.retryable is False
    return refusal


# --------------------------------------------------------------------------
# Golden
# --------------------------------------------------------------------------


async def test_golden_chain_a_resolvable_key_completes_with_no_refusal(
    provider_stub: LocalProviderStub,
    next_rung_provider: LocalProviderStub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The control: this rig can succeed, so a refusal below means something."""
    with no_ambient_provider_credentials(monkeypatch):
        db_path = use_local_store(monkeypatch, tmp_path)
        local_byok_catalogue(monkeypatch, tmp_path, provider_stub.completions_url)
        _two_rung_ladder(
            monkeypatch,
            first_rung=house_openrouter_rung(monkeypatch),
            next_rung_url=next_rung_provider.completions_url,
        )
        register_local_byok_credential(
            PROVIDER_SLUG, _CUSTOMER_KEY_VALUE, db_path=db_path
        )

        response = await run_local_delegation(
            prompt="write a function that parses a semver string",
            db_path=db_path,
            correlation_id=uuid4(),
        )

    assert response.status == "completed", response.error_message
    assert response.credential_refusal is None
    assert response.attempts[0].backend_id == BYOK_BACKEND_ID
    assert next_rung_provider.authorizations == [], (
        "the first rung answered; nothing should have climbed"
    )


# --------------------------------------------------------------------------
# Error chains
# --------------------------------------------------------------------------


async def test_error_chain_a_rejected_key_is_typed_and_does_not_climb(
    provider_stub: LocalProviderStub,
    next_rung_provider: LocalProviderStub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """HTTP 401 is a credential rejection, terminal, not an availability blip."""
    provider_stub.completion_status = 401
    with no_ambient_provider_credentials(monkeypatch):
        db_path = use_local_store(monkeypatch, tmp_path)
        local_byok_catalogue(monkeypatch, tmp_path, provider_stub.completions_url)
        _two_rung_ladder(
            monkeypatch,
            first_rung=house_openrouter_rung(monkeypatch),
            next_rung_url=next_rung_provider.completions_url,
        )
        customer_ref = register_local_byok_credential(
            PROVIDER_SLUG, _CUSTOMER_KEY_VALUE, db_path=db_path
        )

        response = await run_local_delegation(
            prompt="write a function that parses a semver string",
            db_path=db_path,
            correlation_id=uuid4(),
        )

    assert response.status == "failed"
    refusal = _assert_refusal_names_what_the_customer_needs(
        response,
        reason=EnumLocalCredentialRefusalReason.CREDENTIAL_REJECTED,
        credential_ref=customer_ref,
        backend_ref=provider_stub.completions_url,
    )
    # The provider's own words reach the customer, attributed to the provider.
    assert "Incorrect API key provided." in refusal.message

    # The value WAS presented -- that is what distinguishes a rejection from an
    # absence -- and it was the customer's.
    assert provider_stub.presented_credential == _CUSTOMER_KEY_VALUE

    assert response.attempts[-1].failure_class == (
        EnumDelegationFailureClass.PROVIDER_AUTH_FAILED.value
    )
    assert next_rung_provider.authorizations == [], (
        "a rejected credential is not a capacity problem; climbing would spend "
        "another rung on a fact that cannot change on re-ask"
    )


async def test_error_chain_an_absent_value_is_typed_and_does_not_climb(
    provider_stub: LocalProviderStub,
    next_rung_provider: LocalProviderStub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A declared reference with no value refuses before any call is made."""
    with no_ambient_provider_credentials(monkeypatch):
        db_path = use_local_store(monkeypatch, tmp_path)
        local_byok_catalogue(monkeypatch, tmp_path, provider_stub.completions_url)
        _two_rung_ladder(
            monkeypatch,
            first_rung=house_openrouter_rung(monkeypatch),
            next_rung_url=next_rung_provider.completions_url,
        )
        customer_ref = register_local_byok_credential(
            PROVIDER_SLUG, _CUSTOMER_KEY_VALUE, db_path=db_path
        )
        _empty_the_stored_value(db_path, customer_ref)

        response = await run_local_delegation(
            prompt="write a function that parses a semver string",
            db_path=db_path,
            correlation_id=uuid4(),
        )

    assert response.status == "failed"
    _assert_refusal_names_what_the_customer_needs(
        response,
        reason=EnumLocalCredentialRefusalReason.CREDENTIAL_ABSENT,
        credential_ref=customer_ref,
        backend_ref=provider_stub.completions_url,
    )
    assert response.attempts[-1].failure_class == (
        EnumDelegationFailureClass.PROVIDER_CREDENTIAL_MISSING.value
    )
    assert provider_stub.authorizations == [], (
        "no call may be made at all: there was no value to present"
    )
    assert next_rung_provider.authorizations == []


async def test_error_chain_a_provider_outage_is_not_a_credential_refusal(
    next_rung_provider: LocalProviderStub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The positive control, and OMN-18696's third code.

    An unreachable provider carries no credential refusal, stays retryable,
    and climbs to the next rung. Every "did not climb" assertion above is
    only a claim because this one passes.
    """
    with no_ambient_provider_credentials(monkeypatch):
        db_path = use_local_store(monkeypatch, tmp_path)
        # Port 1 on the loopback: nothing listens, and binding it needs root,
        # so it is unreachable by construction rather than by a race.
        dead_url = "http://127.0.0.1:1/v1/chat/completions"
        local_byok_catalogue(monkeypatch, tmp_path, dead_url)
        _two_rung_ladder(
            monkeypatch,
            first_rung=house_openrouter_rung(monkeypatch),
            next_rung_url=next_rung_provider.completions_url,
        )
        register_local_byok_credential(
            PROVIDER_SLUG, _CUSTOMER_KEY_VALUE, db_path=db_path
        )

        response = await run_local_delegation(
            prompt="write a function that parses a semver string",
            db_path=db_path,
            correlation_id=uuid4(),
        )

    assert response.credential_refusal is None, (
        "an unreachable host says nothing about the credential"
    )
    assert next_rung_provider.authorizations != [], (
        "a retryable failure must climb; if it does not, the no-climb "
        "assertions in the two credential chains prove nothing"
    )
    assert response.escalation_count >= 1
