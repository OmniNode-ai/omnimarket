# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17930 (OMN-17372 AC2) — the refusal code must survive the consume boundary.

OMN-17082 mints the keyless-customer refusal correctly: ``delta()`` raises
:class:`CustomerKeyRefusedError` carrying a :class:`ModelCustomerKeyRefusal`
whose ``error_code`` is the stable public literal
``delegation.customer_provider_key.absent`` and whose remediation tells the
customer to register a provider key. ``tests/test_omn17082_customer_key_terminus.py``
pins that.

It stopped there. Repository-wide, that code appeared NOWHERE downstream of
the routing reducer, and what the caller actually received was the bare
string ``CustomerKeyRefusedError``:

* ``omnibase_infra.runtime.boundary_failure_terminal._first_onex_code``
  recovers a code from an ``error_code`` attribute, or from a message token,
  that fullmatches ``ONEX_[A-Z0-9_]+``. The exception was a bare
  ``Exception`` with no ``error_code``, and the dotted public code cannot
  match that token in any case, so ``failure_code`` was ``None``.
* ``handle_boundary_failure_terminal`` in the delegation orchestrator builds
  ``terminal_failure_reason`` as ``f"{failure_class}: {failure_code}"`` only
  when a code exists — otherwise the bare class name.
* On the LIVE path the exception object does not survive at all.
  ``MessageDispatchEngine.dispatch`` flattens it through
  ``sanitize_error_message`` into ``HandlerDispatchFailureError``'s message,
  and that helper collapses the whole message to
  ``CustomerKeyRefusedError: [REDACTED - potentially sensitive data]`` when it
  contains any ``SENSITIVE_PATTERNS`` token. The refusal's own message did:
  ``inference-credentials`` and ``platform credential`` both carry
  ``credential``. So on the wire the customer's "typed refusal naming the
  missing credential" was a class name — no code, no remediation.

What this module pins, against the REAL infra classifier and the REAL
sanitizer rather than transcriptions of them:

1. The public dotted code is not an ONEX token (the reason an alias exists at
   all), and the alias is.
2. Classifying the raised exception yields the alias as ``failure_code`` and
   ``CustomerKeyRefusedError`` as ``failure_class``.
3. The same holds once the exception has been flattened exactly the way the
   engine flattens it — for EVERY refusal reason, because the sanitizer's
   verdict is per-message and a reason phrase carrying a sensitive token
   would silently regress this.
4. Driven through the orchestrator's async ``handle()`` on a seeded
   correlation, the emitted ``ModelDelegationFailed`` carries the alias in
   ``terminal_failure_reason`` and the public code plus the remediation in
   ``failure_reason``.
5. Negative control: a plain ``RuntimeError`` still carries no code.

The gateway hop (onex-api ``workflow_terminal_consumer`` writes only
``status`` on the failed topic) is delegation-v2 Task 4.2 and is not asserted
here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    HandlerDispatchFailureError,
)
from omnibase_infra.runtime.boundary_failure_terminal import (
    _ONEX_CODE_TOKEN,
    ModelBoundaryFailureTerminal,
    classify_boundary_failure,
)
from omnibase_infra.utils.util_error_sanitization import sanitize_error_message

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationResult,
)
from omnimarket.routing.customer_key_terminus import (
    CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE,
    CustomerKeyRefusedError,
    EnumCustomerKeyRefusalReason,
    EnumDelegationSurface,
    ModelCustomerKeyRefusal,
)

# The wire alias. Pinned as a literal on purpose: it is the machine-readable
# half the delegation projections group on, so changing it is a contract change
# and must fail here first — exactly as the dotted public code is pinned in
# ``tests/test_omn17082_customer_key_terminus.py``.
_WIRE_CODE = "ONEX_MARKET_CUSTOMER_PROVIDER_KEY_ABSENT"
_ROUTING_REQUEST_TOPIC = "onex.cmd.omnibase-infra.delegation-routing-request.v1"  # onex-topic-allow: the routing leg's consumed topic, keyed in _BOUNDARY_FAILURE_LEGS
_TENANT = "tenant-acme"
_TASK_TYPE = "code_generation"
_REMEDIATION_PHRASE = "register a provider key"


def _missing_key_refusal(correlation_id: UUID) -> CustomerKeyRefusedError:
    return CustomerKeyRefusedError.for_missing_key(
        tenant_id=_TENANT,
        task_type=_TASK_TYPE,
        correlation_id=correlation_id,
        surface=EnumDelegationSurface.CLOUD,
    )


def _refusal_for(
    reason: EnumCustomerKeyRefusalReason, correlation_id: UUID
) -> CustomerKeyRefusedError:
    """A refusal for any reason the enum names, in the shape the terminus raises."""
    if reason is EnumCustomerKeyRefusalReason.NO_PROVIDER_KEY_REGISTERED:
        return _missing_key_refusal(correlation_id)
    return CustomerKeyRefusedError(
        ModelCustomerKeyRefusal(
            reason=reason,
            tenant_id=_TENANT,
            task_type=_TASK_TYPE,
            surface=EnumDelegationSurface.CLOUD,
            correlation_id=correlation_id,
            attempted_api_key_ref="llm.glm.api_key",
            attempted_backend_ref="cloud-glm",
        )
    )


def _flatten_like_the_engine(exc: Exception) -> HandlerDispatchFailureError:
    """Reproduce the live shape the boundary actually classifies.

    ``MessageDispatchEngine.dispatch`` catches the handler's exception, stores
    ``sanitize_error_message(exc)`` as ``error_message`` on a FAILED result,
    and ``_raise_if_silent_dispatch_failure`` raises
    ``HandlerDispatchFailureError`` with that text. The original object is
    gone; the text is the only thing ``classify_boundary_failure`` can read.
    """
    return HandlerDispatchFailureError(
        f"dispatch to topic={_ROUTING_REQUEST_TOPIC} returned "
        "status=handler_error with no terminal output "
        f"(dispatcher_id=routing-test): {sanitize_error_message(exc)}",
        failure_code="ONEX_CORE_095_HANDLER_EXECUTION_ERROR",
    )


def _classify(exc: Exception, correlation_id: UUID) -> ModelBoundaryFailureTerminal:
    return classify_boundary_failure(
        exc,
        topic=_ROUTING_REQUEST_TOPIC,
        correlation_id=correlation_id,
        failure_reason=str(exc),
        failure_code=getattr(exc, "failure_code", None),
    )


def _make_request(correlation_id: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Reply with the single word: alive.",
        task_type="research",
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# 1 — why an alias exists: the public code is not an ONEX token, the alias is
# ---------------------------------------------------------------------------


def test_the_public_code_is_not_an_onex_token_and_the_wire_alias_is() -> None:
    """The dotted public contract cannot be what the boundary recovers.

    This is the justification for carrying a second, token-conformant code
    ALONGSIDE the public one rather than renaming the public one: the
    boundary's token regex is the infra contract, and the dotted literal is
    the customer contract, and neither may bend to the other.
    """
    assert CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE == (
        "delegation.customer_provider_key.absent"
    ), "the public code is pinned by OMN-17082 and must not be renamed"
    assert _ONEX_CODE_TOKEN.fullmatch(CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE) is None
    # Positive control for the regex itself.
    assert _ONEX_CODE_TOKEN.fullmatch(_WIRE_CODE) is not None


# ---------------------------------------------------------------------------
# 2 — the raised exception classifies to the alias
# ---------------------------------------------------------------------------


def test_the_raised_refusal_classifies_to_the_wire_code() -> None:
    correlation_id = uuid4()
    exc = _missing_key_refusal(correlation_id)

    terminal = _classify(exc, correlation_id)

    assert terminal.failure_code == _WIRE_CODE, (
        "the boundary recovered no code from the refusal; the delegation "
        f"terminal will say a bare class name (got {terminal.failure_code!r})"
    )
    assert terminal.failure_class == "CustomerKeyRefusedError"
    assert terminal.correlation_id == correlation_id
    # The human-readable half must still name the public contract and tell
    # the customer what to do.
    assert CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE in terminal.failure_reason
    assert _REMEDIATION_PHRASE in terminal.failure_reason.casefold()
    assert _TENANT in terminal.failure_reason


def test_the_exception_carries_the_wire_code_as_its_error_code() -> None:
    """The object-path attribute the classifier reads first, pinned directly.

    Mutation check: dropping ``error_code`` from the exception makes this and
    the classification test above fail together.
    """
    exc = _missing_key_refusal(uuid4())
    assert getattr(exc, "error_code", None) == _WIRE_CODE
    # The refusal payload keeps the PUBLIC code — the alias is for the boundary.
    assert exc.refusal.error_code == CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE


# ---------------------------------------------------------------------------
# 3 — the engine-flattened shape, for every refusal reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", list(EnumCustomerKeyRefusalReason))
def test_the_flattened_refusal_still_classifies_to_the_wire_code(
    reason: EnumCustomerKeyRefusalReason,
) -> None:
    """The live path: only sanitized TEXT reaches the boundary.

    Parametrized over every reason because ``sanitize_error_message`` collapses
    an entire message on any sensitive token, and a reason phrase that
    carries one would silently turn this refusal back into a bare class name.
    """
    correlation_id = uuid4()
    exc = _refusal_for(reason, correlation_id)

    sanitized = sanitize_error_message(exc)
    assert "[REDACTED" not in sanitized, (
        "the sanitizer collapsed the refusal message, so nothing but the class "
        f"name survives to the boundary: {sanitized!r}"
    )

    terminal = _classify(_flatten_like_the_engine(exc), correlation_id)

    assert terminal.failure_code == _WIRE_CODE, (
        f"reason={reason.value}: recovered {terminal.failure_code!r} from the "
        "flattened message"
    )
    assert terminal.failure_class == "CustomerKeyRefusedError", (
        "the boundary wrapper must not win the attribution over the real cause"
    )
    assert CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE in terminal.failure_reason
    assert _REMEDIATION_PHRASE in terminal.failure_reason.casefold()


def test_the_flattened_message_carries_no_secret_material() -> None:
    """Reference NAMES may cross the boundary; values never do."""
    exc = _refusal_for(
        EnumCustomerKeyRefusalReason.HOUSE_CREDENTIAL_ON_CUSTOMER_PATH, uuid4()
    )
    text = str(exc)
    # The message is built from the refusal's typed fields, none of which is
    # a secret value; the ref NAME is allowed and the ticket-pinned house
    # value string from the OMN-17082 fixtures is not present by construction.
    assert "house-key-planted-by-test" not in text
    assert _WIRE_CODE in text


# ---------------------------------------------------------------------------
# 4 — through the orchestrator: the delegation terminal names the code
# ---------------------------------------------------------------------------


async def test_the_delegation_terminal_carries_the_code_and_the_remediation() -> None:
    """One boundary terminal in, one ATTRIBUTED delegation terminal out.

    Driven through the async ``handle()`` the live dispatcher calls (memory
    ``feedback_real_dispatch_path_tests``), on the flattened shape.
    """
    correlation_id = uuid4()
    handler = HandlerDelegationWorkflow(workflows={})
    intents = await handler.handle(_make_request(correlation_id))
    assert len(intents) == 1, "the request must emit the routing intent first"

    boundary_terminal = _classify(
        _flatten_like_the_engine(_missing_key_refusal(correlation_id)),
        correlation_id,
    )
    events = await handler.handle(boundary_terminal)

    assert len(events) == 1, (
        f"expected exactly one delegation terminal; got "
        f"{[type(e).__name__ for e in events]}"
    )
    terminal = events[0]
    assert isinstance(terminal, ModelDelegationResult)
    assert type(terminal).__name__ == "ModelDelegationFailed"
    assert terminal.correlation_id == correlation_id
    assert terminal.terminal_failure_reason is not None
    assert _WIRE_CODE in terminal.terminal_failure_reason, (
        "the machine-readable half of the terminal still says a bare class "
        f"name: {terminal.terminal_failure_reason!r}"
    )
    assert "CustomerKeyRefusedError" in terminal.terminal_failure_reason
    assert CUSTOMER_PROVIDER_KEY_ABSENT_ERROR_CODE in terminal.failure_reason
    assert _REMEDIATION_PHRASE in terminal.failure_reason.casefold()
    assert "timeout" not in terminal.failure_reason.casefold()


# ---------------------------------------------------------------------------
# 5 — negative control
# ---------------------------------------------------------------------------


def test_a_plain_runtime_error_still_carries_no_code() -> None:
    """The alias is attached to the refusal, not sprayed over every failure."""
    correlation_id = uuid4()
    terminal = _classify(RuntimeError("boom"), correlation_id)
    assert terminal.failure_code is None
    assert terminal.failure_class == "RuntimeError"
