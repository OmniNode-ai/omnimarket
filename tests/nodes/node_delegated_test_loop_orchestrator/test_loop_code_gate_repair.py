# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19527 AC2 — a gate digest with findings buys exactly one repair round.

When a written test passes at the fixed ref but the repository's lint and type
gates refuse it, the loop makes exactly ONE repair delegate call that carries
the gate digest text verbatim; a clean digest makes none. The repair is a
polish: if the repaired test no longer passes, the loop keeps the passing test
it had. The three-call bound of OMN-19362 still holds.
"""

from __future__ import annotations

from typing import Literal

import pytest

from omnimarket.nodes.node_delegated_test_loop_orchestrator.handlers.handler_delegated_test_loop_orchestrator import (
    HandlerDelegatedTestLoopOrchestrator,
    result_json_bytes,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator.handlers.replay import (
    replay_loop_receipt,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator.models.model_delegated_test_loop import (
    MAX_RESULT_BYTES,
    EnumLoopStatus,
    ModelDelegatedTestLoopRequest,
    ModelDelegateReply,
    ModelGateDigestSeam,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator.protocols.protocol_delegated_test_loop_ports import (
    ModelGateToolRun,
    ModelRunReceipt,
    ProtocolDelegatedTestLoopPorts,
)
from tests.nodes.node_delegated_test_loop_orchestrator.test_handler_delegated_test_loop_orchestrator import (
    CORRELATION,
    FakePorts,
    _request,
)

pytestmark = pytest.mark.unit

FINDINGS = (
    "tests/unit/projection/test_dtl_generated_0f1e2d3c.py:5: [ruff_check] F401 "
    "`os` imported but unused\n"
    "tests/unit/projection/test_dtl_generated_0f1e2d3c.py:9: [any_types] ONEX-ANY "
    "typing.Any is refused"
)


def _gate(kind: str) -> ModelGateDigestSeam:
    if kind == "clean":
        return ModelGateDigestSeam(clean=True)
    if kind == "infra":
        return ModelGateDigestSeam(
            clean=False,
            infra_error=True,
            digest_text="t.py: [infra] mypy exited 127: not found",
            fingerprint="infra",
        )
    return ModelGateDigestSeam(
        clean=False, digest_text=FINDINGS, finding_count=2, fingerprint=f"fp-{kind}"
    )


class GatedPorts(FakePorts):
    """FakePorts whose fixed runs carry gate outputs, digested from a script."""

    def __init__(
        self, runs: dict[str, list[str]], gates: list[str], **kw: object
    ) -> None:
        super().__init__(runs, **kw)  # type: ignore[arg-type]
        self.gates = list(gates)
        self.gate_prompts: list[ModelGateDigestSeam | None] = []
        self.run_sources: list[tuple[str, str]] = []

    def build_prompt(  # type: ignore[no-untyped-def,override]
        self, request, target_excerpt, previous_test, last, gate=None
    ):
        self.gate_prompts.append(gate)
        return super().build_prompt(request, target_excerpt, previous_test, last, gate)

    def run(  # type: ignore[no-untyped-def]
        self,
        request,
        ref,
        ref_role: Literal["fixed", "prefix", "mutation"],
        attempt,
        test_source,
    ):
        self.run_sources.append((ref_role, test_source))
        receipt = super().run(request, ref, ref_role, attempt, test_source)
        if ref_role != "fixed" or receipt.status != "completed":
            return receipt
        return receipt.model_copy(
            update={
                "gate_outputs": (
                    ModelGateToolRun(
                        path=request.test_path,
                        gate="ruff_check",
                        exit_code=1,
                        output="x",
                    ),
                )
            }
        )

    def digest_gates(
        self, receipt: ModelRunReceipt, source: str
    ) -> ModelGateDigestSeam:
        return _gate(self.gates.pop(0))


def _run(ports: GatedPorts, **overrides: object):  # type: ignore[no-untyped-def]
    assert isinstance(ports, ProtocolDelegatedTestLoopPorts)
    return HandlerDelegatedTestLoopOrchestrator(ports).run(_request(**overrides))


def test_findings_produce_exactly_one_repair_carrying_the_digest_verbatim() -> None:
    ports = GatedPorts(
        {"fixed": ["passed", "passed"], "prefix": ["failed_call"]},
        ["findings", "clean"],
    )
    result = _run(ports)
    assert ports.delegate_calls == 2
    assert ports.gate_prompts[0] is None
    repair = ports.gate_prompts[1]
    assert repair is not None
    assert repair.digest_text == FINDINGS
    assert result.status is EnumLoopStatus.ACCEPTED_CALL
    assert result.gate_repairs == 1
    assert result.gate_clean is True
    # The control ran the repaired test.
    assert ports.run_sources[-1] == ("prefix", ports.run_sources[1][1])


def test_a_clean_digest_produces_no_repair() -> None:
    ports = GatedPorts({"fixed": ["passed"], "prefix": ["failed_call"]}, ["clean"])
    result = _run(ports)
    assert ports.delegate_calls == 1
    assert result.gate_repairs == 0
    assert result.gate_clean is True


def test_findings_that_survive_the_repair_do_not_buy_a_second_one() -> None:
    ports = GatedPorts(
        {"fixed": ["passed", "passed"], "prefix": ["failed_call"]},
        ["findings", "findings2"],
    )
    result = _run(ports)
    assert ports.delegate_calls == 2
    assert result.gate_repairs == 1
    assert result.gate_clean is False
    assert result.gate_findings == 2
    assert result.status is EnumLoopStatus.ACCEPTED_CALL


def test_a_repair_that_breaks_the_test_keeps_the_passing_test() -> None:
    ports = GatedPorts(
        {"fixed": ["passed", "failed_call"], "prefix": ["failed_call"]}, ["findings"]
    )
    result = _run(ports)
    assert ports.delegate_calls == 2
    first_passing = ports.run_sources[0][1]
    assert ports.run_sources[-1] == ("prefix", first_passing)
    assert result.status is EnumLoopStatus.ACCEPTED_CALL
    assert result.gate_clean is False
    assert result.gate_repairs == 1


def test_an_unusable_repair_reply_keeps_the_passing_test() -> None:
    bad = ModelDelegateReply(run_id="run-2", ok=False, invalid_reason="not JSON")
    first = ModelDelegateReply(
        run_id="run-1", ok=True, test_source="def test_a(): pass\n"
    )
    ports = GatedPorts(
        {"fixed": ["passed"], "prefix": ["failed_call"]},
        ["findings"],
        replies=[first, bad],
    )
    result = _run(ports)
    assert ports.delegate_calls == 2
    assert ports.run_sources[-1] == ("prefix", "def test_a(): pass\n")
    assert result.status is EnumLoopStatus.ACCEPTED_CALL


def test_a_gate_infra_error_is_never_repaired_and_never_clean() -> None:
    ports = GatedPorts({"fixed": ["passed"], "prefix": ["failed_call"]}, ["infra"])
    result = _run(ports)
    assert ports.delegate_calls == 1
    assert result.gate_repairs == 0
    assert result.gate_clean is False


def test_the_gate_repair_respects_the_three_call_bound() -> None:
    ports = GatedPorts(
        {"fixed": ["failed_call", "failed_call", "passed"], "prefix": ["failed_call"]},
        ["findings"],
    )
    result = _run(ports)
    assert ports.delegate_calls == 3
    assert result.gate_repairs == 0
    assert result.gate_clean is False
    assert result.status is EnumLoopStatus.ACCEPTED_CALL


def test_a_run_without_gate_outputs_reports_gates_as_not_run() -> None:
    ports = FakePorts({"fixed": ["passed"], "prefix": ["failed_call"]})
    result = HandlerDelegatedTestLoopOrchestrator(ports).run(_request())
    assert result.gate_clean is None
    assert result.gate_repairs == 0


def test_a_gated_loop_receipt_replays_to_the_same_status() -> None:
    ports = GatedPorts(
        {"fixed": ["passed", "failed_call"], "prefix": ["failed_call"]}, ["findings"]
    )
    result = _run(ports)
    receipt = ports.written[CORRELATION]
    steps = receipt["steps"]
    assert isinstance(steps, list)
    kinds = [s["kind"] for s in steps]
    assert "gate" in kinds
    assert replay_loop_receipt(receipt) is result.status


def test_the_result_stays_under_4kb_with_gate_fields() -> None:
    ports = GatedPorts(
        {"fixed": ["passed", "passed"], "prefix": ["failed_call"]},
        ["findings", "findings2"],
    )
    assert result_json_bytes(_run(ports)) < MAX_RESULT_BYTES


def test_the_request_runs_the_gates_by_default() -> None:
    assert ModelDelegatedTestLoopRequest.model_fields["run_code_gates"].default is True
