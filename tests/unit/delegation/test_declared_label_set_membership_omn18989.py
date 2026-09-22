# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18989: a declared label set becomes a provider-native constraint.

**Half of this ticket was already shipped, and this module pins it rather than
reimplementing it.** The quality gate validates a declared ``response_contract``
with a real JSON-Schema validator, so an ``enum`` is already enforced on the way
back and the violation reason already names the offending token. Measured, not
assumed — see :func:`test_the_inbound_membership_check_already_exists`. Building
a second membership check beside it would be the duplication this codebase
keeps paying for.

What is missing is the OUTBOUND half, and the codebase already names it. The
request model deliberately refuses ``response_format: json_schema``, with a
sound reason: forwarding a directive to a provider that ignores it lets a
caller believe it constrained the response when it did not. The rendering
module records the same thing and names the missing piece exactly — a
capability field on the binding contract — warning that asserting the directive
against a backend that lacks support turns a gradeable near-miss into an HTTP
400.

So the constraint is threaded only where a backend DECLARES support, and
declaring it is evidence-backed rather than optimistic.

**The defect this closes**, reproduced three times on 2026-09-21 against the
live lab endpoint. A prompt asked in prose for one of ``HEALTH_SIGNAL`` or
``INSTRUMENTATION_DEFECT``; the model returned ``INSTRUMENTEMENT_DEFECT`` and
the run terminalised completed at quality 1.0 against a 0.8 bar. Correlations
``a75a211c-0828-4714-ad7e-610f226529fe`` and
``582e957c-d2bf-49c8-83e7-5e545d0e7765``, plus a direct probe. The same prompt
with the two labels declared as a JSON-Schema ``enum`` returned valid members
only.

**The limit, stated plainly:** this closes the vocabulary defect and not the
wrong-in-vocabulary defect. A label outside the declared set becomes
mechanically impossible; a wrong label that IS in the set stays invisible to
every check here and always will.
"""

from __future__ import annotations

import pytest

from omnimarket.delegation.response_contract_conformance import (
    schema_violation_reasons,
)
from omnimarket.delegation.structured_output import (
    provider_response_format_for_contract,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)

#: The two labels the reproduced prompt declared, in prose, and the corruption
#: the model returned in their place. Captured verbatim.
LABELS: tuple[str, ...] = ("HEALTH_SIGNAL", "INSTRUMENTATION_DEFECT")
GARBLED = "INSTRUMENTEMENT_DEFECT"

#: A caller-declared contract over exactly that closed set.
CLASSIFIER_CONTRACT: dict[str, object] = {
    "type": "object",
    "properties": {
        "deployed_revision": {"type": "string", "enum": list(LABELS)},
        "probe_generation_bound": {"type": "string", "enum": list(LABELS)},
    },
    "required": ["deployed_revision", "probe_generation_bound"],
    "additionalProperties": False,
}


def _backend(*, supports: bool) -> ModelDelegationBackendConfig:
    return ModelDelegationBackendConfig(
        backend_id="local-heavy-reasoning",
        provider="local",
        model_name="Qwen3.8-27B",
        tier="local",
        supports_response_format_json_schema=supports,
    )


# ---------------------------------------------------------------------------
# Already shipped — pinned, not rebuilt
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_inbound_membership_check_already_exists() -> None:
    """The captured garbled token is already refused, and the reason names it.

    This is the measurement that shrank this ticket. If it ever goes red, the
    outbound work below stops being sufficient and a gate-side membership
    check becomes real work again.
    """

    reasons = schema_violation_reasons(
        {"deployed_revision": GARBLED, "probe_generation_bound": LABELS[0]},
        CLASSIFIER_CONTRACT,
    )

    assert reasons, "an out-of-vocabulary label produced no violation"
    assert any(GARBLED in reason for reason in reasons), (
        "the violation does not name the offending token, so a reader cannot "
        f"tell which label failed; reasons={reasons!r}"
    )


@pytest.mark.unit
def test_a_valid_member_is_not_refused() -> None:
    """The other direction: the check is not a new way to fail correct work."""

    assert (
        schema_violation_reasons(
            {
                "deployed_revision": LABELS[0],
                "probe_generation_bound": LABELS[1],
            },
            CLASSIFIER_CONTRACT,
        )
        == []
    )


@pytest.mark.unit
def test_a_case_only_difference_is_refused() -> None:
    """`health_signal` is not `HEALTH_SIGNAL`, and the set is closed."""

    reasons = schema_violation_reasons(
        {
            "deployed_revision": LABELS[0].lower(),
            "probe_generation_bound": LABELS[1],
        },
        CLASSIFIER_CONTRACT,
    )
    assert reasons


# ---------------------------------------------------------------------------
# The outbound half — the work
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_declaring_backend_gets_the_provider_native_enum() -> None:
    """The declared contract becomes a provider-native structured-output body."""

    response_format = provider_response_format_for_contract(
        backend=_backend(supports=True),
        response_contract=CLASSIFIER_CONTRACT,
    )

    assert response_format is not None
    assert response_format["type"] == "json_schema"
    schema = response_format["json_schema"]
    assert isinstance(schema, dict)
    assert schema["schema"] == CLASSIFIER_CONTRACT, (
        "the schema sent to the provider must be the caller's declared "
        "contract verbatim; a rewritten one constrains something the caller "
        "did not ask for and the gate will not grade"
    )


@pytest.mark.unit
def test_a_backend_that_does_not_declare_support_gets_nothing() -> None:
    """Fail safe. An undeclared backend is treated as unsupporting.

    Asserting the directive at a provider that lacks it turns a gradeable
    near-miss into an HTTP 400, which is strictly worse than the status quo.
    """

    assert (
        provider_response_format_for_contract(
            backend=_backend(supports=False),
            response_contract=CLASSIFIER_CONTRACT,
        )
        is None
    )


@pytest.mark.unit
def test_support_is_off_unless_declared() -> None:
    """Undeclared means unsupported, never silently assumed."""

    backend = ModelDelegationBackendConfig(
        backend_id="some-backend",
        provider="local",
        model_name="m",
        tier="local",
    )
    assert backend.supports_response_format_json_schema is False


@pytest.mark.unit
def test_no_declared_contract_changes_nothing() -> None:
    """A caller that declared no contract is unaffected, even on a declaring backend.

    This is the case the reproduced defect was in, and it must stay untouched
    here: there is no vocabulary to constrain, so inventing one would be a
    guess about what the caller wanted.
    """

    assert (
        provider_response_format_for_contract(
            backend=_backend(supports=True),
            response_contract=None,
        )
        is None
    )


@pytest.mark.unit
def test_the_lab_backend_declares_support_on_measured_evidence() -> None:
    """The one backend measured to honour the directive declares it.

    Measured 2026-09-21 on the live endpoint: the same prompt returned the
    garbled token as prose and valid members only under a json_schema enum.
    If this assertion is ever relaxed to cover a backend nobody probed, the
    HTTP-400 hazard the rendering module warns about comes back.
    """

    from omnimarket.delegation.structured_output import (
        load_backends_declaring_structured_output,
    )

    declaring = load_backends_declaring_structured_output()
    assert "local-heavy-reasoning" in declaring, (
        "the lab backend measured to support json_schema does not declare it, "
        f"so the constraint is never sent; declaring={sorted(declaring)!r}"
    )


@pytest.mark.unit
def test_the_capability_travels_onto_the_resolved_backend() -> None:
    """Consumer-first, and the reason the flag lives on the RESOLVED backend.

    The binding config is not what the call site holds; a resolved backend is,
    and an escalation swaps it. Reading the binding at the call site instead
    would let a request that climbed to a different rung carry the previous
    rung's capability, which is how a directive reaches a provider that never
    declared it.
    """

    from omnimarket.routing.delegation_backend_resolution import (
        ModelResolvedDelegationBackend,
    )

    declaring = ModelResolvedDelegationBackend(
        backend_id="local-heavy-reasoning",
        model_id="Qwen3.8-27B",
        endpoint_ref="http://example.invalid/v1/chat/completions",
        tier="local",
        max_tokens=1024,
        timeout_ms=1000,
        supports_response_format_json_schema=True,
    )
    assert declaring.supports_response_format_json_schema is True

    silent = ModelResolvedDelegationBackend(
        backend_id="some-other",
        model_id="m",
        endpoint_ref="http://example.invalid/v1/chat/completions",
        tier="local",
        max_tokens=1024,
        timeout_ms=1000,
    )
    assert silent.supports_response_format_json_schema is False, (
        "a resolved backend defaulted to declaring support, so the directive "
        "would reach a provider nobody probed"
    )


@pytest.mark.unit
def test_the_resolved_backend_and_the_helper_compose() -> None:
    """The two halves fit: a resolved declaring backend yields the constraint.

    This is the shape the dispatch seam builds, asserted without standing up
    the whole port, so a change to either half that breaks the pairing fails
    here rather than only in an integration run.
    """

    from omnimarket.routing.delegation_backend_resolution import (
        ModelResolvedDelegationBackend,
    )

    backend = ModelResolvedDelegationBackend(
        backend_id="local-heavy-reasoning",
        model_id="Qwen3.8-27B",
        endpoint_ref="http://example.invalid/v1/chat/completions",
        tier="local",
        max_tokens=1024,
        timeout_ms=1000,
        supports_response_format_json_schema=True,
    )

    response_format = provider_response_format_for_contract(
        backend=backend, response_contract=CLASSIFIER_CONTRACT
    )
    assert response_format is not None
    assert response_format["type"] == "json_schema"
