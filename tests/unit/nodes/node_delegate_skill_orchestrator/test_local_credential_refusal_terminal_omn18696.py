# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18696: a credential refusal terminates the ladder and reaches the caller.

The unit suite beside this one
(``tests/unit/delegation/test_omn18696_local_typed_credential_refusals.py``)
binds the effect boundary: a live local provider answering 401, and a declared
credential reference that resolves to nothing, each classified into its own
code. This file binds the two steps after that -- that the local dispatch
ladder REFUSES on those codes instead of climbing past them, and that the typed
payload survives onto the terminal the caller reads.

Both halves were live defects. The absent case classified as ``UNKNOWN`` and
the rejected case as ``MODEL_UNAVAILABLE``, and both of those classes are in
the retryable set, so the ladder walked every configured tier before reporting
a failure that named no credential. AC1's falsifier names that shape exactly:
"a generic error, a retry loop, or a silent fallback to any other route".

Every assertion that a tier was NOT reached carries the retryable case beside
it as a positive control, because a ladder that escalates on nothing and a
ladder that refuses correctly produce the same single-element call list.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.models.delegation.local_credential_refusal import (
    EnumLocalCredentialRefusalReason,
    ModelLocalCredentialRefusal,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)

from .test_local_dispatch_escalation_omn13849 import _dispatch, _install_ladder

pytestmark = pytest.mark.unit

_CREDENTIAL_REF = "llm.omn18696nosuchprovider.api_key"


def _refusing_effect(reason: EnumLocalCredentialRefusalReason, calls: list[str]):
    """An effect that refuses on a credential, recording every tier it is asked."""

    def effect(
        request: ModelLlmDelegationCallRequest,
    ) -> ModelLlmDelegationCallResult:
        calls.append(request.model_tier)
        refusal = ModelLocalCredentialRefusal(
            reason=reason,
            credential_ref=_CREDENTIAL_REF,
            credential_env="OMN18696_EXAMPLE_ENV",
            backend_ref=request.endpoint_ref,
            model_id=request.model_id,
            correlation_id=request.correlation_id,
            detail="the refusing party's own words",
        )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=False,
            failure_class=refusal.failure_class,
            error_message=refusal.message,
            credential_refusal=refusal,
        )

    return effect


@pytest.mark.parametrize(
    ("reason", "expected_class"),
    [
        (
            EnumLocalCredentialRefusalReason.CREDENTIAL_ABSENT,
            EnumDelegationFailureClass.PROVIDER_CREDENTIAL_MISSING,
        ),
        (
            EnumLocalCredentialRefusalReason.CREDENTIAL_REJECTED,
            EnumDelegationFailureClass.PROVIDER_AUTH_FAILED,
        ),
    ],
)
def test_a_credential_refusal_terminates_the_ladder_at_the_first_tier(
    reason: EnumLocalCredentialRefusalReason,
    expected_class: EnumDelegationFailureClass,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither credential class may climb. Both used to climb the whole ladder."""
    _install_ladder(monkeypatch, max_escalations=2)
    calls: list[str] = []

    port = LocalDelegationDispatchPort(
        effect_handler=_refusing_effect(reason, calls),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, task_type="research", correlation_id=uuid4())

    assert calls == ["local"], f"the ladder climbed past a credential refusal: {calls}"
    assert result["status"] == "failed"
    assert result["escalation_count"] == 0
    assert result["attempts"][0]["failure_class"] == expected_class.value


def test_positive_control_the_same_ladder_still_escalates_on_availability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the test above: a retryable class DOES climb.

    Without it, a ladder broken to escalate on nothing at all would satisfy
    every ``calls == ["local"]`` assertion in this file.
    """
    _install_ladder(monkeypatch, max_escalations=2)
    calls: list[str] = []

    def unavailable_effect(
        request: ModelLlmDelegationCallRequest,
    ) -> ModelLlmDelegationCallResult:
        calls.append(request.model_tier)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=False,
            failure_class=EnumDelegationFailureClass.MODEL_UNAVAILABLE,
            error_message="connection refused",
        )

    port = LocalDelegationDispatchPort(
        effect_handler=unavailable_effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    _dispatch(port, task_type="research", correlation_id=uuid4())

    assert len(calls) > 1, "an availability failure must still climb the ladder"


def test_the_typed_refusal_reaches_the_terminal_the_caller_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The payload survives onto the terminal, with its names and its action.

    A class alone cannot tell a customer WHICH credential to fix. The terminal
    carries the reference name, the env-var name and the remediation as fields,
    so a skill branches on them rather than parsing a sentence.
    """
    _install_ladder(monkeypatch, max_escalations=2)
    calls: list[str] = []

    port = LocalDelegationDispatchPort(
        effect_handler=_refusing_effect(
            EnumLocalCredentialRefusalReason.CREDENTIAL_ABSENT, calls
        ),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, task_type="research", correlation_id=uuid4())

    payload = result["credential_refusal"]
    assert payload["reason"] == "credential_absent"
    assert payload["credential_ref"] == _CREDENTIAL_REF
    assert payload["credential_env"] == "OMN18696_EXAMPLE_ENV"
    # Re-validating proves the terminal carries a parseable refusal, not a
    # look-alike dict a consumer would have to trust.
    parsed = ModelLocalCredentialRefusal.model_validate(payload)
    assert parsed.retryable is False
    assert parsed.remediation
    # And the prose surface names the credential too, for a human reading the
    # CLI's `failure_reason` without the typed payload in hand.
    assert _CREDENTIAL_REF in result["error_message"]


def test_a_non_credential_failure_carries_no_refusal_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key is ABSENT, not null, on every other terminal.

    Its presence is the refusal fact. A null placeholder on every terminal
    would make "refused on a credential" and "failed for another reason"
    indistinguishable without reading a second field.
    """
    _install_ladder(monkeypatch, max_escalations=0)

    def invalid_json_effect(
        request: ModelLlmDelegationCallRequest,
    ) -> ModelLlmDelegationCallResult:
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=False,
            failure_class=EnumDelegationFailureClass.INVALID_JSON,
            error_message="malformed provider response",
        )

    port = LocalDelegationDispatchPort(
        effect_handler=invalid_json_effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, task_type="research", correlation_id=uuid4())

    assert result["status"] == "failed"
    assert "credential_refusal" not in result
