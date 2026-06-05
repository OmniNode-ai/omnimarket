# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12702: node_task_execution_orchestrator route-planning tests.

Covers the first vertical slice DoD:

- raw prompt normalizes to a ModelTaskContract and route plan.
- a supplied ModelTaskContract is passed through verbatim and route-planned.
- an unsupported request produces a typed deterministic failure (no silent skip).
- dry-run returns the route plan with no side effects.
- determinism: the same task contract always produces the same route plan.
- in-memory Pattern-B integration: publishing a ModelDispatchBusCommand to the
  command topic yields a terminal result with the same correlation_id, a
  completed status, and the expected route decisions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_check_type import EnumCheckType
from omnibase_core.models.dispatch.model_dispatch_bus_command import (
    ModelDispatchBusCommand,
)
from omnibase_core.models.task.model_mechanical_check import ModelMechanicalCheck
from omnibase_core.models.task.model_task_contract import ModelTaskContract

from omnimarket.nodes.node_task_execution_orchestrator.handlers.handler_task_execution_orchestrator import (
    HandlerTaskExecutionOrchestrator,
    UnsupportedTaskActionError,
)
from omnimarket.nodes.node_task_execution_orchestrator.models.model_task_execution import (
    EnumRouteItemKind,
    EnumTaskRoute,
    ModelTaskExecutionRequest,
)

TOPIC_TASK_EXECUTE_START = "onex.cmd.omnimarket.task-execute-start.v1"
TOPIC_TASK_EXECUTE_RESPONSE = "onex.evt.omnimarket.task-execute-completed.v1"

_FIXED_GENERATED_AT = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)


def _sample_contract() -> ModelTaskContract:
    return ModelTaskContract(
        task_id="task-1",
        parent_ticket="OMN-12702",
        repo="omnibase_core",
        generated_at=_FIXED_GENERATED_AT,
        requirements=["refactor the config loader"],
        definition_of_done=[
            ModelMechanicalCheck(
                criterion="tests pass",
                check="uv run pytest tests/",
                check_type=EnumCheckType.COMMAND_EXIT_0,
            ),
            ModelMechanicalCheck(
                criterion="no TODO markers remain",
                check="grep -r TODO src/",
                check_type=EnumCheckType.GREP_ABSENT,
            ),
        ],
    )


@pytest.mark.unit
class TestRawPromptNormalization:
    """A raw prompt normalizes into a contract and a deterministic route plan."""

    def test_prompt_produces_contract_and_delegation_route(self) -> None:
        handler = HandlerTaskExecutionOrchestrator()
        result = handler.handle(
            ModelTaskExecutionRequest(
                prompt="add a CLI flag to the importer",
                target_repo="omnimarket",
                ticket_id="OMN-12702",
            ),
            generated_at=_FIXED_GENERATED_AT,
        )

        assert result.ok is True
        assert result.dry_run is True
        assert result.task_contract.repo == "omnimarket"
        assert result.task_contract.parent_ticket == "OMN-12702"
        assert result.task_contract.requirements == ["add a CLI flag to the importer"]
        assert len(result.route_plan) == 1
        decision = result.route_plan[0]
        assert decision.kind is EnumRouteItemKind.REQUIREMENT
        assert decision.route is EnumTaskRoute.DELEGATION


@pytest.mark.unit
class TestSuppliedContractPassthrough:
    """A supplied ModelTaskContract is used verbatim and route-planned."""

    def test_supplied_contract_maps_requirements_and_checks(self) -> None:
        handler = HandlerTaskExecutionOrchestrator()
        contract = _sample_contract()
        result = handler.handle(
            ModelTaskExecutionRequest(task_contract=contract),
            generated_at=_FIXED_GENERATED_AT,
        )

        assert result.ok is True
        assert result.task_contract == contract
        # 1 requirement -> delegation, 2 mechanical checks -> verification.
        routes = [d.route for d in result.route_plan]
        assert routes == [
            EnumTaskRoute.DELEGATION,
            EnumTaskRoute.VERIFICATION,
            EnumTaskRoute.VERIFICATION,
        ]
        kinds = [d.kind for d in result.route_plan]
        assert kinds == [
            EnumRouteItemKind.REQUIREMENT,
            EnumRouteItemKind.MECHANICAL_CHECK,
            EnumRouteItemKind.MECHANICAL_CHECK,
        ]


@pytest.mark.unit
class TestUnsupportedRequests:
    """Unsupported actions raise typed deterministic failures, never silent skips."""

    def test_neither_prompt_nor_contract(self) -> None:
        handler = HandlerTaskExecutionOrchestrator()
        with pytest.raises(UnsupportedTaskActionError) as excinfo:
            handler.handle(
                ModelTaskExecutionRequest(), generated_at=_FIXED_GENERATED_AT
            )
        assert "exactly one of prompt or task_contract" in excinfo.value.reason

    def test_both_prompt_and_contract(self) -> None:
        handler = HandlerTaskExecutionOrchestrator()
        with pytest.raises(UnsupportedTaskActionError):
            handler.handle(
                ModelTaskExecutionRequest(
                    prompt="do something",
                    task_contract=_sample_contract(),
                ),
                generated_at=_FIXED_GENERATED_AT,
            )

    def test_non_dry_run_unsupported(self) -> None:
        handler = HandlerTaskExecutionOrchestrator()
        with pytest.raises(UnsupportedTaskActionError) as excinfo:
            handler.handle(
                ModelTaskExecutionRequest(prompt="do something", dry_run=False),
                generated_at=_FIXED_GENERATED_AT,
            )
        assert "non-dry-run" in excinfo.value.reason

    def test_side_effects_unsupported(self) -> None:
        handler = HandlerTaskExecutionOrchestrator()
        with pytest.raises(UnsupportedTaskActionError):
            handler.handle(
                ModelTaskExecutionRequest(
                    prompt="do something",
                    allowed_side_effects=("create_pr",),
                ),
                generated_at=_FIXED_GENERATED_AT,
            )


@pytest.mark.unit
class TestDeterminism:
    """Same task contract always produces the same route plan and fingerprint."""

    def test_same_contract_same_plan(self) -> None:
        handler = HandlerTaskExecutionOrchestrator()
        contract = _sample_contract()
        first = handler.handle(
            ModelTaskExecutionRequest(task_contract=contract),
            generated_at=_FIXED_GENERATED_AT,
        )
        second = handler.handle(
            ModelTaskExecutionRequest(task_contract=contract),
            generated_at=_FIXED_GENERATED_AT,
        )

        assert first.route_plan == second.route_plan
        assert first.contract_fingerprint == second.contract_fingerprint

    def test_fingerprint_independent_of_generated_at(self) -> None:
        handler = HandlerTaskExecutionOrchestrator()
        base = _sample_contract()
        later = base.model_copy(
            update={"generated_at": datetime(2026, 12, 31, 23, 59, tzinfo=UTC)}
        )
        first = handler.handle(
            ModelTaskExecutionRequest(task_contract=base),
            generated_at=_FIXED_GENERATED_AT,
        )
        second = handler.handle(
            ModelTaskExecutionRequest(task_contract=later),
            generated_at=_FIXED_GENERATED_AT,
        )
        assert first.contract_fingerprint == second.contract_fingerprint
        assert first.route_plan == second.route_plan

    def test_prompt_path_terminal_payload_is_replay_stable(self) -> None:
        """Two process() calls on the SAME command yield an identical plan payload.

        The prompt path derives generated_at from the command's created_at (not
        wall-clock), so an identical input envelope produces an identical
        normalized contract — including generated_at — and thus an identical
        terminal ``payload`` (the planning result). This is what makes the
        contract's ``idempotent: true`` claim honest for the prompt path.

        The terminal envelope's ``completed_at`` is an observability field (when
        the result envelope was created) carried by the reused core model
        ModelDispatchBusTerminalResult; it is deliberately excluded from the
        idempotency claim, which governs the planning output, not envelope
        creation time.
        """
        import asyncio

        handler = HandlerTaskExecutionOrchestrator()
        command = ModelDispatchBusCommand(
            command_name="task.execute",
            requester="pytest",
            payload={"prompt": "refactor the config loader", "dry_run": True},
            correlation_id=uuid4(),
            response_topic=TOPIC_TASK_EXECUTE_RESPONSE,
            created_at=_FIXED_GENERATED_AT,
        )
        encoded = command.model_dump_json().encode("utf-8")

        first = json.loads(asyncio.run(handler.process(encoded)))
        second = json.loads(asyncio.run(handler.process(encoded)))

        assert first["status"] == "completed"
        # The planning result (normalized contract incl. generated_at + route
        # plan) is byte-for-byte stable across identical input.
        assert first["payload"] == second["payload"]
        assert (
            datetime.fromisoformat(first["payload"]["task_contract"]["generated_at"])
            == _FIXED_GENERATED_AT
        )


@pytest.mark.unit
class TestMechanicalCheckExecution:
    """task.execute COMPOSES the verification authority for mechanical checks.

    It dispatches the contract's mechanical checks to
    node_verification_receipt_generator, aggregates the receipt UNCHANGED, and
    derives terminal status from the receipt — it never executes a check itself
    nor reinterprets a sub-result (OMN-12703).
    """

    def test_passing_checks_aggregate_receipt_unchanged(self) -> None:
        from omnimarket.events.verification import (
            ModelCheckEvidence,
            ModelVerificationReceipt,
            ModelVerificationReceiptRequest,
        )
        from omnimarket.nodes.node_verification_receipt_generator.handlers.handler_verification_receipt import (
            HandlerVerificationReceiptGenerator,
        )

        captured: dict[str, ModelVerificationReceiptRequest] = {}
        sentinel = ModelVerificationReceipt(
            task_id="task-1",
            claim="task.execute mechanical DoD verification",
            overall_pass=True,
            checks=[
                ModelCheckEvidence(
                    dimension="mechanical_check:tests pass",
                    passed=True,
                    summary="command_exit_0 exit_code=0",
                ),
            ],
            verified_at=_FIXED_GENERATED_AT,
        )

        class _StubExecutor(HandlerVerificationReceiptGenerator):
            def handle(  # type: ignore[override]
                self, request: ModelVerificationReceiptRequest
            ) -> ModelVerificationReceipt:
                captured["request"] = request
                return sentinel

        handler = HandlerTaskExecutionOrchestrator(
            mechanical_check_executor=_StubExecutor()
        )
        result = handler.handle(
            ModelTaskExecutionRequest(
                task_contract=_sample_contract(),
                execute_mechanical_checks=True,
                worktree_path="/work/tree",
            ),
            generated_at=_FIXED_GENERATED_AT,
        )

        assert result.ok is True
        assert result.failure_reason is None
        # Receipt is aggregated verbatim — same object, never transformed.
        assert result.verification_receipt is sentinel
        # The orchestrator dispatched the contract's DoD checks unchanged,
        # disabling CI/pytest dimensions, with the worktree passed through.
        dispatched = captured["request"]
        assert dispatched.verify_ci is False
        assert dispatched.verify_tests is False
        assert dispatched.worktree_path == "/work/tree"
        assert dispatched.mechanical_checks == tuple(
            _sample_contract().definition_of_done
        )

    def test_failed_check_yields_deterministic_failure_reason(self) -> None:
        from omnimarket.events.verification import (
            ModelCheckEvidence,
            ModelVerificationReceipt,
            ModelVerificationReceiptRequest,
        )
        from omnimarket.nodes.node_verification_receipt_generator.handlers.handler_verification_receipt import (
            HandlerVerificationReceiptGenerator,
        )

        failing = ModelVerificationReceipt(
            task_id="task-1",
            claim="task.execute mechanical DoD verification",
            overall_pass=False,
            checks=[
                ModelCheckEvidence(
                    dimension="mechanical_check:tests pass",
                    passed=True,
                    summary="command_exit_0 exit_code=0",
                ),
                ModelCheckEvidence(
                    dimension="mechanical_check:no TODO markers remain",
                    passed=False,
                    summary="grep_absent found=True",
                ),
            ],
            verified_at=_FIXED_GENERATED_AT,
        )

        class _StubExecutor(HandlerVerificationReceiptGenerator):
            def handle(  # type: ignore[override]
                self, request: ModelVerificationReceiptRequest
            ) -> ModelVerificationReceipt:
                return failing

        handler = HandlerTaskExecutionOrchestrator(
            mechanical_check_executor=_StubExecutor()
        )
        result = handler.handle(
            ModelTaskExecutionRequest(
                task_contract=_sample_contract(),
                execute_mechanical_checks=True,
            ),
            generated_at=_FIXED_GENERATED_AT,
        )

        assert result.ok is False
        assert result.verification_receipt is failing
        # Deterministic reason names exactly the failed dimension, read off the
        # receipt — never a free-text summary.
        assert result.failure_reason == (
            "mechanical checks failed: mechanical_check:no TODO markers remain"
        )

    def test_real_executor_end_to_end(self, tmp_path: object) -> None:
        """End-to-end against the real verification handler (no network)."""
        from pathlib import Path

        from omnibase_core.models.task.model_task_contract import ModelTaskContract

        work = tmp_path
        assert isinstance(work, Path)
        (work / "code.py").write_text("clean code\n")

        contract = ModelTaskContract(
            task_id="task-real",
            generated_at=_FIXED_GENERATED_AT,
            requirements=["keep it clean"],
            definition_of_done=[
                ModelMechanicalCheck(
                    criterion="command ok",
                    check="true",
                    check_type=EnumCheckType.COMMAND_EXIT_0,
                ),
                ModelMechanicalCheck(
                    criterion="no TODO markers",
                    check="-r TODO .",
                    check_type=EnumCheckType.GREP_ABSENT,
                ),
            ],
        )

        handler = HandlerTaskExecutionOrchestrator()
        result = handler.handle(
            ModelTaskExecutionRequest(
                task_contract=contract,
                execute_mechanical_checks=True,
                worktree_path=str(work),
            ),
            generated_at=_FIXED_GENERATED_AT,
        )

        assert result.ok is True
        assert result.verification_receipt is not None
        assert result.verification_receipt.overall_pass is True
        assert len(result.verification_receipt.checks) == 2


@pytest.mark.unit
def test_pattern_b_payload_must_be_object() -> None:
    """A non-object Pattern-B payload fails deterministically, not silently."""
    handler = HandlerTaskExecutionOrchestrator()
    correlation_id = uuid4()
    command = ModelDispatchBusCommand(
        command_name="task.execute",
        requester="pytest",
        payload=["not", "an", "object"],
        correlation_id=correlation_id,
        response_topic=TOPIC_TASK_EXECUTE_RESPONSE,
    )

    import asyncio

    returned = asyncio.run(handler.process(command.model_dump_json().encode("utf-8")))
    data = json.loads(returned)
    assert data["status"] == "failed"
    assert data["correlation_id"] == str(correlation_id)
