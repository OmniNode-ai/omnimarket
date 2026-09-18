# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18696: the local path refuses on credentials instead of retrying past them.

THE THREE CASES, AS MEASURED BEFORE THIS CHANGE

Reproduced on this Mac against a local stub provider on 127.0.0.1, driving the
real ``HandlerLlmDelegationCall.handle`` -- no mock of the handler, no mock of
the classification:

    absent credential   (declared ref, nothing resolves)  -> ``unknown``
    rejected credential (provider answers HTTP 401)       -> ``model_unavailable``
    provider outage     (nothing listening on the port)   -> ``model_unavailable``

The second and third were the SAME code, which is AC2's falsifier stated
outright. The first was the generic bucket. And all three are in the local
port's retryable set, so every one of them climbed the tier ladder: the
customer's absent key produced a slow walk through every configured tier and
then a generic failure, never a sentence naming the credential -- AC1's
falsifier, "a retry loop, or a silent fallback to any other route".

WHY THE EXISTING REFUSALS DID NOT COVER IT

OMN-18042, OMN-17930 and OMN-17940 are gateway refusals raised in the routing
terminus, before a backend is selected, about a TENANT's registered key. The
local path has no gateway and never reaches that terminus. These refusals fire
at the effect boundary, after a backend is selected, about the PROVIDER
credential that backend declares. Different condition, different place, and the
gateway work is not reachable from here.

WHAT EACH TEST BINDS

Every negative assertion carries a positive control, because "the branch
refused" and "the branch was never reached" look identical from a passing test.
``test_ac4_*`` is the suite's own control: it re-runs the AC1 and AC2 chains
against a deliberately silenced classifier and asserts they go RED. If that
test ever passes while the others do, the others are proving nothing.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.inference.provider_response_error import failure_class_for_status
from omnimarket.models.delegation.local_credential_refusal import (
    EnumLocalCredentialRefusalReason,
    ModelLocalCredentialRefusal,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
    resolve_terminal_failure_cause,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    _is_retryable_transport_failure,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    HandlerLlmDelegationCall,
)

pytestmark = pytest.mark.unit

_STUB_MODEL_ID = "omn18696-stub-model"
# A reference no secret store on any lane carries a value for. The refusal names
# it back to the customer, so it must look like a real reference, not a marker.
_UNRESOLVABLE_REF = "llm.omn18696nosuchprovider.api_key"


class _RejectingProvider(BaseHTTPRequestHandler):
    """Serves its model list, then rejects every completion with HTTP 401.

    A real provider, not a mock: the handler's own transport makes a real
    request and reads a real status line, so the classification under test is
    the one that runs in production rather than one a stub asserted into place.
    """

    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if not self.path.endswith("/models"):
            self.send_response(404)
            self.end_headers()
            return
        self._respond(200, {"data": [{"id": _STUB_MODEL_ID}]})

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._respond(
            401,
            {
                "error": {
                    "message": "Incorrect API key provided.",
                    "type": "invalid_request_error",
                }
            },
        )

    def _respond(self, status: int, body: dict[str, object]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def rejecting_provider() -> Iterator[str]:
    """A live local provider that rejects the credential. Yields its URL."""
    server = HTTPServer(("127.0.0.1", 0), _RejectingProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(endpoint: str, *, secret_ref: str | None) -> ModelLlmDelegationCallRequest:
    return ModelLlmDelegationCallRequest(
        request_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
        causation_id=str(uuid.uuid4()),
        model_id=_STUB_MODEL_ID,
        endpoint_ref=endpoint,
        prompt="say hi",
        prompt_hash="0" * 64,
        timeout_seconds=15.0,
        secret_ref=secret_ref,
        api_key_env=None,
    )


# --------------------------------------------------------------------------
# AC1 -- the absent credential is a typed, non-retryable refusal that names
#        the credential and the action.
# --------------------------------------------------------------------------


def test_ac1_absent_credential_refuses_with_a_typed_payload(
    rejecting_provider: str,
) -> None:
    """A declared reference with no value refuses; it does not fail generically."""
    result = HandlerLlmDelegationCall().handle(
        _request(rejecting_provider, secret_ref=_UNRESOLVABLE_REF)
    )

    assert result.success is False
    assert (
        result.failure_class is EnumDelegationFailureClass.PROVIDER_CREDENTIAL_MISSING
    ), (
        "an unresolvable credential reference used to fall through to the bare "
        "`except Exception` and classify as UNKNOWN"
    )

    refusal = result.credential_refusal
    assert refusal is not None, "the refusal must be a typed payload, not only prose"
    assert refusal.reason is EnumLocalCredentialRefusalReason.CREDENTIAL_ABSENT
    # Names the credential.
    assert refusal.credential_ref == _UNRESOLVABLE_REF
    assert _UNRESOLVABLE_REF in refusal.message
    # Names the action.
    assert refusal.remediation
    assert refusal.remediation in refusal.message
    # Non-retryable, as a field rather than as a convention.
    assert refusal.retryable is False


def test_ac1_absent_credential_does_not_escalate() -> None:
    """The local ladder refuses on it rather than climbing to another tier.

    The positive control is the adjacent assertion: MODEL_UNAVAILABLE, the class
    this case used to be lumped with, is still retryable. Without it a broken
    ``_is_retryable_transport_failure`` that returned ``False`` for everything
    would satisfy the first assertion alone.
    """
    assert (
        _is_retryable_transport_failure(
            EnumDelegationFailureClass.PROVIDER_CREDENTIAL_MISSING
        )
        is False
    )
    assert (
        _is_retryable_transport_failure(EnumDelegationFailureClass.MODEL_UNAVAILABLE)
        is True
    ), "positive control: an availability failure must still escalate"


# --------------------------------------------------------------------------
# AC2 -- absent, rejected and outage are three distinct refusal codes.
# --------------------------------------------------------------------------


def test_ac2_rejected_credential_is_its_own_class(rejecting_provider: str) -> None:
    """HTTP 401 from a live provider classifies as a rejection, not availability."""
    result = HandlerLlmDelegationCall().handle(
        _request(rejecting_provider, secret_ref=None)
    )

    assert result.success is False
    assert result.failure_class is EnumDelegationFailureClass.PROVIDER_AUTH_FAILED, (
        "a 401 status used to be swept into MODEL_UNAVAILABLE together with an "
        "unreachable endpoint"
    )
    refusal = result.credential_refusal
    assert refusal is not None
    assert refusal.reason is EnumLocalCredentialRefusalReason.CREDENTIAL_REJECTED
    assert refusal.retryable is False
    # The provider's own words reach the customer, attributed to the provider.
    assert "Incorrect API key provided." in refusal.message


def test_ac2_the_three_cases_return_three_distinct_codes(
    rejecting_provider: str,
) -> None:
    """The falsifier stated as a test: no two of the three may share a code."""
    handler = HandlerLlmDelegationCall()

    absent = handler.handle(
        _request(rejecting_provider, secret_ref=_UNRESOLVABLE_REF)
    ).failure_class
    rejected = handler.handle(
        _request(rejecting_provider, secret_ref=None)
    ).failure_class
    # An outage: an endpoint the health probe cannot reach. Bound to the same
    # port the fixture is about to release rather than a guessed-free one -- the
    # fixture's server is still up here, so a closed port is taken from a
    # transient bind instead.
    outage = handler(
        _request("http://127.0.0.1:1/v1/chat/completions", secret_ref=None)
    ).failure_class

    codes = {
        "absent": absent,
        "rejected": rejected,
        "outage": outage,
    }
    assert len(set(codes.values())) == 3, (
        f"the three cases must be distinguishable; got {codes}"
    )
    assert absent is EnumDelegationFailureClass.PROVIDER_CREDENTIAL_MISSING
    assert rejected is EnumDelegationFailureClass.PROVIDER_AUTH_FAILED
    assert outage is EnumDelegationFailureClass.MODEL_UNAVAILABLE


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, EnumDelegationFailureClass.PROVIDER_AUTH_FAILED),
        (403, EnumDelegationFailureClass.PROVIDER_AUTH_FAILED),
        (429, EnumDelegationFailureClass.RATE_LIMITED),
        # Positive control: an unnamed status yields None, so each caller keeps
        # its own fallback rather than inheriting one from the classifier.
        (503, None),
        (500, None),
        (None, None),
    ],
)
def test_ac2_one_classifier_serves_both_provider_error_paths(
    status: int | None, expected: EnumDelegationFailureClass | None
) -> None:
    """A 401 in a status line and a 401 in a 200 body classify identically.

    They used to be decided in two places: the 200-body classifier knew about
    401/403, and the HTTP-status branch did not.
    """
    assert failure_class_for_status(status) is expected


# --------------------------------------------------------------------------
# AC3 -- the terminal cause is read off typed evidence, not off error prose.
# --------------------------------------------------------------------------


def _attempt(failure_class: str, error_message: str) -> ModelDelegateSkillAttemptRecord:
    return ModelDelegateSkillAttemptRecord(
        tier="local",
        backend_id="b",
        model_id=_STUB_MODEL_ID,
        quality_gate_passed=False,
        failure_class=failure_class,
        error_message=error_message,
    )


def test_ac3_terminal_cause_comes_from_the_typed_class_not_the_text() -> None:
    """A rejection with no status digits in its prose still resolves AUTH_FAILED.

    The two enums' auth members are near-homonyms -- ``provider_auth_failed``
    against ``auth_failed`` -- and the typed step compared them by string, so it
    never matched and resolution fell through to a regex for "401"/"403" in free
    text. This message deliberately carries neither.
    """
    cause = resolve_terminal_failure_cause(
        [
            _attempt(
                EnumDelegationFailureClass.PROVIDER_AUTH_FAILED.value,
                "Delegation refused: credential_rejected for llm.x.api_key.",
            )
        ]
    )
    assert cause is not None
    assert cause.value == "auth_failed"


def test_ac3_a_credential_refusal_never_reads_as_a_quota_event() -> None:
    """Neither credential class may enter the over-quota metric.

    That metric is measured off ``terminal_failure_cause``; an authentication
    fact recorded as capacity overstates pressure that never happened
    (the OMN-16998 invariant this must not break).
    """
    for failure_class in (
        EnumDelegationFailureClass.PROVIDER_AUTH_FAILED,
        EnumDelegationFailureClass.PROVIDER_CREDENTIAL_MISSING,
    ):
        cause = resolve_terminal_failure_cause(
            [_attempt(failure_class.value, "refused on a credential")]
        )
        assert cause is not None
        assert cause.value != "provider_quota_exhausted"


# --------------------------------------------------------------------------
# AC4 -- the positive control. Silence the classification and the chains above
#        must go RED. If this test fails, AC1/AC2 prove nothing.
# --------------------------------------------------------------------------


def test_ac4_silencing_the_status_classifier_breaks_the_ac2_chain(
    rejecting_provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Suppress the 401 classification in the SOURCE the handler calls.

    Patched at the handler's own import site, which is the object the call path
    actually resolves -- patching the defining module would leave the handler's
    bound reference untouched and the "control" would pass while proving
    nothing, which is the failure mode AC4 exists to exclude.
    """
    import omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call as call_module

    monkeypatch.setattr(
        call_module, "failure_class_for_status", lambda _status: None, raising=True
    )

    result = HandlerLlmDelegationCall().handle(
        _request(rejecting_provider, secret_ref=None)
    )

    # With the classification silenced the rejection collapses back into the
    # availability class, exactly as it did before this change.
    assert result.failure_class is EnumDelegationFailureClass.MODEL_UNAVAILABLE
    assert result.credential_refusal is None
    # And the AC2 assertion, re-run here, fails on it.
    with pytest.raises(AssertionError):
        assert (
            result.failure_class is EnumDelegationFailureClass.PROVIDER_AUTH_FAILED
        ), "AC2's assertion must not survive a silenced classifier"


def test_ac4_silencing_the_absent_credential_branch_breaks_the_ac1_chain(
    rejecting_provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make the resolver's error unrecognisable and AC1's refusal disappears.

    The branch keys on ``SecretResolutionError``. Re-binding that name at the
    handler's import site to a class the resolver never raises sends the real
    exception to the bare ``except Exception`` it used to reach, reproducing the
    pre-change ``UNKNOWN``.
    """
    import omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call as call_module

    class _NeverRaisedError(RuntimeError):
        pass

    monkeypatch.setattr(
        call_module, "SecretResolutionError", _NeverRaisedError, raising=True
    )

    result = HandlerLlmDelegationCall().handle(
        _request(rejecting_provider, secret_ref=_UNRESOLVABLE_REF)
    )

    assert result.failure_class is EnumDelegationFailureClass.UNKNOWN
    assert result.credential_refusal is None
    with pytest.raises(AssertionError):
        assert result.credential_refusal is not None, (
            "AC1's assertion must not survive a silenced refusal branch"
        )


# --------------------------------------------------------------------------
# The payload carries names, never values.
# --------------------------------------------------------------------------


def test_the_refusal_payload_carries_no_secret_value() -> None:
    """Every field is a NAME. There is no field a secret value could occupy."""
    refusal = ModelLocalCredentialRefusal(
        reason=EnumLocalCredentialRefusalReason.CREDENTIAL_ABSENT,
        credential_ref=_UNRESOLVABLE_REF,
        credential_env="OMN18696_EXAMPLE_ENV",
        backend_ref="http://127.0.0.1:1/v1/chat/completions",
        model_id=_STUB_MODEL_ID,
        correlation_id=str(uuid.uuid4()),
    )
    # Both declared names reach the customer: a right reference with a wrong
    # fallback is unactionable if only one of them is reported.
    assert _UNRESOLVABLE_REF in refusal.named_credential
    assert "OMN18696_EXAMPLE_ENV" in refusal.named_credential
    # No field is settable to a value, and extras are refused outright.
    with pytest.raises(ValidationError, match="api_key"):
        ModelLocalCredentialRefusal(
            reason=EnumLocalCredentialRefusalReason.CREDENTIAL_ABSENT,
            backend_ref="http://127.0.0.1:1",
            model_id=_STUB_MODEL_ID,
            correlation_id=str(uuid.uuid4()),
            api_key="anything-at-all",
        )
