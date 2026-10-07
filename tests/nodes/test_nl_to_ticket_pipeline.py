# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure in-process R1/R2 NL-to-ticket pipeline proofs."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_ambiguity_gate.handlers.handler_ambiguity_gate_default import (
    HandlerAmbiguityGateDefault,
)
from omnimarket.nodes.node_ambiguity_gate.models.model_ambiguity_gate_error import (
    AmbiguityGateError,
)
from omnimarket.nodes.node_ambiguity_gate.models.model_gate_check_request import (
    ModelGateCheckRequest,
)
from omnimarket.nodes.node_evidence_bundle.handlers.handler_evidence_bundle_default import (
    HandlerEvidenceBundleDefault,
)
from omnimarket.nodes.node_evidence_bundle.handlers.store_bundle_in_memory import (
    StoreBundleInMemory,
)
from omnimarket.nodes.node_evidence_bundle.models.enum_ac_verdict import (
    EnumAcVerdict,
)
from omnimarket.nodes.node_evidence_bundle.models.enum_execution_outcome import (
    EnumExecutionOutcome,
)
from omnimarket.nodes.node_evidence_bundle.models.model_ac_verification_record import (
    ModelAcVerificationRecord,
)
from omnimarket.nodes.node_evidence_bundle.models.model_bundle_generate_request import (
    ModelBundleGenerateRequest,
)
from omnimarket.nodes.node_nl_intent_pipeline.handlers.handler_nl_intent_default import (
    HandlerNlIntentDefault,
)
from omnimarket.nodes.node_nl_intent_pipeline.models.model_nl_parse_request import (
    ModelNlParseRequest,
)
from omnimarket.nodes.node_plan_dag_generator.handlers.handler_plan_dag_default import (
    HandlerPlanDagDefault,
)
from omnimarket.nodes.node_plan_dag_generator.models.model_plan_dag_request import (
    ModelPlanDagRequest,
)
from omnimarket.nodes.node_ticket_compiler.handlers.handler_ticket_compile_default import (
    HandlerTicketCompileDefault,
)
from omnimarket.nodes.node_ticket_compiler.models.model_ticket_compile_request import (
    ModelTicketCompileRequest,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def nl_handler() -> HandlerNlIntentDefault:
    return HandlerNlIntentDefault()


@pytest.fixture
def dag_handler() -> HandlerPlanDagDefault:
    return HandlerPlanDagDefault()


@pytest.fixture
def gate_handler() -> HandlerAmbiguityGateDefault:
    return HandlerAmbiguityGateDefault()


@pytest.fixture
def ticket_handler() -> HandlerTicketCompileDefault:
    return HandlerTicketCompileDefault()


@pytest.fixture
def bundle_store() -> StoreBundleInMemory:
    return StoreBundleInMemory()


@pytest.fixture
def bundle_handler(bundle_store: StoreBundleInMemory) -> HandlerEvidenceBundleDefault:
    return HandlerEvidenceBundleDefault(bundle_store)


def _nl_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _now() -> datetime:
    return datetime(2025, 6, 1, 10, 0, 0, tzinfo=UTC)


def _later() -> datetime:
    return datetime(2025, 6, 1, 10, 5, 0, tzinfo=UTC)


class TestE2eNlToTicket:
    """R1: Clear NL prompt produces a compiled ticket with IDL + test contract + policy."""

    def test_e2e_nl_to_ticket_happy_path(
        self,
        nl_handler: HandlerNlIntentDefault,
        dag_handler: HandlerPlanDagDefault,
        gate_handler: HandlerAmbiguityGateDefault,
        ticket_handler: HandlerTicketCompileDefault,
    ) -> None:
        """Full pipeline: NL → Intent → Plan DAG → Ambiguity Gate → Compiled Ticket."""
        nl_text = "Add OAuth2 login endpoint to the AuthService with unit tests"
        correlation_id = uuid.uuid4()

        # Stage 1: NL → Intent
        nl_request = ModelNlParseRequest(
            raw_nl=nl_text,
            correlation_id=correlation_id,
        )
        intent = nl_handler.handle(nl_request)
        assert intent.intent_id
        assert intent.nl_input_hash == _nl_hash(nl_text)
        assert intent.confidence > 0.0

        # Stage 2: Intent → Plan DAG
        dag_request = ModelPlanDagRequest(
            intent_id=intent.intent_id,
            intent_type=intent.intent_type.value,
            intent_summary=intent.summary,
            correlation_id=correlation_id,
        )
        plan_dag = dag_handler.handle(dag_request)
        assert plan_dag.dag_id
        assert len(plan_dag.nodes) >= 1

        # Stage 3.5: Ambiguity Gate — all nodes must pass
        dag_id = plan_dag.dag_id
        for unit in plan_dag.nodes:
            gate_req = ModelGateCheckRequest(
                unit_id=unit.unit_id,
                unit_title=unit.title,
                unit_description=unit.description or "Implementation details TBD.",
                unit_type=unit.unit_type.value,
                estimated_scope=unit.estimated_scope,
                context=unit.context,
                dag_id=dag_id,
                intent_id=intent.intent_id,
                correlation_id=correlation_id,
            )
            result = gate_handler.handle(gate_req)
            assert result.verdict.value == "PASS", (
                f"Node {unit.unit_id!r} failed ambiguity gate: "
                + "; ".join(f.description for f in result.ambiguity_flags)
            )

        # Stage 4: Plan → Ticket Compilation (compile first node as representative)
        first_unit = plan_dag.nodes[0]
        compile_req = ModelTicketCompileRequest(
            work_unit_id=first_unit.unit_id,
            work_unit_title=first_unit.title,
            work_unit_description=first_unit.description
            or "Implementation details TBD.",
            work_unit_type=first_unit.unit_type.value,
            dag_id=dag_id,
            intent_id=intent.intent_id,
            correlation_id=correlation_id,
        )
        ticket = ticket_handler.handle(compile_req)

        # Ticket must have all required components
        assert ticket.ticket_id
        assert ticket.work_unit_id == first_unit.unit_id
        assert ticket.intent_id == intent.intent_id
        assert ticket.dag_id == dag_id
        assert len(ticket.acceptance_criteria) >= 1
        assert ticket.idl_spec.input_schema
        assert ticket.policy_envelope.sandbox_level is not None
        assert "## IDL Specification" in ticket.description
        assert "## Acceptance Criteria" in ticket.description
        assert "## Policy Envelope" in ticket.description

    def test_e2e_ticket_linked_to_intent_via_nl_hash(
        self,
        nl_handler: HandlerNlIntentDefault,
        dag_handler: HandlerPlanDagDefault,
        gate_handler: HandlerAmbiguityGateDefault,
        ticket_handler: HandlerTicketCompileDefault,
        bundle_handler: HandlerEvidenceBundleDefault,
    ) -> None:
        """R1: Evidence bundle references nl_input_hash for traceability."""
        nl_text = "Fix the session timeout bug in the login flow"
        correlation_id = uuid.uuid4()

        # Stages 1-4
        intent = nl_handler.handle(
            ModelNlParseRequest(raw_nl=nl_text, correlation_id=correlation_id)
        )
        plan_dag = dag_handler.handle(
            ModelPlanDagRequest(
                intent_id=intent.intent_id,
                intent_type=intent.intent_type.value,
                intent_summary=intent.summary,
                correlation_id=correlation_id,
            )
        )
        first_unit = plan_dag.nodes[0]

        # Pass gate for first unit (may need description padding for generic templates)
        gate_req = ModelGateCheckRequest(
            unit_id=first_unit.unit_id,
            unit_title=first_unit.title,
            unit_description=first_unit.description
            or "Detailed implementation per design.",
            unit_type=first_unit.unit_type.value,
            estimated_scope=first_unit.estimated_scope,
            context=first_unit.context,
            dag_id=plan_dag.dag_id,
            intent_id=intent.intent_id,
            correlation_id=correlation_id,
        )
        gate_handler.handle(gate_req)

        ticket = ticket_handler.handle(
            ModelTicketCompileRequest(
                work_unit_id=first_unit.unit_id,
                work_unit_title=first_unit.title,
                work_unit_description=first_unit.description
                or "Detailed implementation.",
                work_unit_type=first_unit.unit_type.value,
                dag_id=plan_dag.dag_id,
                intent_id=intent.intent_id,
                correlation_id=correlation_id,
            )
        )

        # Stage 5: Evidence Bundle Generation
        ac_records = tuple(
            ModelAcVerificationRecord(
                criterion_id=ac.criterion_id,
                verdict=EnumAcVerdict.PASS,
                actual_value="0",
                verified_at=_later(),
            )
            for ac in ticket.acceptance_criteria
        )
        bundle = bundle_handler.handle(
            ModelBundleGenerateRequest(
                ticket_id=ticket.ticket_id,
                work_unit_id=ticket.work_unit_id,
                dag_id=ticket.dag_id,
                intent_id=ticket.intent_id,
                nl_input_hash=intent.nl_input_hash,
                outcome=EnumExecutionOutcome.SUCCESS,
                ac_records=ac_records,
                started_at=_now(),
                completed_at=_later(),
                correlation_id=correlation_id,
            )
        )

        # Bundle links all chain IDs back to original NL input
        assert bundle.nl_input_hash == intent.nl_input_hash
        assert bundle.nl_input_hash == _nl_hash(nl_text)
        assert bundle.ticket_id == ticket.ticket_id
        assert bundle.work_unit_id == first_unit.unit_id
        assert bundle.dag_id == plan_dag.dag_id
        assert bundle.intent_id == intent.intent_id


class TestE2eAmbiguityRejection:
    """R2: Ambiguous work unit raises AmbiguityGateError; no ticket emitted."""

    def test_e2e_ambiguity_rejection_vague_title(
        self,
        gate_handler: HandlerAmbiguityGateDefault,
    ) -> None:
        """Vague work unit title blocks ticket compilation at the gate."""
        correlation_id = uuid.uuid4()
        dag_id = f"dag-{uuid.uuid4()}"
        intent_id = f"intent-{uuid.uuid4()}"

        gate_req = ModelGateCheckRequest(
            unit_id=f"wu-{uuid.uuid4()}",
            unit_title="Fix",  # Too vague: 1 word
            unit_description="Implement the fix as needed.",
            unit_type="BUG_FIX",
            dag_id=dag_id,
            intent_id=intent_id,
            correlation_id=correlation_id,
        )

        with pytest.raises(AmbiguityGateError) as exc_info:
            gate_handler.handle(gate_req)

        # Gate rejected before ticket compilation; verify rejection details
        assert exc_info.value.result.verdict.value == "FAIL"

        # Error includes which ambiguity type and suggested resolution
        flags = exc_info.value.result.ambiguity_flags
        flag_types = {f.ambiguity_type.value for f in flags}
        assert "TITLE_TOO_VAGUE" in flag_types

        for flag in flags:
            assert flag.suggested_resolution, "Flag missing suggested_resolution"

    def test_e2e_ambiguity_rejection_missing_description(
        self,
        gate_handler: HandlerAmbiguityGateDefault,
    ) -> None:
        """Missing description blocks ticket compilation at the gate."""
        gate_req = ModelGateCheckRequest(
            unit_id=f"wu-{uuid.uuid4()}",
            unit_title="Add OAuth2 login endpoint",
            unit_description="",  # Missing description
            unit_type="FEATURE_IMPLEMENTATION",
            dag_id=f"dag-{uuid.uuid4()}",
            intent_id=f"intent-{uuid.uuid4()}",
            correlation_id=uuid.uuid4(),
        )

        with pytest.raises(AmbiguityGateError) as exc_info:
            gate_handler.handle(gate_req)

        flags = exc_info.value.result.ambiguity_flags
        flag_types = {f.ambiguity_type.value for f in flags}
        assert "DESCRIPTION_MISSING" in flag_types

    def test_e2e_ambiguity_rejection_generic_type(
        self,
        gate_handler: HandlerAmbiguityGateDefault,
    ) -> None:
        """GENERIC unit type blocks ticket compilation at the gate."""
        gate_req = ModelGateCheckRequest(
            unit_id=f"wu-{uuid.uuid4()}",
            unit_title="Add OAuth2 login endpoint",
            unit_description="Implement OAuth2 using the existing session manager.",
            unit_type="GENERIC",  # Unknown type
            dag_id=f"dag-{uuid.uuid4()}",
            intent_id=f"intent-{uuid.uuid4()}",
            correlation_id=uuid.uuid4(),
        )

        with pytest.raises(AmbiguityGateError) as exc_info:
            gate_handler.handle(gate_req)

        flags = exc_info.value.result.ambiguity_flags
        flag_types = {f.ambiguity_type.value for f in flags}
        assert "UNKNOWN_UNIT_TYPE" in flag_types

    def test_e2e_ambiguity_rejection_error_is_traceable(
        self,
        gate_handler: HandlerAmbiguityGateDefault,
    ) -> None:
        """Rejection error is traceable to DAG and Intent IDs."""
        dag_id = f"dag-{uuid.uuid4()}"
        intent_id = f"intent-{uuid.uuid4()}"
        unit_id = f"wu-{uuid.uuid4()}"

        gate_req = ModelGateCheckRequest(
            unit_id=unit_id,
            unit_title="Fix",
            unit_description="",
            unit_type="GENERIC",
            dag_id=dag_id,
            intent_id=intent_id,
            correlation_id=uuid.uuid4(),
        )

        with pytest.raises(AmbiguityGateError) as exc_info:
            gate_handler.handle(gate_req)

        result = exc_info.value.result
        assert result.unit_id == unit_id
        assert result.dag_id == dag_id
        assert result.intent_id == intent_id
