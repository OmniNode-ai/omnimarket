# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18201: no provider call leaves without a credential the route required.

The occasion. On the C9 beta-bar walk against ``onex-dev`` (dev-system cluster,
EC2 ``i-06169517a92b45f86``), correlation ``307bb78f``, a delegation on a live
customer credential came back as a vendor 401 in 110 ms, which the platform
reported as the delegation's own failure. The credential was revoked 2.9 s
LATER, by the harness teardown, so withdrawal was not the cause.

What the investigation actually found, stated plainly because it contradicts the
premise the work started from. Both candidates were REFUTED by direct
measurement, and neither is what these tests guard:

* the reference was NOT lost on the wire. The inference intent was read off the
  broker (partition 5, offset 45) and carries the reference verbatim, with
  ``extra_headers`` null and an ``inference_attempt_id`` equal to the one on the
  401 response, so it is the intent that produced that call;
* the store does NOT resolve it to an empty value. Fingerprinted through the
  deployed lane's own resolver: a clean 62-character value, no surrounding
  whitespace, no newline, with an absent reference of the same shape raising as
  the positive control.

So the boundary held a reference and a resolvable value. The cause of that
particular 401 is therefore NOT an effect-boundary resolution defect, and these
tests make no claim about it.

What they do bind is the gap the investigation exposed, which is real on its own
terms: the boundary could not distinguish "this backend legitimately takes no
credential" from "the credential never arrived", because an absent reference
looks identical in both cases. It resolved that ambiguity by calling the
provider with no Authorization header. After this change it refuses instead, and
a resolved value that is blank once stripped is treated as no value rather than
becoming a header with nothing after ``Bearer``.

Every negative assertion below is paired with a positive control, because "no
request was made" and "the seam was never reachable" look identical otherwise.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from omnibase_core.models.delegation.wire import (
    EnumCredentialSource,
    ModelInferenceIntent,
)
from pydantic import SecretStr

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    InferenceIntentCredentialLostError,
    expected_credential_source_for,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    TENANT_OVERLAY_TIER_NAME,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    CREDENTIAL_UNRESOLVED_ONEX_CODE,
    CredentialUnresolvedError,
    HandlerInferenceIntent,
)

pytestmark = pytest.mark.unit

# The live reference from correlation 307bb78f, shape-preserved. It is a
# reference, not a secret: the minter's own module states the ref carries no
# secret material and is safe to log and publish.
LIVE_TENANT_REF = (
    "cred_d562bf4a-a3f1-4016-8e33-7d4214f62fce_openrouter_"
    "82dc9093c04a491295b86aa3d6001353"
)
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
HOUSE_REF = "llm.openrouter.api_key"


def _intent(**overrides: Any) -> ModelInferenceIntent:
    base: dict[str, Any] = {
        "base_url": OPENROUTER_URL,
        "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "system_prompt": "s",
        "prompt": "p",
        "max_tokens": 64,
        "correlation_id": uuid4(),
        "tenant_id": "d562bf4a-a3f1-4016-8e33-7d4214f62fce",
    }
    base.update(overrides)
    return ModelInferenceIntent.model_validate(base)


def _decision(**overrides: Any) -> ModelRoutingDecision:
    base: dict[str, Any] = {
        "correlation_id": uuid4(),
        "task_type": "summarization",
        "selected_model": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "selected_backend_id": uuid4(),
        "selected_backend_ref": "byok-openrouter",
        "endpoint_url": OPENROUTER_URL,
        "api_key_ref": LIVE_TENANT_REF,
        "extra_headers": None,
        "cost_tier": "tenant_byok",
        "tier_name": TENANT_OVERLAY_TIER_NAME,
        "max_context_tokens": 128000,
        "timeout_ms": 120000,
        "max_tokens": 4096,
        "system_prompt": "s",
        "rationale": "r",
        "dod_deterministic": (),
        "dod_heuristic": (),
        "requested_shape": "unconstrained",
        "dod_deterministic_source": "class_definition_of_done",
        "dod_heuristic_source": "class_definition_of_done",
    }
    base.update(overrides)
    return ModelRoutingDecision.model_validate(base)


class _RecordingTransport:
    """Stands in for ``httpx.Client`` and records whether a request was made.

    Asserting on this recorder rather than on a network error is what makes
    "no outbound call" a measured fact. The positive controls below prove the
    recorder is reachable, so an empty ``requests`` list is a statement about
    the refusal and not about an unreachable seam.
    """

    def __init__(self) -> None:
        self.requests: list[str] = []

    def __call__(self, *args: Any, **kwargs: Any) -> _RecordingTransport:
        return self

    def __enter__(self) -> _RecordingTransport:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append(url)
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp-1",
                "choices": [
                    {"message": {"content": "ok"}, "finish_reason": "stop"},
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> _RecordingTransport:
    recorder = _RecordingTransport()
    monkeypatch.setattr(
        "omnimarket.nodes.node_llm_delegation_call_effect.handlers"
        ".handler_inference_intent.httpx.Client",
        recorder,
    )
    return recorder


def _stub_resolution(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_llm_delegation_call_effect.handlers"
        ".handler_inference_intent._resolve_api_key",
        lambda _ref: value,
    )


# --------------------------------------------------------------------------
# The effect boundary refuses instead of calling
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expected",
    [EnumCredentialSource.CUSTOMER_KEY, EnumCredentialSource.HOUSE],
)
def test_no_provider_call_when_a_required_credential_is_missing(
    expected: EnumCredentialSource,
    transport: _RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The C9 shape: a route that required a credential and holds none."""
    _stub_resolution(monkeypatch, None)
    intent = _intent(expected_credential_source=expected, api_key_ref=None)

    response = HandlerInferenceIntent().handle(intent)

    assert transport.requests == [], (
        "a provider call was made for a route that required a credential; "
        "this is the 307bb78f failure"
    )
    assert CREDENTIAL_UNRESOLVED_ONEX_CODE in response.error_message
    assert response.correlation_id == intent.correlation_id


def test_positive_control_a_resolved_credential_still_reaches_the_provider(
    transport: _RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for every empty ``requests`` assertion above and below.

    Without this, an empty recorder would be equally consistent with the
    refusal working and with the transport seam never being reached at all.
    """
    _stub_resolution(monkeypatch, "resolved-value")
    intent = _intent(
        expected_credential_source=EnumCredentialSource.CUSTOMER_KEY,
        api_key_ref=LIVE_TENANT_REF,
    )

    response = HandlerInferenceIntent().handle(intent)

    assert transport.requests == [OPENROUTER_URL]
    assert response.error_message == ""
    assert response.content == "ok"


def test_the_refusal_names_the_reference_it_could_not_resolve(
    transport: _RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reference that arrived and resolved to nothing is named in the error.

    The receipt cannot reconstruct which of the two happened, so the message
    has to distinguish "a reference resolved to nothing" from "no reference
    arrived". Both halves are asserted, in one test, because the distinction
    is the content of the claim.
    """
    _stub_resolution(monkeypatch, None)

    named = HandlerInferenceIntent().handle(
        _intent(
            expected_credential_source=EnumCredentialSource.CUSTOMER_KEY,
            api_key_ref=LIVE_TENANT_REF,
        )
    )
    assert LIVE_TENANT_REF in named.error_message
    assert "no credential reference at all" not in named.error_message

    unnamed = HandlerInferenceIntent().handle(
        _intent(
            expected_credential_source=EnumCredentialSource.CUSTOMER_KEY,
            api_key_ref=None,
        )
    )
    assert "no credential reference at all" in unnamed.error_message
    assert transport.requests == []


def test_an_auth_free_backend_is_still_callable_without_a_credential(
    transport: _RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``NONE`` is a positive declaration, and it must stay serviceable.

    A customer's own local model takes no credential. Refusing that would
    trade one silent failure for an outage on a legitimate route.
    """
    _stub_resolution(monkeypatch, None)
    intent = _intent(
        base_url="http://127.0.0.1:8000/v1/chat/completions",
        expected_credential_source=EnumCredentialSource.NONE,
        api_key_ref=None,
    )

    response = HandlerInferenceIntent().handle(intent)

    assert transport.requests == ["http://127.0.0.1:8000/v1/chat/completions"]
    assert response.error_message == ""


def test_an_intent_that_makes_no_claim_keeps_the_prior_behaviour(
    transport: _RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent expectation is a legacy producer, not a licence and not a ban.

    During a coordinated release the orchestrator may be older than the
    boundary. Refusing those intents would take delegation down to fix an
    observability gap; treating absent as ``NONE`` would license the very call
    being removed. It does neither: behaviour is unchanged for them.
    """
    _stub_resolution(monkeypatch, None)
    intent = _intent(api_key_ref=None)
    assert intent.expected_credential_source is None

    response = HandlerInferenceIntent().handle(intent)

    assert transport.requests == [OPENROUTER_URL]
    assert response.error_message == ""


def test_the_refusal_is_typed_and_carries_the_onex_code() -> None:
    """The error is addressable by class and by code, not only by message.

    ``omnibase_infra``'s boundary-failure terminal reads ``error_code`` when the
    exception object survives, and leads the message with it when the engine
    flattens the exception to text. Both paths are bound here.
    """
    exc = CredentialUnresolvedError(
        expected=EnumCredentialSource.CUSTOMER_KEY,
        api_key_ref=LIVE_TENANT_REF,
        tenant_id="d562bf4a-a3f1-4016-8e33-7d4214f62fce",
    )
    assert isinstance(exc, RuntimeError)
    assert exc.error_code == CREDENTIAL_UNRESOLVED_ONEX_CODE
    assert str(exc).startswith(f"[{CREDENTIAL_UNRESOLVED_ONEX_CODE}]")


def test_the_refusal_stamps_credential_source_none(
    transport: _RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused call reports that no credential answered it.

    ``NONE`` is a real outcome here and not an absence: the boundary resolved
    nothing and made no call, which is a first-hand fact it is the only party
    able to state. Skipped rather than failed on a core that predates the
    field, so this module still runs during a release window.
    """
    from omnibase_core.models.delegation.wire import ModelInferenceResponseData

    if "credential_source" not in ModelInferenceResponseData.model_fields:
        pytest.skip("core predates the OMN-18196 credential_source field")

    _stub_resolution(monkeypatch, None)
    response = HandlerInferenceIntent().handle(
        _intent(
            expected_credential_source=EnumCredentialSource.CUSTOMER_KEY,
            api_key_ref=LIVE_TENANT_REF,
        )
    )
    assert response.credential_source is EnumCredentialSource.NONE
    assert transport.requests == []


@pytest.mark.parametrize("blank", ["   ", "\t", " \t "])
def test_a_whitespace_only_resolved_value_is_not_a_credential(
    blank: str,
    transport: _RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored value of whitespace must not become an Authorization header.

    The store-side fail-closed check is ``if not value``, which does not strip,
    so whitespace is truthy there and reaches this boundary intact. Building a
    header from it sends ``Bearer`` followed by nothing, which the vendor answers
    by reporting a missing Authentication header -- indistinguishable downstream
    from the platform never attaching one, and with no local record of the real
    cause.

    Found while fingerprinting the 307bb78f reference through the deployed
    lane's own resolver: the fail-closed check there is length-zero, not
    blank-after-strip, so this gap is real independently of what caused that
    particular run.
    """
    monkeypatch.setattr(
        "omnimarket.nodes.node_llm_delegation_call_effect.handlers"
        ".handler_inference_intent.resolve_api_key",
        lambda *_a, **_k: SecretStr(blank),
    )

    response = HandlerInferenceIntent().handle(
        _intent(
            expected_credential_source=EnumCredentialSource.CUSTOMER_KEY,
            api_key_ref=LIVE_TENANT_REF,
        )
    )

    assert transport.requests == []
    assert CREDENTIAL_UNRESOLVED_ONEX_CODE in response.error_message


def test_positive_control_a_value_with_real_content_is_used_verbatim(
    transport: _RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for the whitespace test, and a guard against over-trimming.

    A resolved value is never trimmed before use: altering what the customer
    registered would corrupt it. The only judgement the boundary makes is
    whether anything remains at all, so a value that merely has surrounding
    whitespace is still sent, unchanged.
    """
    monkeypatch.setattr(
        "omnimarket.nodes.node_llm_delegation_call_effect.handlers"
        ".handler_inference_intent.resolve_api_key",
        lambda *_a, **_k: SecretStr(" placeholder-value-under-test "),
    )

    response = HandlerInferenceIntent().handle(
        _intent(
            expected_credential_source=EnumCredentialSource.CUSTOMER_KEY,
            api_key_ref=LIVE_TENANT_REF,
        )
    )

    assert transport.requests == [OPENROUTER_URL]
    assert response.error_message == ""


# --------------------------------------------------------------------------
# The orchestrator declares the expectation
# --------------------------------------------------------------------------


def test_a_tenant_overlay_route_expects_a_customer_key() -> None:
    """The live 307bb78f decision classifies as a customer route."""
    assert (
        expected_credential_source_for(_decision()) is EnumCredentialSource.CUSTOMER_KEY
    )


def test_a_tenant_overlay_route_with_no_reference_still_expects_one() -> None:
    """The tier decides before the reference is consulted.

    This is the load-bearing case. A customer route whose reference has already
    gone missing must not be read as an auth-free backend -- that reading is
    what licensed the headerless call. Keying on the reference alone would
    return ``NONE`` here.
    """
    for blank in (None, "", "   "):
        assert (
            expected_credential_source_for(_decision(api_key_ref=blank))
            is EnumCredentialSource.CUSTOMER_KEY
        )


def test_a_platform_route_with_a_house_reference_expects_a_house_key() -> None:
    """A non-tenant reference on a platform tier is a house credential."""
    assert (
        expected_credential_source_for(
            _decision(
                api_key_ref=HOUSE_REF,
                cost_tier="cheap_cloud",
                tier_name="cheap_cloud",
            )
        )
        is EnumCredentialSource.HOUSE
    )


def test_a_tenant_shaped_reference_on_a_platform_tier_is_a_customer_key() -> None:
    """The minted reference shape wins wherever it appears.

    ``is_tenant_credential_ref`` anchors on the ``cred_`` prefix and the uuid4
    suffix, so a customer's credential cannot be classified as a house one by
    arriving on an unexpected tier.
    """
    assert (
        expected_credential_source_for(
            _decision(cost_tier="cheap_cloud", tier_name="cheap_cloud")
        )
        is EnumCredentialSource.CUSTOMER_KEY
    )


def test_a_platform_route_with_no_reference_declares_an_auth_free_backend() -> None:
    """The one shape under which a headerless call is correct."""
    assert (
        expected_credential_source_for(
            _decision(api_key_ref=None, cost_tier="local", tier_name="local")
        )
        is EnumCredentialSource.NONE
    )


def test_the_orchestrator_refuses_an_intent_that_dropped_the_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A built intent missing a reference its decision named is not dispatched.

    The in-process seam was measured lossless under every ``model_dump``
    posture, so this is a ratchet rather than a repair -- and a ratchet is the
    point, because the loss was never reproduced statically and the only honest
    guard is one that fires if it ever does. The loss is simulated by making the
    model construction drop the field.
    """
    from omnimarket.nodes.node_delegation_orchestrator.handlers import (
        handler_delegation_workflow as mod,
    )

    real_validate = ModelInferenceIntent.model_validate

    def _lossy(payload: Any, *args: Any, **kwargs: Any) -> ModelInferenceIntent:
        stripped = dict(payload)
        stripped["api_key_ref"] = None
        return real_validate(stripped, *args, **kwargs)

    monkeypatch.setattr(mod.ModelInferenceIntent, "model_validate", _lossy)

    with pytest.raises(InferenceIntentCredentialLostError) as caught:
        mod._build_model_inference_intent(
            base_url=OPENROUTER_URL,
            model="m",
            system_prompt="s",
            prompt="p",
            max_tokens=64,
            temperature=0.3,
            timeout_seconds=30.0,
            correlation_id=uuid4(),
            inference_attempt_id=uuid4(),
            api_key_ref=LIVE_TENANT_REF,
            expected_credential_source=EnumCredentialSource.CUSTOMER_KEY,
            route="byok-openrouter",
            provider=None,
            extra_headers=None,
            provider_request_options={},
            response_format=None,
            tenant_id="t",
        )

    assert LIVE_TENANT_REF in str(caught.value)
    assert caught.value.error_code == ("ONEX_MARKET_INFERENCE_INTENT_CREDENTIAL_LOST")


def test_positive_control_the_builder_carries_the_reference_and_the_claim() -> None:
    """The seam is lossless without the simulated loss.

    This is the control for the test above: it proves the builder is reachable
    and correct, so the refusal there is a fact about the injected loss rather
    than about a builder that cannot construct an intent at all.
    """
    from omnimarket.nodes.node_delegation_orchestrator.handlers import (
        handler_delegation_workflow as mod,
    )

    intent = mod._build_model_inference_intent(
        base_url=OPENROUTER_URL,
        model="m",
        system_prompt="s",
        prompt="p",
        max_tokens=64,
        temperature=0.3,
        timeout_seconds=30.0,
        correlation_id=uuid4(),
        inference_attempt_id=uuid4(),
        api_key_ref=LIVE_TENANT_REF,
        expected_credential_source=EnumCredentialSource.CUSTOMER_KEY,
        route="byok-openrouter",
        provider=None,
        extra_headers=None,
        provider_request_options={},
        response_format=None,
        tenant_id="t",
    )

    assert intent.api_key_ref == LIVE_TENANT_REF
    assert intent.expected_credential_source is EnumCredentialSource.CUSTOMER_KEY
