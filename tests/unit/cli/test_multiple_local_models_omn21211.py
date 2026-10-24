# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A developer registers several local models, in order (OMN-21211).

``onex models add local`` used to replace the one ``local-url`` entry, so a
second local model could not be declared. Each add now places its models after
the ones already there, ``onex models list`` shows them in that order, and a
run on this machine tries them in that order before its ladder, so before any
key. The server is a real HTTP server on 127.0.0.1 and the pinned test
delegation is the stub the OMN-21162 tests use.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from click.testing import CliRunner

from omnimarket.cli.cli_secret import models_group
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.routing.local_byok_route import registered_local_model_routes
from tests.unit.cli.test_model_setup_local_url_omn21162 import (
    _Delegations,
    delegations,
    server,
    state,
)

pytestmark = pytest.mark.unit

# The fixtures this module shares with the OMN-21162 tests.
__all__ = ["delegations", "server", "state"]

_FIRST = "qwen3-coder-30b"
_SECOND = "llama-3.3-8b"
_KEY_ROUTE = "byok-openrouter"
_ANSWER = (
    "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the "
    "tradeoff is significant because the evidence shows X; therefore we conclude "
    "Y. See references [12] for the methodical analysis and the risk profile this "
    "approach carries in practice."
)


class _ModelPerBackend(_Delegations):
    """Answers each pinned local backend on the model the overlay declares for it."""

    def __init__(self, tmp_path: Path, state_dir: Path) -> None:
        super().__init__(tmp_path)
        self.state_dir = state_dir

    def __call__(self, backend_id: str) -> tuple[int, str, str]:
        self.answer_model = next(
            b["model_name"]
            for b in _backends(self.state_dir)
            if b["backend_id"] == backend_id
        )
        return super().__call__(backend_id)


@pytest.fixture
def pinned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: Path
) -> _ModelPerBackend:
    fake = _ModelPerBackend(tmp_path, state)
    monkeypatch.setattr(
        "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup."
        "run_pinned_delegation",
        fake,
    )
    return fake


def _models(*args: str) -> Any:
    return CliRunner().invoke(models_group, list(args))


def _backends(state_dir: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load((state_dir / "bifrost_overrides.yaml").read_text("utf-8"))
    return list(data.get("backends") or [])


def _local_row() -> dict[str, Any]:
    rows = (json.loads(line) for line in _models("list", "--json").output.splitlines())
    return next(row for row in rows if row["provider"] == "local")


def test_multiple_local_models_are_listed_in_the_order_they_were_registered(
    server: str, pinned: _ModelPerBackend, state: Path
) -> None:
    first = _models("add", "local", "--url", server, "--model", _FIRST)
    assert first.exit_code == 0, first.output
    second = _models("add", "local", "--url", server, "--model", _SECOND)
    assert second.exit_code == 0, second.output

    # The second add did not replace the first: both are declared, in order.
    assert [(b["backend_id"], b["model_name"]) for b in _backends(state)] == [
        ("local-url", _FIRST),
        ("local-url-2", _SECOND),
    ]
    assert pinned.pinned == ["local-url", "local-url-2"]
    assert _local_row()["models"] == [_FIRST, _SECOND]
    assert f"[models: {_FIRST}, {_SECOND}]" in _models("list").output


def test_multiple_local_models_in_one_add_keep_the_order_given(
    server: str, pinned: _ModelPerBackend, state: Path
) -> None:
    done = _models(
        "add", "local", "--url", server, "--model", _SECOND, "--model", _FIRST
    )
    assert done.exit_code == 0, done.output
    assert _local_row()["models"] == [_SECOND, _FIRST]
    # Adding a model already declared at this URL keeps its place.
    again = _models("add", "local", "--url", server, "--model", _SECOND)
    assert again.exit_code == 0, again.output
    assert _local_row()["models"] == [_SECOND, _FIRST]


def test_multiple_local_models_reorder_and_remove_one(
    server: str, pinned: _ModelPerBackend, state: Path
) -> None:
    assert _models("add", "local", "--url", server, "--model", _FIRST).exit_code == 0
    assert _models("add", "local", "--url", server, "--model", _SECOND).exit_code == 0

    reordered = _models("reorder", "local", _SECOND, _FIRST)
    assert reordered.exit_code == 0, reordered.output
    assert _local_row()["models"] == [_SECOND, _FIRST]
    refused = _models("reorder", "local", _FIRST)
    assert refused.exit_code != 0
    assert "names exactly your local models" in refused.output

    removed = _models("remove", "local", "--model", _SECOND)
    assert removed.exit_code == 0, removed.output
    assert _local_row()["models"] == [_FIRST]
    assert _models("remove", "local").exit_code == 0
    assert _local_row()["set_up"] is False


class _Effect:
    """Every local model is unavailable; the key's route answers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append((request.provider, request.model_id))
        failed = request.model_id in (_FIRST, _SECOND)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=not failed,
            content="" if failed else _ANSWER,
            tokens_in=0 if failed else 11,
            tokens_out=0 if failed else 22,
            failure_class=(
                EnumDelegationFailureClass.MODEL_UNAVAILABLE if failed else None
            ),
            http_status=503 if failed else None,
            error_message="upstream unavailable" if failed else None,
        )


def test_multiple_local_models_are_tried_in_order_before_any_key(
    server: str,
    pinned: _ModelPerBackend,
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert _models("add", "local", "--url", server, "--model", _FIRST).exit_code == 0
    assert _models("add", "local", "--url", server, "--model", _SECOND).exit_code == 0
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(state / "bifrost_overrides.yaml"))

    # The run's ladder would start on the customer's key.
    def key_route(
        self: LocalDelegationDispatchPort, task_type: str, **_: object
    ) -> ModelResolvedDelegationBackend:
        del self
        local = registered_local_model_routes(task_type)[0]
        return local.model_copy(
            update={
                "backend_id": _KEY_ROUTE,
                "model_id": "key-model",
                "endpoint_ref": "http://127.0.0.1:9/v1/chat/completions",
            }
        )

    monkeypatch.setattr(
        LocalDelegationDispatchPort, "_resolve_initial_backend", key_route
    )
    effect = _Effect()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )

    result: dict[str, Any] = asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff",
            task_type="research",
            correlation_id=uuid4(),
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

    assert [model for _, model in effect.calls] == [_FIRST, _SECOND, "key-model"]
    assert [a["backend_id"] for a in result["attempts"]] == [
        "local-url",
        "local-url-2",
        _KEY_ROUTE,
    ]
    assert result["status"] == "completed"
