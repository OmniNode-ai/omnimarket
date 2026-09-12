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
    composes text whose vocabulary the text classifier already recognises, and
    :attr:`ModelProviderResponseError.failure_class` derives the typed verdict
    from the vendor's own ``code`` / ``error_type``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass

#: Tokens in the vendor's ``code`` / ``error_type`` that name a throttle.
_RATE_LIMIT_TOKENS: frozenset[str] = frozenset({"rate", "throttl", "quota"})
#: Tokens that name a credential problem rather than an availability one.
_AUTH_TOKENS: frozenset[str] = frozenset({"auth", "unauthorized", "forbidden", "key"})


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
        suffix = f" ({', '.join(detail)})" if detail else ""
        return f"Provider returned an error in a 200 response: {self.message}{suffix}"

    @property
    def failure_class(self) -> EnumDelegationFailureClass:
        """Classify from what the vendor said, for the typed-class consumer.

        Availability is the default rather than ``UNKNOWN``: a provider that
        answered at all, with an error naming its upstream, has told us the
        route is momentarily unusable. ``UNKNOWN`` would land this in the same
        bucket as a parse failure and lose that.
        """
        haystack = f"{self.error_type or ''} {self.message}".lower()
        if self.code == 429 or any(token in haystack for token in _RATE_LIMIT_TOKENS):
            return EnumDelegationFailureClass.RATE_LIMITED
        if self.code in (401, 403) or any(token in haystack for token in _AUTH_TOKENS):
            return EnumDelegationFailureClass.PROVIDER_AUTH_FAILED
        return EnumDelegationFailureClass.MODEL_UNAVAILABLE


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
    "ModelProviderResponseError",
    "provider_error_from_body",
]
