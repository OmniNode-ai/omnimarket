# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The effect boundary stamps which credential served the call (OMN-18196).

Axiom 9 forbids a customer route binding a house credential. Before this
field, nothing durable recorded which one answered a given run, so the
prohibition was unfalsifiable after the fact: there was no artifact anyone
could audit to find a violation.

``terminal_model_used`` cannot stand in for the fact. The same model id is
reachable on a customer's own OpenRouter key and on the platform's, so a
model-derived answer is a guess wearing evidence's clothes. These tests pin
that the value comes from the resolution the boundary actually performed.

The house case is asserted explicitly and by name. A field only ever observed
in its passing state is indistinguishable from a field hardcoded to
``customer_key``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import (
    EnumCredentialSource,
    ModelInferenceIntent,
)

from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
    _credential_source_for,
)

# A minted tenant-credential reference: ``cred_<tenant>_<provider>_<uuid4hex>``.
# Shape authority is omnimarket.tenant_credential_ref.
_CUSTOMER_REF = "cred_acme_openrouter_" + "0" * 32
# A house reference: the platform's own dotted secret path. Nothing about its
# shape says "house" -- it is house precisely BECAUSE it is not the minted
# tenant shape, which is what the classifier keys on.
_HOUSE_REF = "llm.openrouter.api_key"

_SUCCESSFUL_HTTPX_RESPONSE = {
    "id": "chatcmpl-omn18196",
    "choices": [{"message": {"content": "ok"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
}


def _make_intent(**kwargs: object) -> ModelInferenceIntent:
    defaults: dict[str, object] = {
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "system_prompt": "You are a helpful assistant.",
        "prompt": "Say ok.",
        "max_tokens": 16,
        "temperature": 0.3,
        "timeout_seconds": 30.0,
        "correlation_id": uuid4(),
    }
    defaults.update(kwargs)
    return ModelInferenceIntent(**defaults)  # type: ignore[arg-type]


def _run_handler(intent: ModelInferenceIntent, *, resolved_key: str | None):
    """Drive the handler with a stubbed secret store and a stubbed provider."""
    handler = HandlerInferenceIntent()
    mock_response = MagicMock()
    mock_response.json.return_value = _SUCCESSFUL_HTTPX_RESPONSE
    mock_response.raise_for_status.return_value = None

    module = "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent"
    with (
        patch(f"{module}._resolve_api_key", return_value=resolved_key),
        patch("httpx.Client") as mock_client_cls,  # onex-allow-faked-boundary
    ):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client
        return handler.handle(intent)


@pytest.mark.unit
class TestCredentialSourceClassification:
    """The classifier reads the RESOLVED binding, not the reference alone."""

    def test_a_minted_tenant_reference_that_resolved_is_the_customers_key(
        self,
    ) -> None:
        assert (
            _credential_source_for(_CUSTOMER_REF, "sk-resolved")
            is EnumCredentialSource.CUSTOMER_KEY
        )

    def test_a_platform_reference_that_resolved_is_a_house_credential(self) -> None:
        assert (
            _credential_source_for(_HOUSE_REF, "sk-resolved")
            is EnumCredentialSource.HOUSE
        )

    def test_no_reference_at_all_is_none_not_house(self) -> None:
        """An unauthenticated call is its own outcome, not a house call."""
        assert _credential_source_for(None, None) is EnumCredentialSource.NONE

    def test_a_withdrawn_customer_credential_reads_none_never_customer_key(
        self,
    ) -> None:
        """The OMN-18191 case, and the reason the resolved VALUE is the input.

        Revoking a tenant credential blanks ``secret_ref`` on the routing
        overlay row and KEEPS the row, deliberately, so the tenant does not
        fall back to the house ladder. The binding therefore still names the
        customer while carrying no reference, and the call runs
        unauthenticated.

        Classifying on the reference alone would report that call as
        ``customer_key``: a receipt asserting a customer key answered a call no
        customer key touched. That is the precise failure this field exists to
        make impossible, so it is pinned here rather than left to inspection.
        """
        assert _credential_source_for(None, None) is EnumCredentialSource.NONE

    def test_a_tenant_reference_that_resolved_to_nothing_is_none(self) -> None:
        """Resolution, not intent, is the discriminator."""
        assert _credential_source_for(_CUSTOMER_REF, None) is EnumCredentialSource.NONE

    @pytest.mark.parametrize("blank", ["", " ", "   ", "\t", "\n", " \t\n "])
    def test_a_value_that_is_blank_once_stripped_is_none_not_customer_key(
        self, blank: str
    ) -> None:
        """A string of whitespace is not a credential (found by the OMN-18201 lane).

        The resolver's emptiness test is a bare truthiness check that does not
        strip, so a stored value of spaces or tabs is treated as present and
        arrives at this classifier intact. The header built from it is
        ``Bearer`` followed by nothing.

        Reading that as ``customer_key`` would be a receipt asserting a
        customer key answered a call no usable credential touched -- the same
        misclassification the withdrawn-credential case above exists to
        prevent, reached by a different route.
        """
        assert _credential_source_for(_CUSTOMER_REF, blank) is EnumCredentialSource.NONE
        assert _credential_source_for(_HOUSE_REF, blank) is EnumCredentialSource.NONE

    def test_a_padded_but_real_credential_still_classifies_by_its_reference(
        self,
    ) -> None:
        """Positive control: stripping is a CLASSIFICATION input, not sanitising.

        A value with real content and incidental padding is a real credential.
        It must still classify by its reference, or this hardening would have
        silently turned every padded credential into an unauthenticated call in
        the record. Paired with the send-unchanged assertion below, which is
        what proves the value itself is never trimmed.
        """
        assert (
            _credential_source_for(_CUSTOMER_REF, "  sk-real-value  ")
            is EnumCredentialSource.CUSTOMER_KEY
        )


@pytest.mark.unit
class TestEffectBoundaryStampsTheResponse:
    def test_a_customer_key_call_records_customer_key(self) -> None:
        response = _run_handler(
            _make_intent(api_key_ref=_CUSTOMER_REF), resolved_key="sk-customer"
        )
        assert response.credential_source is EnumCredentialSource.CUSTOMER_KEY

    def test_a_house_key_call_records_house(self) -> None:
        """Acceptance criterion 5: the field is observed NOT passing.

        Without this, ``credential_source`` is only ever seen reading
        ``customer_key`` on the happy path, which is exactly what a field
        hardcoded to ``customer_key`` looks like. This is the test that tells
        the two apart.
        """
        response = _run_handler(
            _make_intent(api_key_ref=_HOUSE_REF), resolved_key="sk-house"
        )
        assert response.credential_source is EnumCredentialSource.HOUSE

    def test_an_unauthenticated_call_records_none(self) -> None:
        response = _run_handler(_make_intent(), resolved_key=None)
        assert response.credential_source is EnumCredentialSource.NONE

    def test_the_route_and_provider_are_echoed_from_the_intent(self) -> None:
        """The effect reports what it called; it does not re-derive it."""
        response = _run_handler(
            _make_intent(
                api_key_ref=_CUSTOMER_REF,
                route="byok-openrouter",
                provider="openrouter",
            ),
            resolved_key="sk-customer",
        )
        assert response.route == "byok-openrouter"
        assert response.provider == "openrouter"

    def test_a_failed_call_still_records_the_credential_it_resolved(self) -> None:
        """Escalation is driven by the error response, so it must carry it too."""
        handler = HandlerInferenceIntent()
        module = "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent"
        with (
            patch(f"{module}._resolve_api_key", return_value="sk-house"),
            patch("httpx.Client") as mock_client_cls,  # onex-allow-faked-boundary
        ):
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = RuntimeError("provider unreachable")
            mock_client_cls.return_value = mock_client
            response = handler.handle(_make_intent(api_key_ref=_HOUSE_REF))

        assert response.error_message
        assert response.credential_source is EnumCredentialSource.HOUSE

    def test_a_call_whose_credential_never_resolved_claims_nothing(self) -> None:
        """A boundary that never resolved a binding reports no credential fact.

        ``none`` would be a positive claim that the call ran unauthenticated.
        It did not run at all. Absent is the honest answer, and the receipt
        renders it as absent rather than as a credential class.
        """
        handler = HandlerInferenceIntent()
        module = "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent"
        with patch(
            f"{module}._resolve_api_key",
            side_effect=RuntimeError("secret store unreachable"),
        ):
            response = handler.handle(_make_intent(api_key_ref=_CUSTOMER_REF))

        assert response.error_message
        assert response.credential_source is None

    def test_a_padded_credential_is_sent_to_the_provider_unchanged(self) -> None:
        """This classifier sanitises nothing. The stored value goes out verbatim.

        The blank-value hardening keys on a STRIPPED copy for the purpose of
        classification only. If it had trimmed the value in place, every
        padded credential would silently start authenticating as a different
        string than the customer stored, which is a worse defect than the
        misclassification it fixes. This is the control that rules that out.
        """
        handler = HandlerInferenceIntent()
        padded = "  sk-real-value  "
        mock_response = MagicMock()
        mock_response.json.return_value = _SUCCESSFUL_HTTPX_RESPONSE
        mock_response.raise_for_status.return_value = None
        module = "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent"
        with (
            patch(f"{module}._resolve_api_key", return_value=padded),
            patch("httpx.Client") as mock_client_cls,  # onex-allow-faked-boundary
        ):
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client
            handler.handle(_make_intent(api_key_ref=_CUSTOMER_REF))
            sent = mock_client.post.call_args.kwargs["headers"]

        assert sent["Authorization"] == f"Bearer {padded}"
