# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_typed_decision_effect over a fake transport (OMN-19432).

Every test resolves the backend from the COMMITTED routing contract, so a test
that passes here proves the contract entry, not a fixture copy of it. The fake
transport answers both the visibility read and the decision call and records
every request, so "nothing was sent" is asserted, not inferred from a result.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
import yaml
from omnibase_spi.protocols.services import ProtocolSecretStore

from omnimarket.config.service_endpoints import GITHUB_REST_URL
from omnimarket.inference.local_byok_credential_adapter import is_local_store_only_ref
from omnimarket.nodes.node_typed_decision_effect.handlers.handler_typed_decision import (
    HandlerTypedDecision,
)
from omnimarket.nodes.node_typed_decision_effect.models.model_typed_decision import (
    EnumTypedDecisionDecider,
    EnumTypedDecisionKind,
    EnumTypedDecisionReason,
    ModelTypedDecisionRequest,
    ModelTypedDecisionResult,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_BIFROST = _REPO_ROOT / "src/omnimarket/configs/bifrost_delegation.yaml"
_NODE_DIR = _REPO_ROOT / "src/omnimarket/nodes/node_typed_decision_effect"
_CONTRACT = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
_BACKEND_ID = _CONTRACT["decision_routing"]["backend_id"]
_THRESHOLD = _CONTRACT["decision_routing"]["abstain_below_probability"]
_BACKEND = next(
    b
    for b in yaml.safe_load(_BIFROST.read_text(encoding="utf-8"))["backends"]
    if b["backend_id"] == _BACKEND_ID
)
_FAKE_KEY = "fake-typesafe-key-for-tests"
_PUBLIC = "OmniNode-ai/public-repo"
_PRIVATE = "OmniNode-ai/private-repo"


class _Store:
    """A secret store holding only what the test gives it."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    async def get_secret(self, key: str) -> str | None:
        return self._values.get(key)


class _Recorder:
    """Fake transport: GitHub visibility reads plus the decision endpoint."""

    def __init__(self, decision: httpx.Response | Exception | None = None) -> None:
        self.decision = decision
        self.visibility_calls: list[httpx.Request] = []
        self.decision_calls: list[httpx.Request] = []
        self.visibility_error: Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(f"{GITHUB_REST_URL}/repos/"):
            self.visibility_calls.append(request)
            if self.visibility_error is not None:
                raise self.visibility_error
            if url.endswith(_PUBLIC):
                return httpx.Response(200, json={"private": False})
            return httpx.Response(404, json={"message": "Not Found"})
        if url == _BACKEND["endpoint_url"]:
            self.decision_calls.append(request)
            if isinstance(self.decision, Exception):
                raise self.decision
            assert self.decision is not None
            return self.decision
        raise AssertionError(f"unexpected request to {url}")


def _handler(
    recorder: _Recorder, *, key: str | None = _FAKE_KEY, tmp: Path
) -> HandlerTypedDecision:
    values = {_BACKEND["secret_ref"]: key} if key is not None else {}
    return HandlerTypedDecision(
        transport=httpx.MockTransport(recorder),
        secret_store=cast(ProtocolSecretStore, _Store(values)),
        bifrost_config_path=_BIFROST,
        bifrost_overlay_path=tmp / "no-overlay.yaml",
    )


def _choice(
    repo: str | None = _PUBLIC, *, incumbent_answer: str | None = "infrastructure"
) -> ModelTypedDecisionRequest:
    return ModelTypedDecisionRequest(
        correlation_id=uuid4(),
        work_unit_repository=repo,
        state={"ci_failure": "ruff format check failed on two files"},
        kind=EnumTypedDecisionKind.CHOICE,
        instructions="Is this CI failure caused by the change, or is it infrastructure?",
        criteria={
            "change": "The diff caused it",
            "infrastructure": "Runner or network",
        },
        incumbent_answer=incumbent_answer,
    )


def _choice_answer(
    choice: str, p: float, *, model: str = "jev-9.9.9"
) -> httpx.Response:
    other = "infrastructure" if choice == "change" else "change"
    return httpx.Response(
        200,
        json={
            "model": model,
            "answers": {
                "decision": {
                    "type": "choice",
                    "choice": choice,
                    "probabilities": {choice: p, other: 1.0 - p},
                    "confidence": p,
                }
            },
            "usage": {"input_tokens": 300, "output_tokens": 20},
        },
    )


@pytest.mark.unit
def test_the_backend_is_declared_by_reference_and_resolves_local_store_only() -> None:
    assert _BACKEND["provider"] == "typesafe"
    assert _BACKEND["tier"] == "typed_decision"
    assert "api_key_env" not in _BACKEND
    assert is_local_store_only_ref(_BACKEND["secret_ref"])


@pytest.mark.unit
def test_the_model_decides_and_the_request_is_built_from_the_routing_contract(
    tmp_path: Path,
) -> None:
    recorder = _Recorder(_choice_answer("change", 0.93))
    result = _handler(recorder, tmp=tmp_path).handle(_choice())

    assert result.decided_by is EnumTypedDecisionDecider.MODEL
    assert result.answer == "change"
    assert result.reason is None
    assert result.probability == pytest.approx(0.93)
    assert result.served_model == "jev-9.9.9"
    assert result.requested_model == _BACKEND["model_name"]
    assert result.backend_id == _BACKEND_ID
    assert (result.input_tokens, result.output_tokens) == (300, 20)

    (call,) = recorder.decision_calls
    assert call.headers["Authorization"] == f"Bearer {_FAKE_KEY}"
    body = json.loads(call.content)
    assert body["model"] == _BACKEND["model_name"]
    assert body["state"] == {"ci_failure": "ruff format check failed on two files"}
    assert body["questions"]["decision"]["type"] == "choice"
    assert set(body["questions"]["decision"]["criteria"]) == {
        "change",
        "infrastructure",
    }
    # The key never appears in the result.
    assert _FAKE_KEY not in result.model_dump_json()


@pytest.mark.unit
def test_public_is_admitted_and_private_is_refused_in_the_same_run(
    tmp_path: Path,
) -> None:
    """Positive control: a refuse-everything bug cannot pass this test."""
    recorder = _Recorder(_choice_answer("change", 0.95))
    handler = _handler(recorder, tmp=tmp_path)

    admitted = handler.handle(_choice(_PUBLIC))
    refused = handler.handle(_choice(_PRIVATE))

    assert admitted.decided_by is EnumTypedDecisionDecider.MODEL
    assert refused.decided_by is EnumTypedDecisionDecider.INCUMBENT_REFUSED
    assert refused.reason is EnumTypedDecisionReason.REPOSITORY_NOT_PUBLIC
    assert refused.answer == "infrastructure"
    assert len(recorder.visibility_calls) == 2
    # Exactly one decision call: the public one. The private request sent nothing.
    assert len(recorder.decision_calls) == 1
    # No GitHub token resolves from this store, so the read is anonymous. With a
    # token the read is authenticated and still admits only ``private: false``
    # (test_visibility_guard.py).
    assert all("Authorization" not in c.headers for c in recorder.visibility_calls)


@pytest.mark.unit
def test_a_visibility_resolution_failure_is_a_refusal_and_sends_nothing(
    tmp_path: Path,
) -> None:
    recorder = _Recorder(_choice_answer("change", 0.95))
    recorder.visibility_error = httpx.ConnectTimeout("timed out")

    result = _handler(recorder, tmp=tmp_path).handle(_choice())

    assert result.decided_by is EnumTypedDecisionDecider.INCUMBENT_REFUSED
    assert result.reason is EnumTypedDecisionReason.REPOSITORY_VISIBILITY_UNRESOLVED
    assert recorder.decision_calls == []


@pytest.mark.unit
def test_no_repository_attribution_is_refused_with_no_network_call(
    tmp_path: Path,
) -> None:
    recorder = _Recorder(_choice_answer("change", 0.95))

    result = _handler(recorder, tmp=tmp_path).handle(_choice(None))

    assert result.reason is EnumTypedDecisionReason.NO_REPOSITORY_ATTRIBUTION
    assert recorder.visibility_calls == []
    assert recorder.decision_calls == []


@pytest.mark.unit
def test_a_key_absent_from_the_store_refuses_and_never_reads_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("TYPESAFE_API_KEY", "LLM_TYPESAFE_API_KEY", "llm.typesafe.api_key"):
        monkeypatch.setenv(name, "an-environment-key-that-must-not-be-used")
    recorder = _Recorder(_choice_answer("change", 0.95))

    result = _handler(recorder, key=None, tmp=tmp_path).handle(_choice())

    assert result.decided_by is EnumTypedDecisionDecider.INCUMBENT_REFUSED
    assert result.reason is EnumTypedDecisionReason.CREDENTIAL_NOT_REGISTERED
    assert result.answer == "infrastructure"
    assert recorder.decision_calls == []


@pytest.mark.unit
def test_a_blind_low_probability_answer_has_no_answer(tmp_path: Path) -> None:
    below = round(_THRESHOLD - 0.05, 4)
    recorder = _Recorder(_choice_answer("change", below))

    result = _handler(recorder, tmp=tmp_path).handle(_choice(incumbent_answer=None))

    assert result.decided_by is EnumTypedDecisionDecider.NO_ANSWER
    assert result.answer is None
    assert result.reason is EnumTypedDecisionReason.BELOW_ABSTENTION_THRESHOLD
    assert result.model_answer == "change"
    assert result.probability == pytest.approx(below)


@pytest.mark.unit
def test_a_blind_private_repository_request_has_no_answer(tmp_path: Path) -> None:
    recorder = _Recorder()

    result = _handler(recorder, tmp=tmp_path).handle(
        _choice(_PRIVATE, incumbent_answer=None)
    )

    assert result.decided_by is EnumTypedDecisionDecider.NO_ANSWER
    assert result.answer is None
    assert result.reason is EnumTypedDecisionReason.REPOSITORY_NOT_PUBLIC
    assert result.model_answer is None
    assert recorder.decision_calls == []


@pytest.mark.unit
def test_a_blind_confident_answer_is_the_models_answer(tmp_path: Path) -> None:
    recorder = _Recorder(_choice_answer("change", 0.93))
    request = ModelTypedDecisionRequest.model_validate(
        _choice().model_dump(exclude={"incumbent_answer"})
    )

    result = _handler(recorder, tmp=tmp_path).handle(request)

    assert request.incumbent_answer is None
    assert result.decided_by is EnumTypedDecisionDecider.MODEL
    assert result.answer == "change"
    assert result.reason is None
    assert result.model_answer == "change"
    assert len(recorder.decision_calls) == 1


@pytest.mark.unit
def test_a_blind_backend_failure_has_no_answer(tmp_path: Path) -> None:
    recorder = _Recorder(httpx.ReadTimeout("slow"))

    result = _handler(recorder, tmp=tmp_path).handle(_choice(incumbent_answer=None))

    assert result.decided_by is EnumTypedDecisionDecider.NO_ANSWER
    assert result.answer is None
    assert result.reason is EnumTypedDecisionReason.BACKEND_TRANSPORT_ERROR
    assert result.model_answer is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "criteria", "invalid_incumbent"),
    [
        ("choice", {"a": None, "b": None}, "c"),
        ("score", ["low", "high"], "2"),
        ("noul", None, "maybe"),
    ],
)
def test_incumbent_is_optional_but_validated_when_present(
    kind: str, criteria: Any, invalid_incumbent: str
) -> None:
    data = {
        **_choice().model_dump(exclude={"incumbent_answer"}),
        "kind": kind,
        "criteria": criteria,
    }
    assert ModelTypedDecisionRequest.model_validate(data).incumbent_answer is None
    assert (
        ModelTypedDecisionRequest.model_validate(
            {**data, "incumbent_answer": None}
        ).incumbent_answer
        is None
    )
    with pytest.raises(ValueError, match="incumbent_answer"):
        ModelTypedDecisionRequest.model_validate(
            {**data, "incumbent_answer": invalid_incumbent}
        )
    with pytest.raises(ValueError, match="at least 1 character"):
        ModelTypedDecisionRequest.model_validate({**data, "incumbent_answer": ""})


@pytest.mark.unit
@pytest.mark.parametrize(
    "decider",
    [
        "model",
        "incumbent_abstained",
        "incumbent_refused",
        "incumbent_backend_error",
        "no_answer",
    ],
)
@pytest.mark.parametrize("answer", [None, "change"])
def test_result_has_no_answer_exactly_when_no_answer_decided(
    decider: str, answer: str | None
) -> None:
    data = {
        "correlation_id": uuid4(),
        "decided_by": decider,
        "answer": answer,
        "abstain_below_probability": _THRESHOLD,
        "backend_id": _BACKEND_ID,
    }
    if (answer is None) == (decider == "no_answer"):
        assert ModelTypedDecisionResult.model_validate(data).answer == answer
    else:
        with pytest.raises(ValueError, match="answer"):
            ModelTypedDecisionResult.model_validate(data)


@pytest.mark.unit
def test_a_schema_valid_low_probability_answer_abstains_to_the_incumbent(
    tmp_path: Path,
) -> None:
    below = round(_THRESHOLD - 0.05, 4)
    recorder = _Recorder(_choice_answer("change", below))

    result = _handler(recorder, tmp=tmp_path).handle(_choice())

    assert result.decided_by is EnumTypedDecisionDecider.INCUMBENT_ABSTAINED
    assert result.reason is EnumTypedDecisionReason.BELOW_ABSTENTION_THRESHOLD
    assert result.answer == "infrastructure"
    # The model's own answer is kept for the receipt, never used.
    assert result.model_answer == "change"
    assert result.abstain_below_probability == _THRESHOLD


@pytest.mark.unit
@pytest.mark.parametrize(
    ("decision", "reason"),
    [
        (
            httpx.Response(401, json={"detail": "bad key"}),
            EnumTypedDecisionReason.BACKEND_HTTP_ERROR,
        ),
        (
            httpx.Response(529, json={"detail": "overloaded"}),
            EnumTypedDecisionReason.BACKEND_HTTP_ERROR,
        ),
        (httpx.ReadTimeout("slow"), EnumTypedDecisionReason.BACKEND_TRANSPORT_ERROR),
        (
            _choice_answer("neither", 0.99),
            EnumTypedDecisionReason.BACKEND_MALFORMED_RESPONSE,
        ),
        (
            httpx.Response(200, json={"answers": {}}),
            EnumTypedDecisionReason.BACKEND_MALFORMED_RESPONSE,
        ),
    ],
)
def test_a_backend_failure_hands_the_decision_to_the_incumbent(
    tmp_path: Path,
    decision: httpx.Response | Exception,
    reason: EnumTypedDecisionReason,
) -> None:
    recorder = _Recorder(decision)

    result = _handler(recorder, tmp=tmp_path).handle(_choice())

    assert result.decided_by is EnumTypedDecisionDecider.INCUMBENT_BACKEND_ERROR
    assert result.reason is reason
    assert result.answer == "infrastructure"
    assert result.model_answer is None


@pytest.mark.unit
def test_a_noul_answer_maps_to_true_or_false_with_its_own_probability(
    tmp_path: Path,
) -> None:
    recorder = _Recorder(
        httpx.Response(
            200,
            json={
                "model": "jev-9.9.9",
                "answers": {"decision": {"type": "noul", "noul": 0.08}},
            },
        )
    )
    request = ModelTypedDecisionRequest(
        correlation_id=uuid4(),
        work_unit_repository=_PUBLIC,
        state="The retry succeeded on the same head with no change.",
        kind=EnumTypedDecisionKind.NOUL,
        instructions="Did the change under review cause the failure?",
        incumbent_answer="true",
    )

    result = _handler(recorder, tmp=tmp_path).handle(request)

    assert result.decided_by is EnumTypedDecisionDecider.MODEL
    assert result.answer == "false"
    assert result.probability == pytest.approx(0.92)
    assert (
        "criteria"
        not in json.loads(recorder.decision_calls[0].content)["questions"]["decision"]
    )


@pytest.mark.unit
def test_a_score_answer_is_its_most_probable_level(tmp_path: Path) -> None:
    recorder = _Recorder(
        httpx.Response(
            200,
            json={
                "model": "jev-9.9.9",
                "answers": {
                    "decision": {
                        "type": "score",
                        "score": 1.9,
                        "legend": {"0": "low", "1": "medium", "2": "high"},
                        "probabilities": {"0": 0.0, "1": 0.1, "2": 0.9},
                        "confidence": 0.85,
                    }
                },
            },
        )
    )
    request = ModelTypedDecisionRequest(
        correlation_id=uuid4(),
        work_unit_repository=_PUBLIC,
        state={"finding": "SQL built by string concatenation from request input"},
        kind=EnumTypedDecisionKind.SCORE,
        instructions="How severe is this review finding?",
        criteria=["low", "medium", "high"],
        incumbent_answer="1",
    )

    result = _handler(recorder, tmp=tmp_path).handle(request)

    assert (result.decided_by, result.answer) == (EnumTypedDecisionDecider.MODEL, "2")
    assert result.score == pytest.approx(1.9)


@pytest.mark.unit
def test_no_endpoint_model_or_key_literal_lives_in_the_node_source() -> None:
    """The backend resolves only from the routing contract. The positive
    control proves the same search finds the literals where they DO live, so an
    empty result here cannot be a broken search."""
    literals = (
        _BACKEND["endpoint_url"],
        _BACKEND["model_name"],
        _BACKEND["secret_ref"],
        "api.typesafe.ai",
    )

    def hits(paths: list[Path]) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for path in paths:
            text = path.read_text(encoding="utf-8")
            found.extend((str(path), lit) for lit in literals if lit in text)
        return found

    node_sources = sorted(_NODE_DIR.rglob("*.py"))
    assert node_sources, "the search found no node source at all"
    assert hits(node_sources) == []
    assert {lit for _, lit in hits([_BIFROST])} == set(literals)


@pytest.mark.unit
def test_a_request_that_does_not_fit_its_kind_is_rejected() -> None:
    base: dict[str, Any] = {
        "correlation_id": uuid4(),
        "work_unit_repository": _PUBLIC,
        "state": "x",
        "instructions": "q",
    }
    with pytest.raises(ValueError, match="incumbent_answer must be one of"):
        ModelTypedDecisionRequest(
            **base, kind="choice", criteria={"a": None, "b": None}, incumbent_answer="c"
        )
    with pytest.raises(ValueError, match="2 to 10 levels"):
        ModelTypedDecisionRequest(
            **base, kind="score", criteria=["only"], incumbent_answer="0"
        )
    with pytest.raises(ValueError, match="owner/name"):
        ModelTypedDecisionRequest(
            **{**base, "work_unit_repository": "not a slug"},
            kind="noul",
            incumbent_answer="true",
        )
