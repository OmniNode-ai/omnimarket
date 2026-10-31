# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol-driven service calls with durable, fenced terminal delivery."""

from __future__ import annotations

import asyncio
import importlib
import math
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml
from omnibase_core.enums.enum_model_swap_validation_failure import (
    EnumModelSwapValidationFailure,
)
from omnibase_core.enums.enum_model_swap_validation_status import (
    EnumModelSwapValidationStatus,
)
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.model_swap_validation import (
    ModelModelSwapValidationRequest,
    ModelModelSwapValidationResult,
)
from omnibase_spi.protocols.services import ProtocolSecretStore
from omnibase_spi.protocols.validation.protocol_model_swap_validator import (
    ProtocolModelSwapValidator,
)
from pydantic import ValidationError

from omnimarket.nodes.node_model_swap_validation_effect.handlers.terminal_state import (
    TerminalState,
)
from omnimarket.nodes.node_model_swap_validation_effect.models.model_model_swap_state_config import (
    ModelModelSwapStateConfig,
)
from omnimarket.nodes.node_model_swap_validation_effect.models.model_model_swap_validation import (
    terminal_event,
)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_contract(path: Path = _CONTRACT_PATH) -> dict[str, Any]:
    """Read the rendered configuration supplied by the operator."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("validation contract must be a mapping")
    return cast(dict[str, Any], raw)


class HandlerModelSwapValidation:
    """Issue at most one service call for an immutable command identity.

    Discovery constructs this class without credentials, state directories or
    environment-dependent work. Configuration errors become typed refusals on
    the first call. A persisted start intent is never retried after a crash:
    an interrupted HTTP request may still be running remotely.
    """

    def __init__(
        self,
        *,
        validator: ProtocolModelSwapValidator | None = None,
        contract_path: Path | None = None,
        secret_store: ProtocolSecretStore | None = None,
    ) -> None:
        self._contract_path = contract_path or _CONTRACT_PATH
        self._contract: dict[str, Any] | None = None
        self._state: TerminalState | None = None
        self._timeout = 0.0
        self._validator = validator
        self._secret_store = secret_store

    def _configuration(self) -> TerminalState:
        if self._state is not None:
            return self._state
        contract = load_contract(self._contract_path)
        packaged = load_contract()
        for boundary in (
            "event_bus",
            "runtime_dispatch",
            "published_events",
            "handler_routing",
            "validator",
        ):
            if contract.get(boundary) != packaged.get(boundary):
                raise ValueError(f"operator contract must preserve {boundary}")
        state = ModelModelSwapStateConfig.model_validate(contract["state"])
        timeout_ms = contract["descriptor"]["timeout_ms"]
        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int | float)
            or not math.isfinite(timeout_ms)
            or timeout_ms <= 0
        ):
            raise ValueError("descriptor.timeout_ms must be finite and positive")
        self._timeout = timeout_ms / 1000
        self._contract = contract
        self._state = TerminalState(state)
        return self._state

    def _provider(self) -> ProtocolModelSwapValidator:
        if self._validator is None:
            if self._contract is None:
                raise ValueError("validation contract is not configured")
            binding: Mapping[str, str] = self._contract["validator"]
            module = importlib.import_module(binding["module"])
            provider_type = getattr(module, binding["class"])
            provider = provider_type.from_contract(
                self._contract, secret_store=self._secret_store
            )
            if not isinstance(provider, ProtocolModelSwapValidator):
                raise TypeError("contract validator must satisfy its platform protocol")
            self._validator = provider
        return self._validator

    @staticmethod
    def _failure(
        request: ModelModelSwapValidationRequest,
        reason: EnumModelSwapValidationFailure,
        *,
        code: str | None = None,
    ) -> ModelModelSwapValidationResult:
        return terminal_event(
            ModelModelSwapValidationResult(
                correlation_id=request.correlation_id,
                status=EnumModelSwapValidationStatus.FAILED,
                failure_reason=reason,
                judge_mode=request.judge_mode,
                quality_floor=request.quality_floor,
                tool_error_code=code,
            )
        )

    @staticmethod
    def _matches_request(
        result: ModelModelSwapValidationResult,
        request: ModelModelSwapValidationRequest,
    ) -> bool:
        return (
            result.correlation_id == request.correlation_id
            and result.judge_mode == request.judge_mode
            and result.quality_floor == request.quality_floor
            and (
                result.recommended_model is None
                or result.recommended_model == request.candidate_model
            )
        )

    async def handle(
        self, request: ModelModelSwapValidationRequest
    ) -> ModelModelSwapValidationResult:
        try:
            state = self._configuration()
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            sqlite3.Error,
            yaml.YAMLError,
        ):
            return self._failure(
                request,
                EnumModelSwapValidationFailure.NEEDS_INPUT,
                code="configuration_invalid",
            )
        try:
            return await self._handle_in_state(request, state)
        except (OSError, sqlite3.Error):
            return self._failure(
                request,
                EnumModelSwapValidationFailure.NEEDS_INPUT,
                code="state_unavailable",
            )

    async def _handle_in_state(
        self, request: ModelModelSwapValidationRequest, state: TerminalState
    ) -> ModelModelSwapValidationResult:
        request_hash = state.request_hash(request.model_dump(mode="json"))
        async with state.ownership(request.correlation_id):
            try:
                fence, stored, interrupted = state.claim(
                    request.correlation_id, request_hash
                )
            except ValueError:
                return self._failure(
                    request,
                    EnumModelSwapValidationFailure.NEEDS_INPUT,
                    code="correlation_identity_conflict",
                )
            if stored is not None:
                try:
                    recovered = ModelModelSwapValidationResult.model_validate_json(
                        stored
                    )
                except ValidationError:
                    recovered = None
                if recovered is None or not self._matches_request(recovered, request):
                    return self._failure(
                        request,
                        EnumModelSwapValidationFailure.NEEDS_INPUT,
                        code="state_unavailable",
                    )
                return terminal_event(recovered)
            if interrupted:
                result = self._failure(
                    request,
                    EnumModelSwapValidationFailure.TIMEOUT,
                    code="interrupted_service_call",
                )
            elif request.work_unit_repository is None:
                result = self._failure(
                    request, EnumModelSwapValidationFailure.REPOSITORY_REFUSED
                )
            else:
                state.store_start_intent(request.correlation_id, fence)
                try:
                    result = await asyncio.wait_for(
                        self._provider().validate(request), timeout=self._timeout
                    )
                    if not self._matches_request(result, request):
                        result = self._failure(
                            request, EnumModelSwapValidationFailure.SCHEMA_MISMATCH
                        )
                except TimeoutError:
                    result = self._failure(
                        request,
                        EnumModelSwapValidationFailure.TIMEOUT,
                        code="service_outcome_unknown",
                    )
                except asyncio.CancelledError:
                    result = self._failure(
                        request,
                        EnumModelSwapValidationFailure.TIMEOUT,
                        code="service_outcome_unknown",
                    )
                    state.store_terminal(
                        request.correlation_id, fence, result.model_dump_json()
                    )
                    raise
                except Exception:
                    result = self._failure(
                        request,
                        EnumModelSwapValidationFailure.TOOL_FAILED,
                        code="validator_error",
                    )
            terminal = terminal_event(result)
            state.store_terminal(
                request.correlation_id, fence, terminal.model_dump_json()
            )
            return terminal

    async def handle_async(
        self, request: ModelModelSwapValidationRequest
    ) -> ModelHandlerOutput[None]:
        """Publish one terminal through the existing contract-derived applier."""
        result = await self.handle(request)
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="node_model_swap_validation_effect.validate",
            events=(result,),
        )
