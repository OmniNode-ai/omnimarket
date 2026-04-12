# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verification gate for OMN-8504: Pydantic get_type_hints() resolution.

Confirms no ForwardRef resolution failures across all onex_change_control.overseer
imports after OMN-8502/8503 (ruff TC003/TC001 noqa cleanup) landed.
"""

from __future__ import annotations

import typing

import pytest


@pytest.mark.unit
class TestOmn8504OverseerTypeHintResolution:
    def test_model_worker_contract_type_hints_resolve(self) -> None:
        from onex_change_control.overseer.model_worker_contract import (
            ModelWorkerContract,
        )

        hints = typing.get_type_hints(ModelWorkerContract)
        assert hints, "get_type_hints() returned empty for ModelWorkerContract"
        assert "worker_name" in hints

    def test_model_overnight_contract_type_hints_resolve(self) -> None:
        from onex_change_control.overseer.model_overnight_contract import (
            ModelOvernightContract,
        )

        hints = typing.get_type_hints(ModelOvernightContract)
        assert hints

    def test_model_session_contract_type_hints_resolve(self) -> None:
        from onex_change_control.overseer.model_session_contract import (
            ModelSessionContract,
        )

        hints = typing.get_type_hints(ModelSessionContract)
        assert hints

    def test_model_dispatch_item_type_hints_resolve(self) -> None:
        from onex_change_control.overseer.model_dispatch_item import ModelDispatchItem

        hints = typing.get_type_hints(ModelDispatchItem)
        assert hints

    def test_model_verifier_output_type_hints_resolve(self) -> None:
        from onex_change_control.overseer.model_verifier_output import (
            ModelVerifierOutput,
        )

        hints = typing.get_type_hints(ModelVerifierOutput)
        assert hints

    def test_model_task_state_envelope_type_hints_resolve(self) -> None:
        from onex_change_control.overseer.model_task_state_envelope import (
            ModelTaskStateEnvelope,
        )

        hints = typing.get_type_hints(ModelTaskStateEnvelope)
        assert hints

    def test_model_completion_report_type_hints_resolve(self) -> None:
        from onex_change_control.overseer.model_completion_report import (
            ModelCompletionReport,
        )

        hints = typing.get_type_hints(ModelCompletionReport)
        assert hints

    def test_model_context_bundle_importable(self) -> None:
        from onex_change_control.overseer.model_context_bundle import ModelContextBundle

        # ModelContextBundle is a Union type alias — not a class, get_type_hints() N/A
        assert ModelContextBundle is not None

    def test_model_worker_contract_model_fields_accessible(self) -> None:
        from onex_change_control.overseer import ModelWorkerContract

        fields = ModelWorkerContract.model_fields
        assert "worker_name" in fields
        assert "schema_version" in fields

    def test_handler_overseer_verifier_type_hints_resolve(self) -> None:
        from omnimarket.nodes.node_overseer_verifier.handlers.handler_overseer_verifier import (
            HandlerOverseerVerifier,
        )

        hints = typing.get_type_hints(HandlerOverseerVerifier)
        assert hints is not None
