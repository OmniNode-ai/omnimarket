# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""L11 chain pair: the model is shown the contract it is graded against (OMN-18698).

Row L11 of the local-path MVP (`beta/GOAL.md`, ticket OMN-18700, delivered by
OMN-7942): *the served local model honours the response contract it is handed*.

The row's second measurement reattributed the cause: this was never a
model-quality finding. A declared ``response_contract`` reached the quality
gate and stopped there, so the model was graded against a schema it had never
been shown. Its own recorded reasoning read "We have no explicit schema.", it
guessed a key name the contract does not contain, and six rungs across three
providers failed the same gate.

The pair
--------
``test_golden_chain_...``
    A request declares a contract. The assertion is about the WIRE: the
    outbound system prompt the provider received carries the rendered
    instruction and names every required key. The model answers conformingly
    behind a reasoning preamble, the chain completes, and the caller is handed
    the OBJECT rather than the prose around it.

``test_error_chain_...``
    The same contract, and a model that answers with plausible, well-formed
    JSON under GUESSED key names -- the live failure, reproduced. The chain
    must fail, naming the missing keys, and must not hand a non-conforming
    value back as a success.

Why the wire assertion is the load-bearing one
----------------------------------------------
A conformance test alone passes in the world this row describes: a model that
happens to guess right is indistinguishable from one that was told. Reading
the instruction off the request the provider actually received is what
separates "graded against a schema" from "shown a schema".

Falsifier (OMN-18698 AC2): making
``compose_system_prompt_with_response_contract`` return its base prompt turns
the golden chain red at the wire assertion, and the error chain keeps its
meaning either way -- a guessed-key answer must fail whether or not the model
was instructed.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.delegation.response_contract_conformance import (
    schema_violation_reasons,
)
from omnimarket.inference.local_byok_credential_adapter import (
    register_local_byok_credential,
)
from tests.chains.local.harness import (
    PROVIDER_SLUG,
    LocalProviderStub,
    house_openrouter_rung,
    local_byok_catalogue,
    no_ambient_provider_credentials,
    run_local_delegation,
    use_local_store,
)

pytestmark = [pytest.mark.unit, pytest.mark.local_chain]

_CUSTOMER_KEY_VALUE = "sk-or-omn18698-l11-customer-value"

#: The declared contract. Small on purpose: the row is about whether the model
#: is SHOWN the schema, not about how elaborate a schema the gate can express.
RESPONSE_CONTRACT: dict[str, object] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["verdict", "confidence"],
    "additionalProperties": False,
}

#: A conforming answer, behind the untagged reasoning preamble the served model
#: actually emits. The preamble is part of the fixture, not noise: a gate that
#: parsed from character zero is what made a correct answer look wrong.
_CONFORMING_ANSWER = (
    "Let me think about this. The change looks correct to me.\n\n"
    '{"verdict": "pass", "confidence": 0.91}\n'
)

#: The live failure: well-formed JSON under key names the contract does not
#: contain. A model that was never shown the schema produces exactly this.
_GUESSED_KEY_ANSWER = (
    'We have no explicit schema.\n\n{"result": "pass", "score": 0.91}\n'
)


async def test_golden_chain_the_contract_reaches_the_model_and_the_caller_gets_the_object(
    provider_stub: LocalProviderStub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The schema is on the wire, and the terminal carries the object."""
    provider_stub.content = _CONFORMING_ANSWER
    with no_ambient_provider_credentials(monkeypatch):
        db_path = use_local_store(monkeypatch, tmp_path)
        local_byok_catalogue(monkeypatch, tmp_path, provider_stub.completions_url)
        house_openrouter_rung(monkeypatch)
        register_local_byok_credential(
            PROVIDER_SLUG, _CUSTOMER_KEY_VALUE, db_path=db_path
        )

        response = await run_local_delegation(
            prompt="review this diff and give a verdict",
            db_path=db_path,
            correlation_id=uuid4(),
            response_contract=RESPONSE_CONTRACT,
        )

    # -- the model was SHOWN the schema, read off the wire ------------------
    system_prompt = provider_stub.last_message("system")
    assert system_prompt, "the request carried no system message at all"
    for key in ("verdict", "confidence"):
        assert key in system_prompt, (
            f"the declared contract's required key {key!r} never reached the "
            "model; it would be graded against a schema it was not shown"
        )
    assert '"type": "object"' in system_prompt or "'type': 'object'" in system_prompt

    # -- and the run completed ---------------------------------------------
    assert response.status == "completed", response.error_message

    # -- the CALLER gets the object, not the prose around it ----------------
    returned = json.loads(response.response)
    assert returned == {"verdict": "pass", "confidence": 0.91}
    assert schema_violation_reasons(returned, RESPONSE_CONTRACT) == []
    assert "Let me think about this" not in response.response, (
        "the value graded and the value returned must be the same value; "
        "handing back the reasoning preamble leaves the caller unable to parse "
        "a response the gate just scored"
    )


async def test_error_chain_guessed_key_names_fail_rather_than_pass(
    provider_stub: LocalProviderStub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A well-formed answer under the wrong keys is a failure, named."""
    provider_stub.content = _GUESSED_KEY_ANSWER
    with no_ambient_provider_credentials(monkeypatch):
        db_path = use_local_store(monkeypatch, tmp_path)
        local_byok_catalogue(monkeypatch, tmp_path, provider_stub.completions_url)
        house_openrouter_rung(monkeypatch)
        register_local_byok_credential(
            PROVIDER_SLUG, _CUSTOMER_KEY_VALUE, db_path=db_path
        )

        response = await run_local_delegation(
            prompt="review this diff and give a verdict",
            db_path=db_path,
            correlation_id=uuid4(),
            response_contract=RESPONSE_CONTRACT,
        )

    assert response.status == "failed", (
        "a response that does not satisfy the declared contract must not "
        "terminate as a success"
    )
    assert response.quality_gate_passed is False

    # OMN-7942 moved the per-violation detail off the free-text gate reasons
    # and onto typed evidence: a schema-violating embedded value is a refusal
    # (never a response), so what is wrong with it is named on
    # ``output_refusal.contract_failure_reasons`` rather than in
    # ``quality_gates_failed``/``error_message``, which now carry only the
    # generic MALFORMED reason. See
    # ``test_a_schema_violating_embedded_value_fails_without_returning_raw_text``
    # in tests/unit/delegation/test_response_contract_reaches_the_model_omn7942.py
    # for the paired assertion this test must stay consistent with.
    assert response.output_refusal is not None
    assert response.output_refusal.reason == "no_schema_conforming_json"
    reported = " ".join(response.output_refusal.contract_failure_reasons)
    for key in ("verdict", "confidence"):
        assert key in reported, (
            f"the missing required key {key!r} is not named anywhere in the "
            f"typed refusal evidence; got {reported!r}"
        )

    # The non-conforming value is withheld, never handed back as a response:
    # a reader diagnosing the failure gets the typed evidence above, not the
    # model's raw prose.
    assert response.response == ""

    # The fixture is a genuine violation, not a shape the contract accepts.
    assert (
        schema_violation_reasons({"result": "pass", "score": 0.91}, RESPONSE_CONTRACT)
        != []
    )
