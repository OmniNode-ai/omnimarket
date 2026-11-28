# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26122: the healthy liveness submission is graded by a class whose gate it can pass.

Recorded C16 run 46064337534 (stability lane, job 140725906798, step 11 "Drive the healthy and the
dying delegation and grade C16"): ``C16 RED -- R-DELEG-11 SKIP: the healthy run produced no completed
receipt (receipt status='failed')`` and ``R-DELEG-12 SKIP ... (submit=202, receipt status='failed')``.
The probe's tenant is customer-attributed, so its overlay row is a single rung with no lawful successor
(INV-068) and a refusal by the quality gate ends the run. The submission was a liveness prompt classed
``test``, whose gate grades one pytest artifact marked ``@pytest.mark.unit``: the prompt asked for a
bare test function, and the real gate refuses even a well-formed answer to it (score 0.85 with
TASK_MISMATCH: missing @pytest.mark.unit).

These tests send each check's healthy submission through the real quality gate with the answer a
compliant model gives and require a pass. The old pair is the known-bad control.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_golden_answers_gateway_graders import GATEWAY_CHECKS, _real, _run

from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

OLD_TASK_TYPE = "test"
OLD_PROMPT = (
    "Reply with only a python code block holding one pytest test function named test_alive "
    "that asserts True. No prose outside the code block. Automated C16 delegation check."
)
OLD_ANSWER = "```python\ndef test_alive():\n    assert True\n```"
LIVE_ANSWER = "alive"


def _gate(task_type: str, prompt: str, answer: str) -> tuple[bool, tuple[str, ...]]:
    deterministic, heuristic = resolve_task_class_dod_checks(task_type, prompt=prompt)
    result = delta(
        ModelQualityGateInput(
            correlation_id=UUID(int=26122),
            task_type=task_type,
            llm_response_content=answer,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        finish_reason=EnumProviderFinishReason.STOP,
    )
    return result.passed, tuple(result.failure_reasons)


def test_known_bad_the_old_class_and_prompt_pair_is_refused_by_the_real_gate() -> None:
    passed, reasons = _gate(OLD_TASK_TYPE, OLD_PROMPT, OLD_ANSWER)
    assert not passed
    assert any("TASK_MISMATCH" in r for r in reasons)


@pytest.mark.parametrize("check", GATEWAY_CHECKS)
def test_every_healthy_submission_passes_the_real_gate_on_a_compliant_answer(
    check: str,
) -> None:
    gateway = _real("api_key")
    _run(check, gateway)
    sent = [
        s["body"]["payload"]
        for s in gateway.sent
        if s["path"] == "/v1/workflows"
        and s["body"]["payload"]["task_type"] != "omn19181_not_a_task_class"
        and s["body"]["payload"]["source"] != "not-a-source"
    ]
    # C11, C12 and C17 submit no healthy run through this gateway; the control below reads a check that does.
    for payload in sent:
        passed, reasons = _gate(payload["task_type"], payload["prompt"], LIVE_ANSWER)
        assert passed, (payload["task_type"], reasons)


def test_the_control_reads_a_healthy_submission_so_the_loop_above_is_not_vacuous() -> (
    None
):
    gateway = _real("api_key")
    _run("C16", gateway)
    healthy = [
        s["body"]["payload"]
        for s in gateway.sent
        if s["path"] == "/v1/workflows"
        and s["body"]["payload"]["task_type"] != "omn19181_not_a_task_class"
    ]
    assert healthy
    assert {p["task_type"] for p in healthy} == {"document"}
