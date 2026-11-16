# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20172: the local dispatch port climbs swap, shrink, escalate on a rubric failure.

Drives ``LocalDelegationDispatchPort.dispatch`` for a code_review whose class
rubric is configured MET. The whole request is answered with a citation that
does not exist (``rubric_failed``); a part of it is answered with a citation
that does. The effect handler is injected, so no model is called.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.delegation.rubric import attempt_verdict
from omnimarket.delegation.rubric.contract_loader import load_delegation_class_rubrics
from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation.wire.model_attempt_configuration import (
    ModelAttemptConfiguration,
)
from omnimarket.models.delegation_capability_map.enum_capability_cell_verdict import (
    EnumCapabilityCellVerdict,
)
from omnimarket.models.delegation_capability_map.model_capability_cell import (
    ModelCapabilityCell,
)
from omnimarket.models.ranges import EnumRangeVerdict
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)

pytestmark = pytest.mark.unit

_LADDER = ("local", "cheap_cloud")
_BACKEND = {"local": "local-coder", "cheap_cloud": "cloud-glm"}
_TIER = {value: key for key, value in _BACKEND.items()}
_MODEL = {"local": "Qwen3.6-35B-A3B", "cheap_cloud": "glm-5.2"}
_FILES = (("src/alpha.py", "alpha_value"), ("src/beta.py", "beta_value"))
_PREFIX = "Review this synthetic diff:\n"
_MARKER = "### ANSWER\n"
_BAD = "src/absent.py:1 has a defect."
_TEMPERATURE = 0.0
_TENANT = UUID("00000000-0000-0000-0000-000000000001")


def _diff() -> str:
    return _PREFIX + "".join(
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1 +1 @@\n-{name} = 1\n+{name} = 2\n"
        for path, name in _FILES
    )


def _backend(tier: str) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id=_BACKEND[tier],
        model_id=_MODEL[tier],
        endpoint_ref=f"https://{tier}-host.example:8443/v1/chat/completions",
        tier=tier,
        max_tokens=4096,
        timeout_ms=30000,
    )


class _Effect:
    """Answers the whole request badly and each part well; a swap answers well."""

    def __init__(self, *, good_at_temperature: float | None = None) -> None:
        self.calls: list[ModelLlmDelegationCallRequest] = []
        self.good_at_temperature = good_at_temperature

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request)
        match = re.findall(r"diff --git a/(\S+) b/", request.prompt)
        if (
            self.good_at_temperature is not None
            and request.temperature == self.good_at_temperature
        ) or len(match) == 1:
            # a part (or a swapped retry): cite each file in it, which exists
            content = _MARKER + (
                "\n".join(
                    f"{path}:1 `{name} = 2`" for path, name in _FILES if path in match
                )
                or "\n".join(f"{path}:1 `{name} = 2`" for path, name in _FILES)
            )
        else:
            content = _MARKER + _BAD
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=content,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


def _arm(monkeypatch: pytest.MonkeyPatch, *, shrink_caps: bool = True) -> None:
    """The ladder, the MET rubric for code_review and a large measured band."""

    def resolve(task_type: str, *, backend_id: str | None = None) -> Any:
        return _backend("local" if backend_id is None else _TIER[backend_id])

    def next_tier(current: str, excluded: Any, **_: Any) -> str | None:
        later = _LADDER[_LADDER.index(current) + 1 :]
        return next((tier for tier in later if tier not in excluded), None)

    monkeypatch.setattr(port_mod, "resolve_delegation_backend", resolve)
    monkeypatch.setattr(port_mod, "next_eligible_tier", next_tier)
    monkeypatch.setattr(port_mod, "first_eligible_tier", lambda *_a, **_k: "local")
    monkeypatch.setattr(
        port_mod, "backend_id_for_tier", lambda t, *_a, **_k: _BACKEND[t]
    )
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda b: _TIER.get(b))
    monkeypatch.setattr(
        port_mod, "sibling_backend_available_in_tier", lambda *_a, **_k: None
    )
    monkeypatch.setattr(port_mod, "resolve_task_class_max_escalations", lambda _t: 2)
    monkeypatch.setattr(port_mod, "is_free_tier", lambda _tier: False)
    monkeypatch.setattr(port_mod, "_measured_size_band", lambda **_: EnumSizeBand.L)
    contract = load_delegation_class_rubrics()
    raw = contract.model_dump(mode="json")
    raw["false_pass_status"]["code_review"] = EnumRangeVerdict.MET.value
    configured = type(contract).model_validate(raw)
    monkeypatch.setattr(attempt_verdict, "_load_contract", lambda: configured)
    if not shrink_caps:
        monkeypatch.setattr(port_mod, "resolve_task_class_shrink_caps", lambda _t: None)


class _Cells:
    """The capability map's stored cells for code_review, as a reader returns them."""

    def __init__(self, cells: tuple[ModelCapabilityCell, ...]) -> None:
        self._cells = cells

    def read_cells(
        self, *, task_class: str, tenant_id: str
    ) -> tuple[ModelCapabilityCell, ...]:
        return self._cells


def _cell(
    configuration: ModelAttemptConfiguration, cell_id: str, bound: float
) -> ModelCapabilityCell:
    return ModelCapabilityCell(
        tenant_id=_TENANT,
        map_run_id="run-1",
        model_key=_MODEL["local"],
        backend_id=_BACKEND["local"],
        configuration_id=cell_id,
        configuration_parameters=configuration,
        task_class="code_review",
        size_band=EnumSizeBand.L,
        n=120,
        passes=118,
        pass_rate=118 / 120,
        wilson_low=0.9,
        wilson_high=0.99,
        decision_lower_bound=bound,
        floor=0.9,
        required_n=89,
        verdict=EnumCapabilityCellVerdict.MET,
    )


def _run(
    tmp_path: Path, effect: _Effect, *, reader: _Cells | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    db_path = tmp_path / "delegation.sqlite"
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=db_path,
        effect_process_boundary=False,
        capability_cell_reader=reader,
    )
    correlation_id = uuid4()
    result: dict[str, Any] = asyncio.run(
        port.dispatch(
            prompt=_diff(),
            task_type="code_review",
            correlation_id=correlation_id,
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM delegation_events WHERE correlation_id = ?",
            (str(correlation_id),),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, "the evidence write produced no row"
    return result, json.loads(row["attempt_history"])


def test_shrink_retry_same_model_then_whole_check_through_the_local_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch)
    effect = _Effect()
    result, stored = _run(tmp_path, effect)

    assert result["status"] == "completed"
    # The failed whole, one attempt per file on the same backend and model,
    # then the check of the recombined whole.
    assert [rung["attempt_kind"] for rung in stored] == [
        "first_try",
        "split_child",
        "split_child",
        "recombine_check",
    ]
    assert [rung["backend_id"] for rung in stored] == ["local-coder"] * 4
    assert [rung["model_id"] for rung in stored] == [_MODEL["local"]] * 4
    assert [rung["quality_gate_passed"] for rung in stored] == [
        False,
        True,
        True,
        True,
    ]
    first = stored[0]
    assert first["failure_class"] == "rubric_failed"
    assert {r["split_id"] for r in stored[1:]} != {None}
    assert {r["parent_attempt_id"] for r in stored[1:]} == {first["attempt_id"]}
    # Nothing escalated: the cloud responder was never asked.
    assert {c.model_tier for c in effect.calls} == {"local"}
    # The accepted content is the recombined parts, in source order.
    assert [line for line in result["content"].splitlines() if line.strip()] == [
        f"{path}:1 `{name} = 2`" for path, name in _FILES
    ]
    # Every event decodes as a typed attempt record.
    from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
        _attempt_records,
    )

    typed = _attempt_records(result)
    assert [record.attempt_kind.value for record in typed if record.attempt_kind] == [
        "first_try",
        "split_child",
        "split_child",
        "recombine_check",
    ]


def test_without_declared_caps_the_rubric_failure_escalates_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failing control: no caps, no ladder; the chain escalates and no part is cut."""
    _arm(monkeypatch, shrink_caps=False)
    effect = _Effect()
    _, stored = _run(tmp_path, effect)

    kinds = [rung["attempt_kind"] for rung in stored]
    assert "split_child" not in kinds
    assert "recombine_check" not in kinds
    assert "cheap_cloud" in {c.model_tier for c in effect.calls}


def test_config_swap_before_shrink_through_the_local_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch)
    current = ModelAttemptConfiguration(max_tokens=256, temperature=None)
    second = ModelAttemptConfiguration(max_tokens=256, temperature=_TEMPERATURE)
    reader = _Cells(
        (_cell(current, "cfg-current", 0.95), _cell(second, "cfg-second", 0.93))
    )
    effect = _Effect(good_at_temperature=_TEMPERATURE)
    result, stored = _run(tmp_path, effect, reader=reader)

    assert result["status"] == "completed"
    # One swap on the same model and backend, accepted: no shrink, no escalation.
    assert [rung["attempt_kind"] for rung in stored] == ["first_try", "config_swap"]
    assert [rung["backend_id"] for rung in stored] == ["local-coder"] * 2
    assert [rung["model_id"] for rung in stored] == [_MODEL["local"]] * 2
    assert stored[0]["configuration"]["temperature"] is None
    assert stored[1]["configuration"]["temperature"] == _TEMPERATURE
    assert {c.model_tier for c in effect.calls} == {"local"}


def test_config_swap_capped_through_the_local_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The swapped retry fails too: a second swap is refused and the ladder shrinks."""
    _arm(monkeypatch)
    current = ModelAttemptConfiguration(max_tokens=256, temperature=None)
    second = ModelAttemptConfiguration(max_tokens=256, temperature=_TEMPERATURE)
    reader = _Cells(
        (_cell(current, "cfg-current", 0.95), _cell(second, "cfg-second", 0.93))
    )
    effect = _Effect()  # the swapped retry on the whole still cites a missing file
    _, stored = _run(tmp_path, effect, reader=reader)

    kinds = [rung["attempt_kind"] for rung in stored]
    assert kinds.count("config_swap") == 1
    assert kinds[:2] == ["first_try", "config_swap"]
    assert "split_child" in kinds
    assert kinds[-1] == "recombine_check"
