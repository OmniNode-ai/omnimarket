# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evidence-only client for an operator-provided model validation service."""

from __future__ import annotations

import json
from collections.abc import Mapping
from math import fsum, isclose
from typing import TYPE_CHECKING

from omnibase_core.enums.enum_model_swap_decision import EnumModelSwapDecision
from omnibase_core.enums.enum_model_swap_validation_failure import (
    EnumModelSwapValidationFailure,
)
from omnibase_core.enums.enum_model_swap_validation_status import (
    EnumModelSwapValidationStatus,
)
from omnibase_core.models.model_swap_validation import (
    ModelModelSwapValidationRequest,
    ModelModelSwapValidationResult,
)
from omnibase_spi.protocols.validation.protocol_model_swap_validator import (
    ProtocolModelSwapValidator,
)
from pydantic import ValidationError

from omnimarket.handlers.rightmodeler_validation.http_transport import (
    RightModelerHttpTransport,
    RightModelerServiceTransportError,
)
from omnimarket.models.model_swap_validation_service.model_model_swap_service_config import (
    ModelModelSwapServiceConfig,
)

if TYPE_CHECKING:
    from omnibase_spi.protocols.services import ProtocolSecretStore


class ValidatorRightModeler(ProtocolModelSwapValidator):
    """POST the shared request and preserve validated service evidence."""

    def __init__(
        self,
        config: ModelModelSwapServiceConfig | None,
        *,
        secret_store: ProtocolSecretStore | None = None,
        transport: RightModelerHttpTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or (
            RightModelerHttpTransport(config, secret_store=secret_store)
            if config is not None
            else None
        )

    @property
    def validator_id(self) -> str:
        return "rightmodeler_service"

    @classmethod
    def from_contract(
        cls,
        contract: Mapping[str, object],
        secret_store: ProtocolSecretStore | None = None,
        transport: RightModelerHttpTransport | None = None,
    ) -> ValidatorRightModeler:
        """Treat incomplete deployment configuration as a typed preflight refusal."""
        try:
            config = ModelModelSwapServiceConfig.model_validate(contract.get("service"))
        except ValidationError:
            config = None
        return cls(config, secret_store=secret_store, transport=transport)

    async def validate(
        self, request: ModelModelSwapValidationRequest
    ) -> ModelModelSwapValidationResult:
        """Return terminal evidence; this client never changes model routing."""
        if self.config is None or self.transport is None:
            return self._failure(
                request,
                EnumModelSwapValidationFailure.NEEDS_INPUT,
                "service_configuration_unavailable",
            )
        try:
            payload = await self.transport.post(request.model_dump_json().encode())
            decoded: object = json.loads(payload)
            if (
                not isinstance(decoded, dict)
                or decoded.get("evidence_only") is not True
            ):
                raise ValueError("Service must explicitly return evidence_only true")
            result = ModelModelSwapValidationResult.model_validate(decoded)
            total_cost = fsum(
                (result.provider_reported_cost_usd, result.estimated_cost_usd)
            )
            if (
                result.correlation_id != request.correlation_id
                or result.judge_mode != request.judge_mode
                or result.quality_floor != request.quality_floor
                or (
                    result.recommended_model is not None
                    and result.recommended_model != request.candidate_model
                )
                or (
                    total_cost > request.max_cost_usd
                    and not isclose(total_cost, request.max_cost_usd, rel_tol=1e-9)
                )
            ):
                raise ValueError("Service evidence does not match the request")
            if (
                result.status == EnumModelSwapValidationStatus.COMPLETED
                and result.decision == EnumModelSwapDecision.RECOMMEND
                and (
                    result.lower_bound is None
                    or result.lower_bound < request.quality_floor
                    or decoded.get("recommendation_effective") is not True
                    or result.recommended_model != request.candidate_model
                    or any(not gate.passed for gate in result.gates)
                )
            ):
                raise ValueError("Service recommendation contradicts its evidence")
            return result
        except RightModelerServiceTransportError as error:
            return self._failure(request, error.failure, error.code)
        except (ValidationError, ValueError, OverflowError):
            return self._failure(
                request,
                EnumModelSwapValidationFailure.SCHEMA_MISMATCH,
                "service_evidence_invalid",
            )
        except Exception:
            return self._failure(
                request,
                EnumModelSwapValidationFailure.TOOL_FAILED,
                "service_transport_failed",
            )

    @staticmethod
    def _failure(
        request: ModelModelSwapValidationRequest,
        reason: EnumModelSwapValidationFailure,
        code: str,
    ) -> ModelModelSwapValidationResult:
        return ModelModelSwapValidationResult(
            correlation_id=request.correlation_id,
            status=EnumModelSwapValidationStatus.FAILED,
            failure_reason=reason,
            quality_floor=request.quality_floor,
            judge_mode=request.judge_mode,
            tool_error_code=code,
        )


__all__ = ["ValidatorRightModeler"]
