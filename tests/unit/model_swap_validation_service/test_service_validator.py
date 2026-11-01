# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The service adapter preserves evidence and rejects validation_request mismatches."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_model_swap_validation_failure import (
    EnumModelSwapValidationFailure,
)
from omnibase_core.models.model_swap_validation import ModelModelSwapValidationRequest
from omnibase_spi.protocols.validation.protocol_model_swap_validator import (
    ProtocolModelSwapValidator,
)

from omnimarket.handlers.rightmodeler_validation.http_transport import (
    RightModelerHttpTransport,
    RightModelerServiceTransportError,
)
from omnimarket.handlers.rightmodeler_validation.validator import (
    ValidatorRightModeler,
)
from omnimarket.models.model_swap_validation_service.model_model_swap_service_config import (
    ModelModelSwapServiceConfig,
)


@pytest.fixture
def validation_request() -> ModelModelSwapValidationRequest:
    return ModelModelSwapValidationRequest(
        correlation_id=uuid4(),
        task_type="summarize",
        reference_model="vendor/reference",
        candidate_model="other/candidate",
        judge_mode="backend",
        judge_backend_id="neutral-judge",
        answer_source="recorded",
        replay_set_digest="a" * 64,
        quality_floor=0.95,
        max_cost_usd=1.0,
        work_unit_repository="organization/work-unit",
    )


def service_config() -> ModelModelSwapServiceConfig:
    return ModelModelSwapServiceConfig(
        endpoint_ref="https://validator.example/full/endpoint",
        secret_ref="rightmodeler.service.api_key",
        deadline_seconds=30.0,
        connect_timeout_seconds=2.0,
        read_timeout_seconds=10.0,
        write_timeout_seconds=2.0,
        pool_timeout_seconds=2.0,
        max_request_bytes=4096,
        max_response_bytes=65536,
        response_chunk_bytes=1024,
    )


@pytest.fixture
def config() -> ModelModelSwapServiceConfig:
    return service_config()


def terminal(validation_request: ModelModelSwapValidationRequest) -> dict[str, object]:
    return {
        "correlation_id": str(validation_request.correlation_id),
        "status": "completed",
        "tool_commit": "b" * 40,
        "image_digest": "sha256:" + "c" * 64,
        "decision": "recommend",
        "reported_decision": "recommend",
        "recommendation_effective": True,
        "recommended_model": validation_request.candidate_model,
        "lower_bound": validation_request.quality_floor,
        "quality_floor": validation_request.quality_floor,
        "judge_mode": validation_request.judge_mode.value,
        "evidence_only": True,
    }


class Transport(RightModelerHttpTransport):
    def __init__(
        self, response: bytes = b"{}", *, error: BaseException | None = None
    ) -> None:
        super().__init__(service_config())
        self.response = response
        self.error = error
        self.requests: list[bytes] = []

    async def post(self, payload: bytes) -> bytes:
        self.requests.append(payload)
        if self.error is not None:
            raise self.error
        return self.response


async def test_shared_request_and_actual_service_provenance_are_preserved(
    config: ModelModelSwapServiceConfig,
    validation_request: ModelModelSwapValidationRequest,
) -> None:
    body = terminal(validation_request)
    transport = Transport(json.dumps(body).encode())
    validator = ValidatorRightModeler(config, transport=transport)
    assert isinstance(validator, ProtocolModelSwapValidator)
    assert validator.validator_id == "rightmodeler_service"
    result = await validator.validate(validation_request)
    assert result.status.value == "completed"
    assert result.tool_commit == body["tool_commit"]
    assert result.image_digest == body["image_digest"]
    assert result.recommended_model == validation_request.candidate_model
    assert result.evidence_only is True
    assert len(transport.requests) == 1
    assert json.loads(transport.requests[0]) == validation_request.model_dump(
        mode="json"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("correlation_id", str(uuid4())),
        ("status", "running"),
        ("judge_mode", "stub"),
        ("quality_floor", 0.96),
        ("recommended_model", "unrequested/model"),
        ("tool_commit", "unpinned"),
        ("image_digest", None),
        ("evidence_only", False),
        ("evidence_only", 1),
        ("unexpected_field", "extra"),
        ("provider_reported_cost_usd", 0.2),
    ],
)
async def test_invalid_or_mismatched_service_evidence_is_schema_failure(
    config: ModelModelSwapServiceConfig,
    validation_request: ModelModelSwapValidationRequest,
    field: str,
    value: object,
) -> None:
    body = {**terminal(validation_request), field: value}
    result = await ValidatorRightModeler(
        config, transport=Transport(json.dumps(body).encode())
    ).validate(validation_request)
    assert result.failure_reason == EnumModelSwapValidationFailure.SCHEMA_MISMATCH
    assert result.correlation_id == validation_request.correlation_id
    assert result.decision is None
    assert result.tool_commit is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lower_bound", 0.94),
        ("lower_bound", None),
        ("recommendation_effective", False),
        ("recommendation_effective", None),
        ("recommendation_effective", 1),
        ("recommendation_effective", "true"),
        ("recommended_model", None),
        ("gates", [{"id": "quality", "pass": False, "reason": "Below floor"}]),
    ],
)
async def test_contradictory_recommendation_is_schema_failure(
    config: ModelModelSwapServiceConfig,
    validation_request: ModelModelSwapValidationRequest,
    field: str,
    value: object,
) -> None:
    body = {**terminal(validation_request), field: value}
    result = await ValidatorRightModeler(
        config, transport=Transport(json.dumps(body).encode())
    ).validate(validation_request)
    assert result.failure_reason == EnumModelSwapValidationFailure.SCHEMA_MISMATCH
    assert result.decision is None


@pytest.mark.parametrize(
    "field", ["lower_bound", "recommendation_effective", "recommended_model"]
)
async def test_recommendation_requires_complete_reported_evidence(
    config: ModelModelSwapServiceConfig,
    validation_request: ModelModelSwapValidationRequest,
    field: str,
) -> None:
    body = terminal(validation_request)
    body.pop(field)
    result = await ValidatorRightModeler(
        config, transport=Transport(json.dumps(body).encode())
    ).validate(validation_request)
    assert result.failure_reason == EnumModelSwapValidationFailure.SCHEMA_MISMATCH


@pytest.mark.parametrize("bound", [0.95, 0.99])
async def test_recommendation_at_or_above_quality_floor_preserves_reported_gates(
    config: ModelModelSwapServiceConfig,
    validation_request: ModelModelSwapValidationRequest,
    bound: float,
) -> None:
    body = {
        **terminal(validation_request),
        "lower_bound": bound,
        "gates": [{"id": "quality", "pass": True, "reason": "Meets floor"}],
    }
    result = await ValidatorRightModeler(
        config, transport=Transport(json.dumps(body).encode())
    ).validate(validation_request)
    assert result.status.value == "completed"
    assert result.lower_bound == bound
    assert [gate.model_dump(by_alias=True) for gate in result.gates] == body["gates"]


@pytest.mark.parametrize("decision", ["reject", "abstain", "inconclusive"])
async def test_nonrecommendation_evidence_is_preserved_without_eligibility_gates(
    config: ModelModelSwapServiceConfig,
    validation_request: ModelModelSwapValidationRequest,
    decision: str,
) -> None:
    body = {
        **terminal(validation_request),
        "decision": decision,
        "reported_decision": decision,
        "recommended_model": None,
        "lower_bound": None,
        "recommendation_effective": False,
        "gates": [{"id": "quality", "pass": False, "reason": "Insufficient"}],
    }
    result = await ValidatorRightModeler(
        config, transport=Transport(json.dumps(body).encode())
    ).validate(validation_request)
    assert result.status.value == "completed"
    assert result.decision is not None
    assert result.decision.value == decision
    assert result.recommendation_effective is False
    assert [gate.model_dump(by_alias=True) for gate in result.gates] == body["gates"]


@pytest.mark.parametrize("cost", [1.0, 1.01])
async def test_receipted_spend_is_bound_to_request_budget(
    config: ModelModelSwapServiceConfig,
    validation_request: ModelModelSwapValidationRequest,
    cost: float,
) -> None:
    body = {
        **terminal(validation_request),
        "estimated_cost_usd": cost,
        "gateway_calls": [
            {
                "role": "candidate",
                "model_id": validation_request.candidate_model,
                "tokens_in": 10,
                "tokens_out": 5,
                "cost_usd": cost,
                "cost_measurement_source": "estimated_list_price",
            }
        ],
    }
    result = await ValidatorRightModeler(
        config, transport=Transport(json.dumps(body).encode())
    ).validate(validation_request)
    if cost <= validation_request.max_cost_usd:
        assert result.status.value == "completed"
        assert result.estimated_cost_usd == cost
    else:
        assert result.failure_reason == EnumModelSwapValidationFailure.SCHEMA_MISMATCH


async def test_typed_service_failure_is_preserved(
    config: ModelModelSwapServiceConfig,
    validation_request: ModelModelSwapValidationRequest,
) -> None:
    body = {
        "correlation_id": str(validation_request.correlation_id),
        "status": "failed",
        "failure_reason": "spend_cap",
        "quality_floor": validation_request.quality_floor,
        "judge_mode": validation_request.judge_mode.value,
        "evidence_only": True,
        "tool_error_code": "budget_cap_refusal",
    }
    result = await ValidatorRightModeler(
        config, transport=Transport(json.dumps(body).encode())
    ).validate(validation_request)
    assert result.failure_reason == EnumModelSwapValidationFailure.SPEND_CAP
    assert result.tool_error_code == "budget_cap_refusal"


async def test_evidence_only_must_be_explicit(
    config: ModelModelSwapServiceConfig,
    validation_request: ModelModelSwapValidationRequest,
) -> None:
    body = terminal(validation_request)
    body.pop("evidence_only")
    result = await ValidatorRightModeler(
        config, transport=Transport(json.dumps(body).encode())
    ).validate(validation_request)
    assert result.failure_reason == EnumModelSwapValidationFailure.SCHEMA_MISMATCH


@pytest.mark.parametrize("body", [b"invalid-json", b"[]", b"null", b"{}"])
async def test_malformed_terminal_is_schema_failure(
    config: ModelModelSwapServiceConfig,
    validation_request: ModelModelSwapValidationRequest,
    body: bytes,
) -> None:
    result = await ValidatorRightModeler(config, transport=Transport(body)).validate(
        validation_request
    )
    assert result.failure_reason == EnumModelSwapValidationFailure.SCHEMA_MISMATCH


@pytest.mark.parametrize("contract", [{}, {"service": {}}, {"service": "not-a-map"}])
async def test_incomplete_config_is_needs_input_without_external_call(
    validation_request: ModelModelSwapValidationRequest, contract: dict[str, object]
) -> None:
    transport = Transport()
    validator = ValidatorRightModeler.from_contract(contract, transport=transport)
    result = await validator.validate(validation_request)
    assert result.failure_reason == EnumModelSwapValidationFailure.NEEDS_INPUT
    assert transport.requests == []


async def test_from_contract_consumes_service_block(
    config: ModelModelSwapServiceConfig,
    validation_request: ModelModelSwapValidationRequest,
) -> None:
    transport = Transport(json.dumps(terminal(validation_request)).encode())
    validator = ValidatorRightModeler.from_contract(
        {"service": config.model_dump(), "other_node_config": "ignored"},
        transport=transport,
    )
    assert (await validator.validate(validation_request)).status.value == "completed"


@pytest.mark.parametrize(
    "reason",
    [
        EnumModelSwapValidationFailure.NEEDS_INPUT,
        EnumModelSwapValidationFailure.TIMEOUT,
        EnumModelSwapValidationFailure.TOOL_FAILED,
        EnumModelSwapValidationFailure.SCHEMA_MISMATCH,
    ],
)
async def test_transport_failures_remain_typed(
    config: ModelModelSwapServiceConfig,
    validation_request: ModelModelSwapValidationRequest,
    reason: EnumModelSwapValidationFailure,
) -> None:
    transport = Transport(
        error=RightModelerServiceTransportError(reason, "service_error")
    )
    result = await ValidatorRightModeler(config, transport=transport).validate(
        validation_request
    )
    assert result.failure_reason == reason
    assert result.tool_error_code == "service_error"


async def test_unexpected_exception_text_cannot_escape_terminal(
    config: ModelModelSwapServiceConfig,
    validation_request: ModelModelSwapValidationRequest,
) -> None:
    result = await ValidatorRightModeler(
        config, transport=Transport(error=RuntimeError("secret-sensitive-error"))
    ).validate(validation_request)
    assert result.failure_reason == EnumModelSwapValidationFailure.TOOL_FAILED
    assert "secret-sensitive-error" not in result.model_dump_json()


async def test_cancellation_propagates(
    config: ModelModelSwapServiceConfig,
    validation_request: ModelModelSwapValidationRequest,
) -> None:
    with pytest.raises(asyncio.CancelledError):
        await ValidatorRightModeler(
            config, transport=Transport(error=asyncio.CancelledError())
        ).validate(validation_request)
