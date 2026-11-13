# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local decision engine behind node_typed_decision_effect (OMN-18850).

The engine is an overlay value: ``engine: local`` sends the question to the
routing-contract backend the overlay names, constrained to the offered options by
guided decoding, and returns per-option probabilities in the result shape the Jev
engine returns. Every test resolves the local backend from the COMMITTED routing
contract plus a bifrost overlay written in ``tmp_path``, and reads the engine
switch from an overlay file or an explicit overlay, so the lab's real overlay
roots are never read. The fake transport records every call per URL, so "the other
engine was never asked" is asserted, not inferred.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
import yaml
from omnibase_spi.protocols.services import ProtocolSecretStore

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_typed_decision_effect.handlers.handler_local_decision_engine import (
    OPTION_CODES,
    answer_payload,
    build_request_body,
)
from omnimarket.nodes.node_typed_decision_effect.handlers.handler_typed_decision import (
    HandlerTypedDecision,
    HandlerTypedDecisionWorkflow,
)
from omnimarket.nodes.node_typed_decision_effect.handlers.typed_decision_event import (
    InMemoryTypedDecisionEventPublisher,
)
from omnimarket.nodes.node_typed_decision_effect.models.model_typed_decision import (
    EnumTypedDecisionDecider,
    EnumTypedDecisionKind,
    EnumTypedDecisionReason,
    ModelTypedDecisionRequest,
    ModelTypedDecisionResult,
    ModelTypedDecisionWorkflowRequest,
)
from omnimarket.nodes.node_typed_decision_effect.models.model_typed_decision_engine import (
    OVERLAY_ID,
    EnumTypedDecisionEngine,
    ModelTypedDecisionEngineOverlay,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_BIFROST = _REPO_ROOT / "src/omnimarket/configs/bifrost_delegation.yaml"
_NODE_DIR = _REPO_ROOT / "src/omnimarket/nodes/node_typed_decision_effect"
_CONTRACT = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
_JEV_ID = _CONTRACT["decision_routing"]["backend_id"]
_JEV = next(
    b
    for b in yaml.safe_load(_BIFROST.read_text(encoding="utf-8"))["backends"]
    if b["backend_id"] == _JEV_ID
)
_LOCAL_ID = "local-coder"
_LOCAL_URL = "http://lab-model.test/v1/chat/completions"
_LOCAL_THRESHOLD = 0.5
_REPO = "OmniNode-ai/code-repo"


class _Store:
    async def get_secret(self, key: str) -> str | None:
        return "fake-jev-key-for-tests" if key == _JEV["secret_ref"] else None


class _Transport:
    """Records every call per URL; answers the local server and the Jev backend."""

    def __init__(self, local: httpx.Response | Exception | None = None) -> None:
        self.local = local
        self.local_calls: list[dict[str, Any]] = []
        self.jev_calls: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == _LOCAL_URL:
            self.local_calls.append(json.loads(request.content))
            if isinstance(self.local, Exception):
                raise self.local
            assert self.local is not None
            return self.local
        if url == _JEV["endpoint_url"]:
            self.jev_calls.append(json.loads(request.content))
            return _jev_answer("change", 0.97)
        raise AssertionError(f"unexpected request to {url}")


def _jev_answer(choice: str, p: float) -> httpx.Response:
    other = "infrastructure" if choice == "change" else "change"
    return httpx.Response(
        200,
        json={
            "model": "jev-9.9.9",
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


def _vllm(
    logprobs: dict[str, float], *, model: str = "served-local-9"
) -> httpx.Response:
    """A vLLM chat completion whose first token carries ``logprobs`` per code."""
    top = [
        {"token": code, "logprob": value, "bytes": [ord(code)]}
        for code, value in logprobs.items()
    ]
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": next(iter(logprobs))},
                    "logprobs": {
                        "content": [{"token": top[0]["token"], "top_logprobs": top}]
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 41, "completion_tokens": 2},
        },
    )


def _vllm_reasoning(logprobs: dict[str, float]) -> httpx.Response:
    """A reasoning model's reply: unconstrained reasoning tokens, then the guided answer."""
    reasoning = [
        {
            "token": word,
            "logprob": -0.1,
            "top_logprobs": [
                {"token": word, "logprob": -0.1},
                {"token": "B", "logprob": -0.2},
                {"token": "A", "logprob": -9.0},
            ],
        }
        for word in ("We", " need", " B", " is", " odd")
    ]
    answer = {
        "token": next(iter(logprobs)),
        "logprob": next(iter(logprobs.values())),
        "top_logprobs": [
            {"token": code, "logprob": value} for code, value in logprobs.items()
        ],
    }
    stop = {"token": "<|im_end|>", "logprob": 0.0, "top_logprobs": []}
    return httpx.Response(
        200,
        json={
            "model": "served-local-9",
            "choices": [
                {
                    "message": {"role": "assistant", "content": answer["token"]},
                    "logprobs": {"content": [*reasoning, answer, stop]},
                }
            ],
            "usage": {"prompt_tokens": 90, "completion_tokens": 40},
        },
    )


def _p(*probabilities: float) -> dict[str, float]:
    """Logprobs by code for the given probabilities, in code order."""
    return {
        code: math.log(p) for code, p in zip(OPTION_CODES, probabilities, strict=False)
    }


def _bifrost_overlay(tmp: Path, backend_id: str = _LOCAL_ID) -> Path:
    path = tmp / "bifrost_overlay.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "backends": [
                    {
                        "backend_id": backend_id,
                        "endpoint_url": _LOCAL_URL,
                        "model_name": "test-model-9",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _local_overlay(
    backend_id: str = _LOCAL_ID, threshold: float = _LOCAL_THRESHOLD
) -> ModelTypedDecisionEngineOverlay:
    return ModelTypedDecisionEngineOverlay(
        engine=EnumTypedDecisionEngine.LOCAL,
        local_backend_id=backend_id,
        local_abstain_below_probability=threshold,
    )


def _write_engine_file(tmp: Path, document: dict[str, Any]) -> dict[str, str]:
    """Write the engine overlay under a root; return the environment naming it."""
    root = tmp / "roots"
    (root / OVERLAY_ID).mkdir(parents=True, exist_ok=True)
    (root / OVERLAY_ID / "overlay.yaml").write_text(
        yaml.safe_dump(document), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def _no_overlay_env(tmp: Path) -> dict[str, str]:
    root = tmp / "empty-roots"
    root.mkdir(exist_ok=True)
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def _handler(
    transport: _Transport,
    tmp: Path,
    *,
    engine_overlay: ModelTypedDecisionEngineOverlay | None = None,
    environ: dict[str, str] | None = None,
    publisher: InMemoryTypedDecisionEventPublisher | None = None,
    bifrost_backend_id: str = _LOCAL_ID,
) -> HandlerTypedDecision:
    return HandlerTypedDecision(
        transport=httpx.MockTransport(transport),
        secret_store=cast(ProtocolSecretStore, _Store()),
        bifrost_config_path=_BIFROST,
        bifrost_overlay_path=_bifrost_overlay(tmp, bifrost_backend_id),
        event_publisher=publisher,
        engine_overlay=engine_overlay,
        overlay_environ=environ if environ is not None else _no_overlay_env(tmp),
    )


def _choice(
    *,
    repo: str | None = _REPO,
    incumbent: str | None = "infrastructure",
    options: int = 2,
) -> ModelTypedDecisionRequest:
    labels = ["change", "infrastructure"] + [f"extra_{i}" for i in range(options - 2)]
    return ModelTypedDecisionRequest(
        correlation_id=uuid4(),
        work_unit_repository=repo,
        state={"ci_failure": "ruff format check failed on two files"},
        kind=EnumTypedDecisionKind.CHOICE,
        instructions="Is this CI failure caused by the change, or is it infrastructure?",
        criteria={label: f"description of {label}" for label in labels},
        incumbent_answer=incumbent,
    )


def _noul(incumbent: str | None = "true") -> ModelTypedDecisionRequest:
    return ModelTypedDecisionRequest(
        correlation_id=uuid4(),
        work_unit_repository=_REPO,
        state={"checks": "green"},
        kind=EnumTypedDecisionKind.NOUL,
        instructions="Is the pull request ready to land?",
        incumbent_answer=incumbent,
    )


def _score(incumbent: str | None = "1") -> ModelTypedDecisionRequest:
    return ModelTypedDecisionRequest(
        correlation_id=uuid4(),
        work_unit_repository=_REPO,
        state={"diff_lines": 40},
        kind=EnumTypedDecisionKind.SCORE,
        instructions="How risky is the change?",
        criteria=["low", "medium", "high", "critical"],
        incumbent_answer=incumbent,
    )


# ------------------------------------------------------------------- golden chain


@pytest.mark.unit
def test_the_local_engine_answers_a_choice_constrained_to_the_labels(
    tmp_path: Path,
) -> None:
    transport = _Transport(_vllm(_p(0.8, 0.2)))
    result = _handler(transport, tmp_path, engine_overlay=_local_overlay()).handle(
        _choice()
    )

    assert result.decided_by is EnumTypedDecisionDecider.MODEL
    assert result.answer == "change"
    assert result.model_answer == "change"
    assert result.reason is None
    assert result.probabilities == pytest.approx({"change": 0.8, "infrastructure": 0.2})
    assert sum(result.probabilities.values()) == pytest.approx(1.0)
    assert result.probability == pytest.approx(0.8)
    assert result.backend_id == _LOCAL_ID
    assert result.requested_model == "test-model-9"
    assert result.served_model == "served-local-9"
    assert (result.input_tokens, result.output_tokens) == (41, 2)
    assert result.abstain_below_probability == _LOCAL_THRESHOLD
    assert transport.jev_calls == []
    (body,) = transport.local_calls
    assert body["structured_outputs"] == {"choice": ["A", "B"]}
    assert "guided_choice" not in body
    assert body["logprobs"] is True
    assert body["top_logprobs"] == 2
    prompt = body["messages"][0]["content"]
    assert "change" in prompt
    assert "infrastructure - description of infrastructure" in prompt


@pytest.mark.unit
def test_a_shadow_decision_carries_the_local_engines_receipt(tmp_path: Path) -> None:
    """The seam with the shadow workflow: the incumbent decides, the receipt is local."""
    transport = _Transport(_vllm(_p(0.9, 0.1)))
    workflow = HandlerTypedDecisionWorkflow(
        decision_handler=_handler(transport, tmp_path, engine_overlay=_local_overlay())
    )
    out = workflow.handle(
        ModelTypedDecisionWorkflowRequest(decision=_choice(incumbent="infrastructure"))
    )

    receipt = out.decision
    assert receipt is not None
    assert receipt.decided_by is EnumTypedDecisionDecider.INCUMBENT_SHADOW
    assert receipt.shadow_decided_by is EnumTypedDecisionDecider.MODEL
    assert receipt.answer == "infrastructure"
    assert receipt.model_answer == "change"
    assert receipt.backend_id == _LOCAL_ID
    assert receipt.probabilities == pytest.approx(
        {"change": 0.9, "infrastructure": 0.1}
    )


@pytest.mark.unit
def test_a_local_decision_leaves_a_delegation_terminal_labelled_local(
    tmp_path: Path,
) -> None:
    publisher = InMemoryTypedDecisionEventPublisher()
    handler = _handler(
        _Transport(_vllm(_p(0.8, 0.2))),
        tmp_path,
        engine_overlay=_local_overlay(),
        publisher=publisher,
    )
    handler.handle(_choice())
    (event,) = publisher.events
    assert event.payload["provider"] == _CONTRACT["delegation_event"]["local_provider"]
    assert event.payload["provider"] != _CONTRACT["delegation_event"]["provider"]
    assert event.payload["backend_id"] == _LOCAL_ID

    jev_publisher = InMemoryTypedDecisionEventPublisher()
    _handler(
        _Transport(),
        tmp_path,
        engine_overlay=ModelTypedDecisionEngineOverlay(
            engine=EnumTypedDecisionEngine.JEV
        ),
        publisher=jev_publisher,
    ).handle(_choice())
    (jev_event,) = jev_publisher.events
    assert jev_event.payload["provider"] == _CONTRACT["delegation_event"]["provider"]


# ------------------------------------------------- failing controls: the typed laws


@pytest.mark.unit
def test_a_choice_outside_the_labels_is_impossible(tmp_path: Path) -> None:
    """The model puts its mass on a code that was not offered: the answer stays a label."""
    # the sampled token is offered; the top list still leaks mass on a code that was not
    reply = _vllm({"A": math.log(0.06), "C": math.log(0.9), "B": math.log(0.04)})
    result = _handler(
        _Transport(reply), tmp_path, engine_overlay=_local_overlay()
    ).handle(_choice())

    assert result.answer in {"change", "infrastructure"}
    assert result.model_answer in {"change", "infrastructure"}
    assert set(result.probabilities) == {"change", "infrastructure"}
    assert result.probabilities == pytest.approx({"change": 0.6, "infrastructure": 0.4})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("request_factory", "logprobs", "expected_keys"),
    [
        (
            lambda: _choice(options=3),
            _p(0.3, 0.2, 0.1),
            {"change", "infrastructure", "extra_0"},
        ),
        (lambda: _score(), _p(0.1, 0.2, 0.1, 0.1), {"0", "1", "2", "3"}),
        (lambda: _noul(), _p(0.3, 0.3), {"true", "false"}),
    ],
    ids=["choice", "score", "noul"],
)
def test_probabilities_sum_to_one_whatever_the_raw_mass(
    tmp_path: Path,
    request_factory: Any,
    logprobs: dict[str, float],
    expected_keys: set[str],
) -> None:
    """The raw first-token mass is below 1; the receipt is renormalised over the options."""
    assert math.fsum(math.exp(v) for v in logprobs.values()) < 0.7
    result = _handler(
        _Transport(_vllm(logprobs)), tmp_path, engine_overlay=_local_overlay()
    ).handle(request_factory())

    assert set(result.probabilities) == expected_keys
    assert math.fsum(result.probabilities.values()) == pytest.approx(1.0)
    assert all(0.0 <= p <= 1.0 for p in result.probabilities.values())


@pytest.mark.unit
def test_the_answer_token_is_read_after_the_reasoning_tokens(tmp_path: Path) -> None:
    """A reasoning model emits unconstrained tokens first; the answer is the guided one."""
    reply = _vllm_reasoning({"A": math.log(0.9), "B": math.log(0.1)})
    result = _handler(
        _Transport(reply), tmp_path, engine_overlay=_local_overlay()
    ).handle(_choice())
    assert result.model_answer == "change"
    assert result.probabilities == pytest.approx({"change": 0.9, "infrastructure": 0.1})


@pytest.mark.unit
def test_the_overlays_request_options_ride_the_request_and_never_replace_a_field(
    tmp_path: Path,
) -> None:
    options = {"chat_template_kwargs": {"enable_thinking": False}}
    transport = _Transport(_vllm(_p(0.8, 0.2)))
    _handler(
        transport,
        tmp_path,
        engine_overlay=ModelTypedDecisionEngineOverlay(
            engine=EnumTypedDecisionEngine.LOCAL,
            local_backend_id=_LOCAL_ID,
            local_abstain_below_probability=_LOCAL_THRESHOLD,
            local_request_options=options,
        ),
    ).handle(_choice())
    (body,) = transport.local_calls
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["structured_outputs"] == {"choice": ["A", "B"]}
    with pytest.raises(ValueError, match="cannot replace"):
        ModelTypedDecisionEngineOverlay(
            engine=EnumTypedDecisionEngine.LOCAL,
            local_backend_id=_LOCAL_ID,
            local_abstain_below_probability=_LOCAL_THRESHOLD,
            local_request_options={"structured_outputs": {"choice": ["Z"]}},
        )
    with pytest.raises(ValueError, match="no local fields"):
        ModelTypedDecisionEngineOverlay(
            engine=EnumTypedDecisionEngine.JEV, local_request_options=options
        )


@pytest.mark.unit
def test_a_noul_and_a_score_answer_in_the_jev_shapes(tmp_path: Path) -> None:
    noul = _handler(
        _Transport(_vllm(_p(0.7, 0.3))), tmp_path, engine_overlay=_local_overlay()
    ).handle(_noul())
    assert noul.model_answer == "true"
    assert noul.probabilities == pytest.approx({"true": 0.7, "false": 0.3})

    score = _handler(
        _Transport(_vllm(_p(0.1, 0.2, 0.3, 0.4))),
        tmp_path,
        engine_overlay=_local_overlay(),
    ).handle(_score())
    assert score.model_answer == "3"
    assert score.score == pytest.approx(0 * 0.1 + 1 * 0.2 + 2 * 0.3 + 3 * 0.4)


@pytest.mark.unit
def test_the_mapping_is_pure_and_names_the_guided_decoding_field() -> None:
    request = _choice(incumbent="infrastructure")
    body = build_request_body(request, model_id="m", max_tokens=7)
    assert body["structured_outputs"] == {"choice": ["A", "B"]}
    assert (body["model"], body["max_tokens"]) == ("m", 7)
    assert "temperature" not in body
    # the incumbent is the caller's answer and never goes to the model as a hint
    assert "incumbent" not in json.dumps(body)
    payload = answer_payload(request, json.loads(_vllm(_p(0.25, 0.75)).content))
    assert payload["answers"]["decision"]["choice"] == "infrastructure"


# ---------------------------------------------------------- the overlay switch


@pytest.mark.unit
def test_the_overlay_switch_changes_the_engine_on_the_next_decision(
    tmp_path: Path,
) -> None:
    transport = _Transport(_vllm(_p(0.8, 0.2)))
    environ = _write_engine_file(
        tmp_path,
        {
            "engine": "local",
            "local_backend_id": _LOCAL_ID,
            "local_abstain_below_probability": _LOCAL_THRESHOLD,
        },
    )
    handler = _handler(transport, tmp_path, environ=environ)

    first = handler.handle(_choice())
    assert (len(transport.local_calls), len(transport.jev_calls)) == (1, 0)

    _write_engine_file(tmp_path, {"engine": "jev"})
    second = handler.handle(_choice())
    assert (len(transport.local_calls), len(transport.jev_calls)) == (1, 1)
    assert first.backend_id == _LOCAL_ID
    assert second.backend_id == _JEV_ID
    assert first.backend_id != second.backend_id


@pytest.mark.unit
def test_a_site_with_no_engine_overlay_keeps_the_contract_pinned_engine(
    tmp_path: Path,
) -> None:
    transport = _Transport(_vllm(_p(0.8, 0.2)))
    result = _handler(transport, tmp_path).handle(_choice())
    assert result.backend_id == _JEV_ID
    assert transport.local_calls == []
    assert len(transport.jev_calls) == 1
    # the contract still pins Jev; the engine switch is the overlay's, not a code default
    assert _CONTRACT["decision_routing"]["backend_id"] == "cloud-typesafe-jev"


@pytest.mark.unit
@pytest.mark.parametrize(
    "document",
    [
        {"engine": "local", "local_abstain_below_probability": 0.5},
        {"engine": "local", "local_backend_id": _LOCAL_ID},
        {"engine": "jev", "local_backend_id": _LOCAL_ID},
        {"engine": "other"},
        {"local_backend_id": _LOCAL_ID},
        {"engine": "jev", "surprise": 1},
    ],
    ids=[
        "local-without-backend",
        "local-without-threshold",
        "jev-with-local-field",
        "unknown-engine",
        "engine-missing",
        "unknown-key",
    ],
)
def test_an_invalid_engine_overlay_refuses_and_sends_nothing(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    transport = _Transport(_vllm(_p(0.8, 0.2)))
    handler = _handler(
        transport, tmp_path, environ=_write_engine_file(tmp_path, document)
    )
    with pytest.raises(NodeOverlayError):
        handler.handle(_choice())
    assert transport.local_calls == []
    assert transport.jev_calls == []


# --------------------------------------------------------------- error chain


def _no_logprobs() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "served-local-9",
            "choices": [{"message": {"content": "A"}, "logprobs": None}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("local", "decided_by", "reason"),
    [
        (
            httpx.Response(500, text="vllm down"),
            EnumTypedDecisionDecider.INCUMBENT_BACKEND_ERROR,
            EnumTypedDecisionReason.BACKEND_HTTP_ERROR,
        ),
        (
            httpx.ConnectError("refused"),
            EnumTypedDecisionDecider.INCUMBENT_BACKEND_ERROR,
            EnumTypedDecisionReason.BACKEND_TRANSPORT_ERROR,
        ),
        (
            _no_logprobs(),
            EnumTypedDecisionDecider.INCUMBENT_BACKEND_ERROR,
            EnumTypedDecisionReason.BACKEND_MALFORMED_RESPONSE,
        ),
        (
            _vllm({"Z": -0.1}),
            EnumTypedDecisionDecider.INCUMBENT_BACKEND_ERROR,
            EnumTypedDecisionReason.BACKEND_MALFORMED_RESPONSE,
        ),
        (
            httpx.Response(200, text="not json"),
            EnumTypedDecisionDecider.INCUMBENT_BACKEND_ERROR,
            EnumTypedDecisionReason.BACKEND_MALFORMED_RESPONSE,
        ),
    ],
    ids=["http-500", "transport", "no-logprobs", "no-offered-code", "not-json"],
)
def test_a_failed_local_call_hands_the_decision_to_the_incumbent(
    tmp_path: Path,
    local: httpx.Response | Exception,
    decided_by: EnumTypedDecisionDecider,
    reason: EnumTypedDecisionReason,
) -> None:
    handler = _handler(_Transport(local), tmp_path, engine_overlay=_local_overlay())
    with_incumbent = handler.handle(_choice(incumbent="infrastructure"))
    assert with_incumbent.decided_by is decided_by
    assert with_incumbent.reason is reason
    assert with_incumbent.answer == "infrastructure"
    assert with_incumbent.abstain_below_probability == _LOCAL_THRESHOLD

    blind = handler.handle(_choice(incumbent=None))
    assert blind.decided_by is EnumTypedDecisionDecider.NO_ANSWER
    assert blind.answer is None
    assert blind.reason is reason


@pytest.mark.unit
def test_an_overlay_naming_an_unresolvable_backend_refuses(tmp_path: Path) -> None:
    transport = _Transport(_vllm(_p(0.8, 0.2)))
    result = _handler(
        transport, tmp_path, engine_overlay=_local_overlay("no-such-backend")
    ).handle(_choice())
    assert result.decided_by is EnumTypedDecisionDecider.INCUMBENT_REFUSED
    assert result.reason is EnumTypedDecisionReason.BACKEND_UNRESOLVED
    assert result.answer == "infrastructure"
    assert transport.local_calls == []


@pytest.mark.unit
def test_more_options_than_codes_is_refused_before_any_call(tmp_path: Path) -> None:
    transport = _Transport(_vllm(_p(0.8, 0.2)))
    result = _handler(transport, tmp_path, engine_overlay=_local_overlay()).handle(
        _choice(options=len(OPTION_CODES) + 1)
    )
    assert result.decided_by is EnumTypedDecisionDecider.INCUMBENT_REFUSED
    assert result.reason is EnumTypedDecisionReason.TOO_MANY_OPTIONS
    assert transport.local_calls == []


@pytest.mark.unit
def test_an_answer_below_the_overlays_threshold_abstains(tmp_path: Path) -> None:
    result: ModelTypedDecisionResult = _handler(
        _Transport(_vllm(_p(0.7, 0.3))),
        tmp_path,
        engine_overlay=_local_overlay(threshold=0.9),
    ).handle(_choice(incumbent="infrastructure"))
    assert result.decided_by is EnumTypedDecisionDecider.INCUMBENT_ABSTAINED
    assert result.reason is EnumTypedDecisionReason.BELOW_ABSTENTION_THRESHOLD
    assert result.answer == "infrastructure"
    assert result.model_answer == "change"
    # the overlay's threshold, not the contract's
    assert result.abstain_below_probability == 0.9
    assert (
        result.abstain_below_probability
        != _CONTRACT["decision_routing"]["abstain_below_probability"]
    )


# ------------------------------------------------------------------ scoping


@pytest.mark.unit
def test_work_unit_scoping_is_the_jev_engines_and_not_the_local_engines(
    tmp_path: Path,
) -> None:
    transport = _Transport(_vllm(_p(0.8, 0.2)))
    local = _handler(transport, tmp_path, engine_overlay=_local_overlay()).handle(
        _choice(repo=None)
    )
    assert local.decided_by is EnumTypedDecisionDecider.MODEL

    jev = _handler(
        transport,
        tmp_path,
        engine_overlay=ModelTypedDecisionEngineOverlay(
            engine=EnumTypedDecisionEngine.JEV
        ),
    ).handle(_choice(repo=None))
    assert jev.decided_by is EnumTypedDecisionDecider.INCUMBENT_REFUSED
    assert jev.reason is EnumTypedDecisionReason.NO_REPOSITORY_ATTRIBUTION
    assert transport.jev_calls == []


@pytest.mark.unit
def test_the_contract_declares_the_local_provider_label() -> None:
    assert _CONTRACT["delegation_event"]["local_provider"]
    assert (
        _CONTRACT["delegation_event"]["local_provider"]
        != _CONTRACT["delegation_event"]["provider"]
    )
