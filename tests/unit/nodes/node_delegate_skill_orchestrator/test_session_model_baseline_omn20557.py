# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20557: the savings baseline is the caller's Claude Code session model.

The /onex:delegate plugin hands ``onex delegate`` the session's model id, and
the CLI puts it on the request's ``metadata`` map under ``session_model``.
These tests start at that request and run the real local path --
``HandlerDelegateSkill`` over ``LocalDelegationDispatchPort`` with its default
evidence store on a fresh HOME -- with only the provider stubbed at the
transport boundary.

* AC1: a priced session model is the run's baseline: the receipt names
  ``baseline_source: session_model`` and that model, the saving is stated
  against it, and the metering rows ``onex metering`` prints are folded under it.
* AC2: an unpriced session model is ``BASELINE_UNRESOLVED`` with a null saving,
  never the fixed default and never a number.
* AC3: no session model still resolves the fixed default, and says so.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from click.testing import CliRunner

from omnimarket import pricing
from omnimarket.cli.cli_metering import metering_command
from omnimarket.local_deployment import tenant_identity
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_named_test_run import ModelNamedTestRun
from omnimarket.models.delegation.wire.model_named_test_run_request import (
    EnumNamedTestRunStatus,
    ModelNamedTestRunRequest,
    ModelNamedTestRunResult,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call,
    transport,
)
from omnimarket.projection import sqlite_database
from omnimarket.routing import delegation_backend_resolution

pytestmark = pytest.mark.unit

SESSION_MODEL = "claude-opus-5-5"
UNPRICED_SESSION_MODEL = "claude-unpriced-session-omn20557"
_TOKENS_IN = 1200
_TOKENS_OUT = 300


@pytest.fixture(autouse=True)
def _clear_health_cache() -> None:
    handler_llm_delegation_call._health_cache.clear()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A fresh HOME under tmp_path: never the real ~/.omninode or ~/.onex."""
    root = tmp_path / "home"
    store = root / ".omninode" / "delegation" / "delegation.sqlite"
    store.parent.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setattr(sqlite_database, "_DEFAULT_EVIDENCE_DB_PATH", store)
    monkeypatch.setattr(tenant_identity, "default_evidence_db_path", lambda: store)
    tenant_identity.reset_local_tenant_identity_cache()
    backends: list[dict[str, object]] = [
        {
            "backend_id": "local-coder",
            "endpoint_url": "http://inference.example:8000/v1/chat/completions",
            "model_name": "Qwen3.6-35B-A3B",
            "tier": "local",
            "max_tokens": 65536,
            "timeout_ms": 300000,
            "capabilities": ["code_generation"],
        }
    ]
    monkeypatch.setattr(
        delegation_backend_resolution, "load_bifrost_backends", lambda **_: backends
    )

    def fake_post(**_: Any) -> transport.ModelTransportResponse:
        return transport.ModelTransportResponse(
            status_code=200,
            json_body={
                "choices": [
                    {
                        "message": {
                            "content": "### ANSWER\ndef reverse(s): return s[::-1]"
                        }
                    }
                ],
                "model": "Qwen3.6-35B-A3B",
                "usage": {
                    "prompt_tokens": _TOKENS_IN,
                    "completion_tokens": _TOKENS_OUT,
                    "total_tokens": _TOKENS_IN + _TOKENS_OUT,
                },
            },
            latency_ms=42,
        )

    monkeypatch.setattr(transport, "probe_health", lambda *_a, **_k: True)
    monkeypatch.setattr(transport, "post_chat_completion", fake_post)
    yield store
    tenant_identity.reset_local_tenant_identity_cache()


def _passing_named_test_runner(
    request: ModelNamedTestRunRequest,
) -> ModelNamedTestRunResult:
    return ModelNamedTestRunResult(
        status=EnumNamedTestRunStatus.COMPLETED,
        run=ModelNamedTestRun(test_ids=request.spec.test_ids, exit_code=0),
    )


def _request(session_model: str | None) -> ModelDelegateSkillRequest:
    metadata = {
        "named_tests": "test_reverse.py::test_reverse",
        "named_tests_answer_path": "reverse_impl.py",
        "named_tests_workspace": str(Path.cwd()),
    }
    if session_model is not None:
        metadata["session_model"] = session_model
    return ModelDelegateSkillRequest(
        metadata=metadata,
        prompt="reverse a string",
        task_type="code_generation",
        source="claude-code",
    )


def _delegate(session_model: str | None) -> Any:
    port = LocalDelegationDispatchPort(
        named_test_runner=_passing_named_test_runner,
        effect_process_boundary=False,
    )
    handler = HandlerDelegateSkill(None, dispatch_port=port)
    return asyncio.run(handler.handle(_request(session_model)))


def _folded_baselines(store: Path) -> set[str]:
    conn = sqlite3.connect(store)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT baseline_model FROM metering_summary WHERE window_kind = 'all'"
            ).fetchall()
        }
    finally:
        conn.close()


def _metering_json() -> dict[str, Any]:
    result = CliRunner().invoke(metering_command, ["--json"])
    assert result.exit_code == 0, result.output
    printed: dict[str, Any] = json.loads(result.stdout)
    return printed


def _expected_saving(model: str, response: Any) -> float | None:
    return pricing.estimate_baseline_savings_usd(
        prompt_tokens=_TOKENS_IN,
        completion_tokens=_TOKENS_OUT,
        actual_cost_usd=float(response.metrics.cost_usd or 0.0),
        baseline_model=model,
    )


def test_session_model_on_the_request_is_the_receipt_baseline(home: Path) -> None:
    """AC1: the receipt names session_model and the session's model."""
    assert (
        pricing.resolve_baseline_model(
            overlay={}, store={}, session_model=SESSION_MODEL
        ).state
        == "RESOLVED"
    ), "positive control: the session model must be priced"
    assert SESSION_MODEL != pricing.DEFAULT_BASELINE_MODEL

    response = _delegate(SESSION_MODEL)

    assert response.status == "completed"
    assert response.baseline_source == "session_model"
    assert response.model_cloud_baseline == SESSION_MODEL
    assert response.baseline_state == "RESOLVED"
    saving = response.metrics.cost_savings_usd
    assert saving is not None
    assert saving == pytest.approx(_expected_saving(SESSION_MODEL, response))
    assert saving != pytest.approx(
        _expected_saving(pricing.DEFAULT_BASELINE_MODEL, response)
    )


def test_session_model_is_the_metering_baseline_onex_metering_prints(
    home: Path,
) -> None:
    """AC1/AC4 local half: the run's rows are folded under the session model."""
    response = _delegate(SESSION_MODEL)
    assert response.status == "completed"

    assert _folded_baselines(home) == {SESSION_MODEL}
    printed = _metering_json()
    assert printed["baseline_model"] == SESSION_MODEL
    assert printed["runs_total"] == 1


def test_unpriced_session_model_is_unresolved_with_null_savings(home: Path) -> None:
    """AC2: never the fixed default and never a number."""
    response = _delegate(UNPRICED_SESSION_MODEL)

    assert response.status == "completed"
    assert response.baseline_source == "session_model"
    assert response.model_cloud_baseline == UNPRICED_SESSION_MODEL
    assert response.baseline_state == "BASELINE_UNRESOLVED"
    assert response.metrics.cost_savings_usd is None
    assert _folded_baselines(home) == {UNPRICED_SESSION_MODEL}


def test_no_session_model_resolves_the_fixed_default_and_says_so(
    home: Path,
) -> None:
    """AC3: the receipt states the selection case."""
    response = _delegate(None)

    assert response.status == "completed"
    assert response.baseline_source == "fixed_default"
    assert response.model_cloud_baseline == pricing.DEFAULT_BASELINE_MODEL
    assert response.baseline_state == "RESOLVED"
    assert _folded_baselines(home) == {pricing.DEFAULT_BASELINE_MODEL}
    assert _metering_json()["baseline_model"] == pricing.DEFAULT_BASELINE_MODEL


def test_blank_session_model_is_no_session_model(home: Path) -> None:
    response = _delegate("   ")

    assert response.baseline_source == "fixed_default"
    assert response.model_cloud_baseline == pricing.DEFAULT_BASELINE_MODEL


def test_onex_metering_reads_the_baseline_of_the_newest_run(home: Path) -> None:
    """A later run with no session model is printed under the fixed default."""
    _delegate(SESSION_MODEL)
    assert _metering_json()["baseline_model"] == SESSION_MODEL

    _delegate(None)
    printed = _metering_json()
    assert printed["baseline_model"] == pricing.DEFAULT_BASELINE_MODEL
    assert printed["runs_total"] == 2


class _PortWithoutSessionModel:
    """A port that does not declare ``session_model``, as infra's runtime port.

    Its saving is priced against its own baseline, so the handler must neither
    pass it the keyword nor keep that saving under the session model's name.
    """

    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    async def dispatch(
        self,
        *,
        prompt: str,
        task_type: str,
        correlation_id: object,
        max_tokens: int | None,
        source_file_path: str | None,
        source_session_id: str | None,
        wait: bool,
        execution_timeout_seconds: int,
        terminal_delivery_margin_seconds: int,
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
        tenant_id: str | None,
        provenance: object = None,
        backend_id: str | None = None,
        response_contract: dict[str, object] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        response_format: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.kwargs = {"prompt": prompt, "task_type": task_type}
        return {
            "status": "completed",
            "content": "answer",
            "delegated_to": "cheap-cloud",
            "model_name": "gemini-2.5-flash",
            "quality_gate_passed": True,
            "quality_score": 1.0,
            "cost_usd": 0.001,
            "input_tokens": _TOKENS_IN,
            "output_tokens": _TOKENS_OUT,
            "cost_savings_usd": 99.0,
        }


def test_a_port_without_the_keyword_never_names_its_saving_as_the_session_one() -> None:
    port = _PortWithoutSessionModel()
    handler = HandlerDelegateSkill(None, dispatch_port=cast(Any, port))
    response = asyncio.run(
        handler.handle(
            ModelDelegateSkillRequest(
                metadata={"session_model": SESSION_MODEL},
                prompt="summarise this",
                task_type="document",
                source="claude-code",
            )
        )
    )

    assert port.kwargs, "the port was dispatched"
    assert response is not None
    assert response.baseline_source == "session_model"
    assert response.model_cloud_baseline == SESSION_MODEL
    assert response.metrics.cost_savings_usd != 99.0
    assert response.metrics.cost_savings_usd == pytest.approx(
        pricing.estimate_baseline_savings_usd(
            prompt_tokens=_TOKENS_IN,
            completion_tokens=_TOKENS_OUT,
            actual_cost_usd=0.001,
            baseline_model=SESSION_MODEL,
        )
    )
