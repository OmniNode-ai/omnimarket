# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26640: a delegated request's output budget is the window less its measured input less the margin.

The window, the margin, the floor and the ceiling are the code budget overlay's. The port measures the
request it is about to send, asks the node for the budget, sends exactly that as ``max_tokens`` and records
it on the call result so the receipt shows what was sent. A backend the overlay does not name keeps the
routing contract's budget.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.handlers.routing.measured_correct_ceiling import CODE_BUDGET_OVERLAY_ID
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]


class _Meter:
    def __init__(self, count: int) -> None:
        self._count = count
        self.messages: list[list[dict[str, str]]] = []

    def count(
        self,
        *,
        endpoint_ref: str,
        model_id: str,
        messages: Sequence[Mapping[str, str]],
        timeout_s: float,
    ) -> int:
        self.messages.append([dict(m) for m in messages])
        return self._count


FIXTURE_OVERLAY = next(
    (REPO / "tests/nodes/node_delegation_code_budget_compute/fixtures").glob(
        f"*/{CODE_BUDGET_OVERLAY_ID}/overlay.yaml"
    )
)


def _overlay(root: Path, window: int, margin: int, ceiling: int) -> None:
    """The fixture overlay (every input ceiling the routing config needs) plus one named output budget."""
    path = root / CODE_BUDGET_OVERLAY_ID / "overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        FIXTURE_OVERLAY.read_text(encoding="utf-8")
        + "output_budgets:\n  inkling-lab:\n"
        f"    window: {window}\n    margin: {margin}\n    floor: 1024\n"
        f"    ceiling: {ceiling}\n    measure_timeout_s: 5\n",
        encoding="utf-8",
    )


def _run(
    tmp_path: Path,
    backend_id: str,
    meter: _Meter,
    max_tokens: int | None = None,
    contract_max_tokens: int = 65536,
) -> tuple[list[ModelLlmDelegationCallRequest], ModelLlmDelegationCallResult | None]:
    requests: list[ModelLlmDelegationCallRequest] = []
    results: list[ModelLlmDelegationCallResult] = []

    def server(request: ModelLlmDelegationCallRequest) -> ModelLlmDelegationCallResult:
        requests.append(request)
        result = ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content="def add(a, b):\n    return a + b\n",
            finish_reason=EnumProviderFinishReason.STOP,
            tokens_in=117,
            tokens_out=40,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )
        results.append(result)
        return result

    port = LocalDelegationDispatchPort(
        effect_handler=server,
        evidence_db_path=tmp_path / "evidence.sqlite",
        effect_process_boundary=False,
        input_token_meter=meter,
    )
    asyncio.run(
        port._run_single_attempt(
            backend=ModelResolvedDelegationBackend(
                backend_id=backend_id,
                model_id="Inkling-2-Mini",
                endpoint_ref="http://192.0.2.7:8111/v1/chat/completions",
                tier="local",
                max_tokens=contract_max_tokens,
                timeout_ms=30000,
            ),
            prompt="Write a function that adds two numbers.",
            task_type="reasoning",
            correlation_id=uuid4(),
            max_tokens=max_tokens,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
        )
    )
    return requests, results[0] if results else None


def test_the_request_carries_window_less_measured_input_less_margin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    _overlay(tmp_path, window=65536, margin=512, ceiling=65536)
    meter = _Meter(1471)
    requests, result = _run(tmp_path, "inkling-lab", meter)
    assert [r.max_tokens for r in requests] == [65536 - 1471 - 512]
    assert result is not None
    assert result.local_output_ceiling == 65536 - 1471 - 512
    assert len(meter.messages) == 1
    sent = requests[0]
    assert meter.messages[0][-1]["content"] == sent.prompt


def test_the_budget_follows_the_overlays_window_not_the_contract_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    _overlay(tmp_path, window=114688, margin=1024, ceiling=114688)
    requests, _ = _run(tmp_path, "inkling-lab", _Meter(2000), contract_max_tokens=32768)
    assert [r.max_tokens for r in requests] == [114688 - 2000 - 1024]


def test_a_larger_input_leaves_a_smaller_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    _overlay(tmp_path, window=65536, margin=512, ceiling=65536)
    small, _ = _run(tmp_path, "inkling-lab", _Meter(833))
    large, _ = _run(tmp_path, "inkling-lab", _Meter(30000))
    assert small[0].max_tokens == 65536 - 833 - 512
    assert large[0].max_tokens == 65536 - 30000 - 512


def test_the_callers_smaller_cap_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    _overlay(tmp_path, window=65536, margin=512, ceiling=65536)
    requests, _ = _run(tmp_path, "inkling-lab", _Meter(1000), max_tokens=2048)
    assert [r.max_tokens for r in requests] == [2048]


def test_a_backend_the_overlay_does_not_name_keeps_the_contract_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    _overlay(tmp_path, window=65536, margin=512, ceiling=65536)
    meter = _Meter(1000)
    requests, _ = _run(tmp_path, "some-other-backend", meter, contract_max_tokens=4096)
    assert [r.max_tokens for r in requests] == [4096]
    assert meter.messages == []


def test_no_literal_output_cap_or_margin_is_in_the_budget_code() -> None:
    sources = [
        REPO / "src/omnimarket/handlers/delegation_output_budget.py",
        REPO / "src/omnimarket/handlers/routing/vllm_input_token_meter.py",
        REPO
        / "src/omnimarket/nodes/node_delegation_code_budget_compute/handlers/handler_delegation_code_budget.py",
    ]
    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert "32768" not in text, source
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        assert re.search(r"\b(?:margin|window|ceiling|floor)\s*=\s*\d", code) is None, (
            source
        )
