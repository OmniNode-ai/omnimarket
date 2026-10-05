# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An OpenAI-compatible provider's error, delivered inside an HTTP 200 (OMN-18265).

WHY THIS EXISTS

    An aggregating provider does not always spend an HTTP status on an upstream
    failure. OpenRouter answers ``200 OK`` with no ``choices`` and a top-level
    ``error`` object when the model's upstream provider is the thing that broke:

        {"id": "gen-...", "error": {"message": "Upstream error from Nvidia:
         Service temporarily overloaded", "code": 502,
         "metadata": {"error_type": "provider_unavailable"}}}

    Both effect-boundary handlers used to read ``choices`` alone, find it empty,
    and report ``"API returned empty choices array"``. That sentence is a claim
    about the MODEL's answer; this body is a statement about the PROVIDER's
    availability. Collapsing the second into the first is what made a transient
    outage read as a permanent refusal: the delegation orchestrator's
    non-retryable marker set carries that exact literal (the minimal-safe
    OMN-13140 classification for a genuinely blank completion), so the workflow
    could only terminalise. Live occurrence: correlation
    ``c1838c39-bb8e-460d-8686-58b1cfbbc41c`` on onex-dev, 2026-09-12T19:11:59Z,
    where an immediate re-probe of the same slug on the same key returned
    content.

WHAT IT DOES NOT DO

    It invents nothing. A body carrying no ``error`` mapping, or one with no
    usable ``message``, parses to ``None`` and the callers' existing behaviour
    is reached unchanged — an empty ``choices`` with no error object still
    reports an empty choices array.

TWO CONSUMERS, ONE PARSE

    The two delegation paths classify failures differently and must not drift:
    the bus orchestrator classifies the error TEXT
    (``_inference_error_failure_class``), while the bus-less local dispatch port
    classifies the effect result's typed ``failure_class``. So this module
    offers both from one parse — :meth:`ModelProviderResponseError.as_error_message`
    carries the shared verdict through the text-only wire, and
    :attr:`ModelProviderResponseError.failure_class` derives the typed verdict
    from the vendor's own ``code`` / ``error_type``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass

#: Tokens in the vendor's ``code`` / ``error_type`` that name a throttle.
_RATE_LIMIT_TOKENS: frozenset[str] = frozenset({"rate", "throttl", "quota"})
#: Tokens that name a credential problem rather than an availability one.
_AUTH_TOKENS: frozenset[str] = frozenset({"auth", "unauthorized", "forbidden", "key"})


#: How every 200-delivered provider error message opens (see ``as_error_message``).
IN_BODY_ERROR_MESSAGE_PREFIX = "Provider returned an error in a 200 response"


class ModelProviderResponseError(BaseModel):
    """The three facts a 200-delivered provider error carries.

    ``code`` and ``error_type`` are optional because a provider may send only a
    message. They are never defaulted to a stand-in value: an absent code is
    absent, and the composed message says so by omission rather than by
    asserting a number nobody sent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message: str = Field(min_length=1)
    code: int | None = None
    error_type: str | None = None

    def as_error_message(self) -> str:
        """Compose the error text the delegation paths raise and classify.

        The prefix names the shape explicitly, because "the provider failed"
        and "the model answered with nothing" are different facts and a reader
        of a terminal record must be able to tell which one happened. The
        vendor's own words follow verbatim so the record is not a paraphrase.
        """
        detail: list[str] = []
        if self.code is not None:
            detail.append(f"provider code {self.code}")
        if self.error_type:
            detail.append(f"error_type={self.error_type}")
        detail.append(f"failure_class={self.failure_class.value}")
        suffix = f" ({', '.join(detail)})"
        return f"{IN_BODY_ERROR_MESSAGE_PREFIX}: {self.message}{suffix}"

    @property
    def failure_class(self) -> EnumDelegationFailureClass:
        """Classify from what the vendor said, for the typed-class consumer.

        Availability is the default rather than ``UNKNOWN``: a provider that
        answered at all, with an error naming its upstream, has told us the
        route is momentarily unusable. ``UNKNOWN`` would land this in the same
        bucket as a parse failure and lose that.
        """
        haystack = f"{self.error_type or ''} {self.message}".lower()
        by_status = failure_class_for_status(self.code)
        if by_status is not None:
            return by_status
        if any(token in haystack for token in _RATE_LIMIT_TOKENS):
            return EnumDelegationFailureClass.RATE_LIMITED
        if any(token in haystack for token in _AUTH_TOKENS):
            return EnumDelegationFailureClass.PROVIDER_AUTH_FAILED
        return EnumDelegationFailureClass.MODEL_UNAVAILABLE


def provider_failure_class_from_error_message(
    error_message: str,
) -> EnumDelegationFailureClass | None:
    """Read the shared verdict from a composed in-body provider error.

    The bus wire carries only text. Match the boundary's prefix and trailing
    class field so vendor prose cannot override the parser's verdict. Older
    messages without the field keep their existing classification.
    """
    if not error_message.startswith(f"{IN_BODY_ERROR_MESSAGE_PREFIX}: "):
        return None
    match = re.search(r"(?:\(|, )failure_class=([a-z_]+)\)$", error_message)
    if match is None:
        return None
    try:
        return EnumDelegationFailureClass(match.group(1))
    except ValueError:
        return None


def failure_class_for_status(
    status_code: int | None,
    detail: str | None = None,
) -> EnumDelegationFailureClass | None:
    """Classify a provider status code, or ``None`` when it names no class.

    OMN-18696. This decision existed only inside the 200-body classifier above,
    so the HTTP-STATUS path in ``HandlerLlmDelegationCall`` made it separately
    and made it differently: it special-cased 429 and swept every other status,
    401 and 403 included, into ``MODEL_UNAVAILABLE``. A provider that rejected
    the credential was therefore reported in the same class as one that was
    unreachable, and since that class is retryable the local ladder escalated
    past the rejection instead of refusing on it.

    One function, both callers. A 401 delivered in a status line and a 401
    declared in a 200 body are the same fact about the same credential, and
    they can no longer be classified apart.

    OMN-20157 adds the two provider answers that are about the ACCOUNT or the
    MODEL rather than the route, and that a retry on the same backend therefore
    cannot change:

    * 402, or a 400/403 whose text names prepaid credits, a balance, payment or
      a billing account: ``PROVIDER_BILLING``. A 429 is deliberately NOT read
      for billing words: Google's free-tier daily cap says "check your plan and
      billing details" on a 429, and that is a throttle with a reset, owned by
      the quota policy (OMN-16891).
    * 404: ``PROVIDER_MODEL_NOT_FOUND``. Google answers a model a key may not
      use ("no longer available to new users") with 404 NOT_FOUND.
    * 400 whose text says the API key is not valid: ``PROVIDER_AUTH_FAILED``.
      Google answers a bad key with 400 INVALID_ARGUMENT, not 401.

    ``detail`` is the provider's response text, when there is one.

    ``None`` -- not a default class -- for a status this does not recognise, so
    a caller keeps whatever fallback it already had rather than inheriting one
    from here.
    """
    text = detail or ""
    if status_code == 402:
        return EnumDelegationFailureClass.PROVIDER_BILLING
    if status_code in (400, 403) and _BILLING_TEXT.search(text):
        return EnumDelegationFailureClass.PROVIDER_BILLING
    if status_code == 429:
        return EnumDelegationFailureClass.RATE_LIMITED
    if status_code in (401, 403):
        return EnumDelegationFailureClass.PROVIDER_AUTH_FAILED
    if status_code == 400 and _INVALID_KEY_TEXT.search(text):
        return EnumDelegationFailureClass.PROVIDER_AUTH_FAILED
    if status_code == 404:
        return EnumDelegationFailureClass.PROVIDER_MODEL_NOT_FOUND
    return None


#: OMN-20157. Failure classes that are a typed provider REFUSAL about the account,
#: the key or the model: the provider answered, and asking the same backend again
#: cannot change its answer. None of them retries on the same backend. On a
#: customer's own route (no house fallback, INV-068) each one terminalises with
#: the provider's message; elsewhere it may still escalate to the next responder
#: (INV-063).
TYPED_PROVIDER_REFUSAL_CLASSES: frozenset[EnumDelegationFailureClass] = frozenset(
    {
        EnumDelegationFailureClass.PROVIDER_AUTH_FAILED,
        EnumDelegationFailureClass.PROVIDER_BILLING,
        EnumDelegationFailureClass.PROVIDER_MODEL_NOT_FOUND,
    }
)

#: Words a provider uses when it refuses on the account's money, read only on a
#: 400/403 (see :func:`failure_class_for_status`).
_BILLING_TEXT: re.Pattern[str] = re.compile(
    r"prepa(?:id|yment)|credits?\b.{0,40}\b(?:exhausted|depleted|empty|balance|"
    r"insufficient|remaining|run out)|insufficient\s+(?:balance|credits?|funds)|"
    r"out of credits|no credits|payment required|billing account|"
    r"billing (?:is not|to be|has been|not) (?:enabled|disabled|active)|"
    r"requires billing",
    re.IGNORECASE,
)

#: How a provider says the key itself is bad on a 400 (Google: "API key not
#: valid. Please pass a valid API key.", reason ``API_KEY_INVALID``).
_INVALID_KEY_TEXT: re.Pattern[str] = re.compile(
    r"api[ _-]?key[^.\n]{0,40}(?:not valid|invalid|expired)|"
    r"invalid[^.\n]{0,20}api[ _-]?key|api_key_invalid|pass a valid api key",
    re.IGNORECASE,
)

#: Shapes of a credential that must never survive into a message, even though a
#: provider's own error text is not expected to echo one.
_SECRET_SHAPES: re.Pattern[str] = re.compile(
    r"AIza[0-9A-Za-z_\-]{20,}|sk-[A-Za-z0-9_\-]{16,}|"
    r"(?i:bearer)\s+[A-Za-z0-9._\-]{8,}|(?i:(?:api_?key|key)=)[^&\s\"']+"
)

#: Bound on the provider text a message carries, so a large HTML error page
#: cannot swamp a receipt.
_MAX_PROVIDER_TEXT_CHARS = 1000


def scrub_secrets(text: str) -> str:
    """Replace anything shaped like a credential with a placeholder."""
    return _SECRET_SHAPES.sub("[redacted]", text)


def provider_message_from_text(text: str | None) -> str | None:
    """The provider's own error sentence out of a raw error body, or ``None``.

    Reads the OpenAI shape (``{"error": {"message": ...}}``), Google's
    OpenAI-compatible shape (the same object wrapped in a one-element list) and a
    bare ``{"message": ...}``. A body that is not JSON is returned stripped and
    bounded, since that text is still what the provider said. Credential shapes
    are scrubbed either way.
    """
    if not text or not text.strip():
        return None
    raw = text.strip()
    try:
        body: Any = json.loads(raw)
    except ValueError:
        return scrub_secrets(raw[:_MAX_PROVIDER_TEXT_CHARS])
    if isinstance(body, list) and body:
        body = body[0]
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("message"), str):
            message = error["message"].strip()
        elif isinstance(body.get("message"), str):
            message = body["message"].strip()
        else:
            message = ""
        if message:
            return scrub_secrets(message[:_MAX_PROVIDER_TEXT_CHARS])
    return scrub_secrets(raw[:_MAX_PROVIDER_TEXT_CHARS])


_REFUSAL_REMEDIATION: dict[EnumDelegationFailureClass, str] = {
    EnumDelegationFailureClass.PROVIDER_BILLING: (
        "The provider refused on your account's billing. The key is yours and so "
        "is the bill: add credits or enable billing on that provider account, "
        "then retry. Retrying before that cannot succeed."
    ),
    EnumDelegationFailureClass.PROVIDER_MODEL_NOT_FOUND: (
        "The provider does not offer this model to this key. The route "
        "re-resolves its model from the provider's own model list once; if this "
        "is still the answer, re-register the key with 'onex secret set' to pick "
        "a model the key can use."
    ),
    EnumDelegationFailureClass.PROVIDER_AUTH_FAILED: (
        "The provider rejected the key. Replace it with one the provider "
        "accepts, then retry."
    ),
}


def describe_provider_refusal(
    failure_class: EnumDelegationFailureClass,
    *,
    status_code: int | None,
    provider_text: str | None,
    model_id: str | None = None,
) -> str:
    """The plain message a typed provider refusal carries to the receipt and CLI.

    Starts with the class name in capitals so the refusal is recognisable in any
    text channel, carries the provider's own words verbatim (credential shapes
    scrubbed), and ends with what the customer can do about it.
    """
    status = f"HTTP {status_code}" if status_code is not None else "provider error"
    model = f" for model {model_id!r}" if model_id else ""
    said = provider_message_from_text(provider_text)
    quoted = f' The provider said: "{said}".' if said else ""
    remedy = _REFUSAL_REMEDIATION.get(failure_class, "")
    return (
        f"{failure_class.value.upper()} ({status}{model}):{quoted} {remedy}"
    ).strip()


def provider_error_from_body(
    body: Mapping[str, Any] | None,
) -> ModelProviderResponseError | None:
    """Return the provider's declared error, or ``None`` when it declared none.

    Fail-closed in the direction that matters: anything this cannot read as a
    genuine error object is ``None``, so the caller's existing empty-choices
    path is reached rather than a fabricated availability claim.
    """
    if not isinstance(body, Mapping):
        return None
    raw = body.get("error")
    if not isinstance(raw, Mapping):
        return None

    message = raw.get("message")
    if not isinstance(message, str) or not message.strip():
        return None

    code_raw = raw.get("code")
    # A provider may send the code as a string; a non-numeric one is dropped
    # rather than coerced, because a wrong number is worse than no number.
    code: int | None = None
    if isinstance(code_raw, bool):
        code = None
    elif isinstance(code_raw, int):
        code = code_raw
    elif isinstance(code_raw, str) and code_raw.strip().isdigit():
        code = int(code_raw.strip())

    metadata = raw.get("metadata")
    error_type_raw = (
        metadata.get("error_type") if isinstance(metadata, Mapping) else None
    )
    if error_type_raw is None:
        error_type_raw = raw.get("type")
    error_type = (
        error_type_raw.strip()
        if isinstance(error_type_raw, str) and error_type_raw.strip()
        else None
    )

    return ModelProviderResponseError(
        message=message.strip(), code=code, error_type=error_type
    )


__all__: list[str] = [
    "TYPED_PROVIDER_REFUSAL_CLASSES",
    "ModelProviderResponseError",
    "describe_provider_refusal",
    "failure_class_for_status",
    "provider_error_from_body",
    "provider_failure_class_from_error_message",
    "provider_message_from_text",
    "scrub_secrets",
]
