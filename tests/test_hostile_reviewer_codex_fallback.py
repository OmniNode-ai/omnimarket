# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hostile reviewer falls back to Codex when its review model is offline (OMN-17427).

Operator ruling 2026-10-10T15:10:25Z: when the code review model is off-line the
review runs on Codex until it is back. The contract declares the fallback route;
the orchestrator takes it automatically when no requested route produced review
output, records which engine reviewed on the completed event, and still fails
closed when no engine ran at all.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.inference.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceAdapter,
    ModelInferenceBridgeConfig,
)
from omnimarket.nodes.node_github_diff_effect.handlers.handler_github_diff import (
    HandlerGithubDiffEffect,
)
from omnimarket.nodes.node_github_diff_effect.models.model_github_diff import (
    ModelGithubDiffCommand,
    ModelGithubDiffResolvedEvent,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.handlers.handler_hostile_reviewer_orchestrator import (
    HandlerHostileReviewerOrchestrator,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.model_config_loader import (
    build_model_configs,
    load_fallback_route,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.models.model_hostile_reviewer_phase import (
    EnumHostileReviewerPhase,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.models.model_hostile_reviewer_start_command import (
    ModelHostileReviewerStartCommand,
)

pytestmark = pytest.mark.unit

_FINDING = json.dumps(
    [
        {
            "category": "logic_error",
            "severity": "major",
            "title": "Off by one",
            "description": "The loop bound skips the last element.",
            "location": "src/foo.py",
        }
    ]
)
_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_hostile_reviewer_orchestrator/contract.yaml"
)


class _StubDiff(HandlerGithubDiffEffect):
    def __init__(self) -> None:  # no network, no secrets
        pass

    async def handle(self, request: ModelGithubDiffCommand) -> ModelHandlerOutput[None]:
        content = "diff --git a/foo.py\n+print('x')"
        event = ModelGithubDiffResolvedEvent(
            correlation_id=request.correlation_id,
            repo=request.repo,
            pr_number=request.pr_number,
            file_path=request.file_path,
            content=content,
            content_chars=len(content),
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="node_github_diff_effect",
            events=(event,),
        )


class _RoutedAdapter(ModelInferenceAdapter):
    """Per-route behaviour: a str is the response, an Exception is raised."""

    def __init__(self, routes: dict[str, str | Exception]) -> None:
        self._routes = routes
        self.called: list[tuple[str, float]] = []

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
        response_format: object | None = None,
        protocol_selection: object | None = None,
    ) -> str:
        self.called.append((model_key, timeout_seconds))
        outcome = self._routes[model_key]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _command(models: list[str]) -> ModelHostileReviewerStartCommand:
    return ModelHostileReviewerStartCommand(
        correlation_id=uuid4(),
        pr_number=7,
        repo="OmniNode-ai/test",
        models=models,
        requested_at=datetime.now(tz=UTC),
    )


def _handler(adapter: ModelInferenceAdapter) -> HandlerHostileReviewerOrchestrator:
    return HandlerHostileReviewerOrchestrator(
        inference_adapter=adapter, github_diff_effect=_StubDiff()
    )


async def test_offline_primary_falls_back_to_codex_and_records_the_engine() -> None:
    adapter = _RoutedAdapter(
        {"primary": ConnectionError("connection refused"), "codex": _FINDING}
    )

    completed = await _handler(adapter).handle(_command(["primary"]))

    assert completed.final_phase == EnumHostileReviewerPhase.DONE
    assert completed.fallback_engine == "codex"
    assert completed.fallback_for == ["primary"]
    assert completed.models_succeeded == ["codex"]
    assert completed.models_failed == ["primary"]
    assert completed.total_findings == 1
    assert completed.error_message is None


async def test_healthy_primary_never_touches_the_fallback() -> None:
    adapter = _RoutedAdapter({"primary": "[]", "codex": _FINDING})

    completed = await _handler(adapter).handle(_command(["primary"]))

    assert completed.final_phase == EnumHostileReviewerPhase.DONE
    assert completed.fallback_engine is None
    assert completed.fallback_for == []
    assert [key for key, _ in adapter.called] == ["primary"]


async def test_offline_primary_and_offline_codex_is_never_a_pass() -> None:
    adapter = _RoutedAdapter(
        {
            "primary": ConnectionError("connection refused"),
            "codex": FileNotFoundError("codex: not found"),
        }
    )

    completed = await _handler(adapter).handle(_command(["primary"]))

    assert completed.final_phase == EnumHostileReviewerPhase.FAILED
    assert completed.fallback_engine is None
    assert completed.models_succeeded == []
    assert completed.models_failed == ["primary"]
    assert completed.error_message is not None
    assert "primary" in completed.error_message
    assert "codex" in completed.error_message
    assert "not found" in completed.error_message


@pytest.mark.parametrize("silent_output", ["", "   \n", "I looked and it seems fine."])
async def test_primary_that_ran_no_review_counts_as_offline(
    silent_output: str,
) -> None:
    # An empty or non-JSON reply is not a clean review: no engine reviewed.
    adapter = _RoutedAdapter({"primary": silent_output, "codex": _FINDING})

    completed = await _handler(adapter).handle(_command(["primary"]))

    assert completed.final_phase == EnumHostileReviewerPhase.DONE
    assert completed.fallback_engine == "codex"
    assert completed.models_failed == ["primary"]


async def test_silent_primary_without_a_working_codex_fails_closed() -> None:
    adapter = _RoutedAdapter({"primary": "", "codex": ""})

    completed = await _handler(adapter).handle(_command(["primary"]))

    assert completed.final_phase == EnumHostileReviewerPhase.FAILED
    assert completed.models_succeeded == []


async def test_a_requested_codex_route_is_not_retried_as_its_own_fallback() -> None:
    adapter = _RoutedAdapter({"codex": ConnectionError("down")})

    completed = await _handler(adapter).handle(_command(["codex"]))

    assert completed.final_phase == EnumHostileReviewerPhase.FAILED
    assert [key for key, _ in adapter.called] == ["codex"]


async def test_fallback_uses_the_contract_timeout_not_the_default_review_timeout() -> (
    None
):
    adapter = _RoutedAdapter({"primary": ConnectionError("down"), "codex": "[]"})

    await _handler(adapter).handle(_command(["primary"]))

    route = load_fallback_route()
    assert route is not None
    assert dict(adapter.called)["codex"] == route.timeout_seconds
    assert route.timeout_seconds > dict(adapter.called)["primary"]


def test_contract_declares_the_codex_fallback_route() -> None:
    routing = yaml.safe_load(_CONTRACT.read_text())["model_routing"]

    assert routing["fallback"]["route_key"] == "codex"
    assert routing["fallback"]["default_route"]["transport"] == "cli"
    assert routing["fallback"]["default_route"]["cli_command"] == "codex"


def test_fallback_route_defaults_from_the_contract_and_env_overrides_it() -> None:
    default = load_fallback_route(runtime_model_configs={})
    assert default is not None
    assert default.key == "codex"
    assert default.config["transport"] == "cli"
    assert default.config["cli_command"] == "codex"

    override = load_fallback_route(
        runtime_model_configs={
            "codex": {"transport": "cli", "cli_command": "/opt/bin/codex-review"}
        }
    )
    assert override is not None
    assert override.config["cli_command"] == "/opt/bin/codex-review"


def test_requested_keys_still_fail_loudly_when_unconfigured() -> None:
    with pytest.raises(ValueError, match="not configured"):
        build_model_configs(requested_keys=["primary"], runtime_model_configs={})


async def test_real_bridge_offline_http_primary_reviewed_by_cli_codex() -> None:
    """No fake adapter: a refused HTTP primary and a real CLI subprocess codex."""
    script = "import sys; print(sys.argv[-1] and '[]')"
    bridge = AdapterInferenceBridge(
        ModelInferenceBridgeConfig(
            model_configs={
                "primary": {
                    "transport": "http",
                    "base_url": "http://127.0.0.1:1",
                    "model_id": "offline-model",
                },
                "codex": {
                    "transport": "cli",
                    "cli_command": sys.executable,
                    "cli_args": ["-c", script],
                    "fail_on_nonzero_exit": True,
                },
            }
        )
    )

    completed = await _handler(bridge).handle(_command(["primary"]))

    assert completed.final_phase == EnumHostileReviewerPhase.DONE
    assert completed.fallback_engine == "codex"
    assert completed.models_failed == ["primary"]
    assert completed.models_succeeded == ["codex"]


async def test_cli_route_with_fail_on_nonzero_exit_raises() -> None:
    bridge = AdapterInferenceBridge(
        ModelInferenceBridgeConfig(
            model_configs={
                "codex": {
                    "transport": "cli",
                    "cli_command": sys.executable,
                    "cli_args": ["-c", "import sys; print('partial'); sys.exit(3)"],
                    "fail_on_nonzero_exit": True,
                }
            }
        )
    )

    with pytest.raises(RuntimeError, match="exited 3"):
        await bridge.infer("codex", "system", "user", timeout_seconds=30.0)
