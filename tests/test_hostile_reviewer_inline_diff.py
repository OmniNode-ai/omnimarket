# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inline-diff review target and declared-lab-route resolution for the
hostile reviewer orchestrator.

A start command may carry the review target inline (``diff_content``), so a
review can run with no GitHub read and no file on the runtime host. Logical
route keys that the runtime has no route-config JSON for resolve against the
lab backends the repo already declares in the bifrost delegation contract.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_github_diff_effect.models.model_github_diff import (
    ModelGithubDiffCommand,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator import model_config_loader
from omnimarket.nodes.node_hostile_reviewer_orchestrator.handlers.handler_hostile_reviewer_orchestrator import (
    HandlerHostileReviewerOrchestrator,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.model_config_loader import (
    build_model_configs,
)
from omnimarket.nodes.node_hostile_reviewer_orchestrator.models.model_hostile_reviewer_start_command import (
    ModelHostileReviewerStartCommand,
)
from tests.integration._review_verify_mocks import (
    _MockInferenceAdapter,
    finding_payload,
    findings_json,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

_START_TOPIC = "onex.cmd.omnimarket.hostile-reviewer-start.v1"
_COMPLETED_TOPIC = "onex.evt.omnimarket.hostile-reviewer-completed.v1"
_ROUTE_CONFIG_ENV = "HOSTILE_REVIEWER_MODEL_CONFIGS_JSON"

_SYNTHETIC_DIFF = (
    "diff --git a/demo/fictional.py b/demo/fictional.py\n"
    "+def pay(amount):\n"
    "+    return eval(amount)\n"
)


class _ForbiddenDiffEffect:
    """A diff effect that fails the test if the orchestrator reaches for it."""

    async def handle(self, request: ModelGithubDiffCommand) -> Any:
        raise AssertionError(
            f"inline diff must not trigger a GitHub/file read: {request!r}"
        )


def _command(**overrides: Any) -> ModelHostileReviewerStartCommand:
    fields: dict[str, Any] = {
        "correlation_id": uuid4(),
        "models": ["local-coder"],
        "diff_content": _SYNTHETIC_DIFF,
        "requested_at": datetime.now(tz=UTC),
    }
    fields.update(overrides)
    return ModelHostileReviewerStartCommand(**fields)


def _local_coder_backend(**overrides: Any) -> ModelDelegationBackendConfig:
    fields: dict[str, Any] = {
        "backend_id": "local-coder",
        "provider": "local",
        "endpoint_url": "http://lab-host.invalid:8000/v1/chat/completions",
        "model_name": "served-model-x",
        "tier": "local",
    }
    fields.update(overrides)
    return ModelDelegationBackendConfig.model_validate(fields)


@pytest.mark.unit
class TestInlineDiffCommand:
    def test_start_command_accepts_inline_diff(self) -> None:
        command = _command()
        assert command.diff_content == _SYNTHETIC_DIFF
        assert command.pr_number is None
        assert command.file_path is None

    def test_start_command_still_forbids_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            _command(not_a_field="x")

    def test_inline_diff_round_trips_through_wire_json(self) -> None:
        command = _command()
        again = ModelHostileReviewerStartCommand.model_validate_json(
            command.model_dump_json()
        )
        assert again == command


@pytest.mark.unit
@pytest.mark.asyncio
class TestInlineDiffHandler:
    async def test_inline_diff_reviews_without_any_diff_read(self) -> None:
        adapter = _MockInferenceAdapter(
            review_raw=findings_json(
                [
                    finding_payload(
                        description="eval on caller input", severity="critical"
                    )
                ]
            )
        )
        handler = HandlerHostileReviewerOrchestrator(
            inference_adapter=adapter,
            github_diff_effect=_ForbiddenDiffEffect(),
        )
        command = _command()

        event = await handler.handle(command)

        assert event.final_phase.value == "done", event.error_message
        assert event.correlation_id == command.correlation_id
        assert event.models_succeeded == ["local-coder"]
        assert event.total_findings == 1
        assert adapter.calls == ["local-coder"]

    async def test_inline_diff_reaches_the_prompt(self) -> None:
        seen: list[str] = []

        class _Recorder:
            async def infer(
                self,
                model_key: str,
                system_prompt: str,
                user_prompt: str,
                timeout_seconds: float,
            ) -> str:
                seen.append(user_prompt)
                return "[]"

        handler = HandlerHostileReviewerOrchestrator(
            inference_adapter=_Recorder(),
            github_diff_effect=_ForbiddenDiffEffect(),
        )
        event = await handler.handle(_command())

        assert event.final_phase.value == "done", event.error_message
        assert len(seen) == 1
        assert "return eval(amount)" in seen[0]

    @pytest.mark.parametrize(
        "extra",
        [
            {"pr_number": 7, "repo": "owner/name"},
            {"file_path": "src/example.py"},
        ],
        ids=["with-pr", "with-file"],
    )
    async def test_inline_diff_plus_another_target_is_a_typed_failure(
        self, extra: dict[str, Any]
    ) -> None:
        handler = HandlerHostileReviewerOrchestrator(
            inference_adapter=_MockInferenceAdapter(),
            github_diff_effect=_ForbiddenDiffEffect(),
        )
        command = _command(**extra)

        event = await handler.handle(command)

        assert event.final_phase.value == "failed"
        assert event.correlation_id == command.correlation_id
        assert event.error_message is not None
        assert "exactly one review target" in event.error_message

    async def test_blank_inline_diff_is_a_typed_failure(self) -> None:
        handler = HandlerHostileReviewerOrchestrator(
            inference_adapter=_MockInferenceAdapter(),
            github_diff_effect=_ForbiddenDiffEffect(),
        )
        event = await handler.handle(_command(diff_content="   \n"))

        assert event.final_phase.value == "failed"
        assert event.error_message is not None
        assert "empty" in event.error_message


@pytest.mark.unit
class TestDeclaredLabRouteFallback:
    """No route-config JSON on the runtime: a key the repo declares as a lab
    backend resolves from the bifrost contract; nothing else is invented."""

    def test_declared_local_backend_resolves_without_route_config_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(_ROUTE_CONFIG_ENV, raising=False)
        monkeypatch.setattr(
            model_config_loader,
            "resolve_bifrost_backend",
            lambda backend_id: (
                _local_coder_backend() if backend_id == "local-coder" else None
            ),
        )

        configs = build_model_configs(requested_keys=["local-coder"])

        assert configs["local-coder"]["transport"] == "http"
        assert configs["local-coder"]["model_id"] == "served-model-x"
        # The adapter appends /chat/completions itself; the declared complete
        # URL must not double the suffix.
        assert configs["local-coder"]["base_url"] == "http://lab-host.invalid:8000/v1"

    def test_route_config_env_wins_over_declared_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            _ROUTE_CONFIG_ENV,
            json.dumps(
                {
                    "local-coder": {
                        "transport": "http",
                        "base_url": "http://env-supplied.invalid:9/v1",
                        "model_id": "env-model",
                    }
                }
            ),
        )
        monkeypatch.setattr(
            model_config_loader,
            "resolve_bifrost_backend",
            lambda _backend_id: _local_coder_backend(),
        )

        configs = build_model_configs(requested_keys=["local-coder"])

        assert configs["local-coder"]["model_id"] == "env-model"

    @pytest.mark.parametrize(
        "backend",
        [
            None,
            _local_coder_backend(endpoint_url=None),
            _local_coder_backend(model_name=None),
            _local_coder_backend(provider="gemini", tier="frontier_api"),
            _local_coder_backend(
                endpoint_url="http://lab-host.invalid:8002/v1/embeddings"
            ),
        ],
        ids=[
            "undeclared",
            "parked-endpoint",
            "no-model-name",
            "non-lab-provider",
            "not-a-chat-endpoint",
        ],
    )
    def test_unresolvable_key_still_fails_loudly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        backend: ModelDelegationBackendConfig | None,
    ) -> None:
        monkeypatch.delenv(_ROUTE_CONFIG_ENV, raising=False)
        monkeypatch.setattr(
            model_config_loader, "resolve_bifrost_backend", lambda _backend_id: backend
        )

        with pytest.raises(ValueError, match=_ROUTE_CONFIG_ENV):
            build_model_configs(requested_keys=["local-coder"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_route_still_publishes_exactly_one_completed_event(
    integration_event_bus: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed-route case through the entry shape the runtime uses.

    The runtime hands the decoded start command to the handler and publishes
    whatever it returns on the terminal topic. An unroutable model key must end
    as one typed FAILED completed event for the correlation id, never silence.
    """
    monkeypatch.delenv(_ROUTE_CONFIG_ENV, raising=False)
    monkeypatch.setattr(
        model_config_loader, "resolve_bifrost_backend", lambda _backend_id: None
    )

    class _Wrapper:
        def __init__(self, handler: HandlerHostileReviewerOrchestrator) -> None:
            self._handler = handler

        async def handle(self, **payload: Any) -> Any:
            return await self._handler.handle(
                ModelHostileReviewerStartCommand(**payload)
            )

    await integration_event_bus.start()
    try:
        adapter = LocalRuntimeBusAdapter(
            handler=_Wrapper(
                HandlerHostileReviewerOrchestrator(
                    github_diff_effect=_ForbiddenDiffEffect()
                )
            ),
            handler_name="hostile-reviewer-orchestrator",
            input_model_cls=ModelHostileReviewerStartCommand,
            output_topic=_COMPLETED_TOPIC,
            bus=integration_event_bus,
        )
        await integration_event_bus.subscribe(
            _START_TOPIC,
            on_message=adapter.on_message,
            group_id="hostile-reviewer-inline-diff-failed-route",
        )
        command = _command(models=["no-such-route"])
        await integration_event_bus.publish(
            _START_TOPIC, key=None, value=command.model_dump_json().encode("utf-8")
        )

        history = await integration_event_bus.get_event_history(topic=_COMPLETED_TOPIC)
        assert len(history) == 1, "expected exactly one terminal completed event"
        payload = json.loads(history[0].value)
        assert payload["correlation_id"] == str(command.correlation_id)
        assert payload["final_phase"] == "failed"
        assert _ROUTE_CONFIG_ENV in payload["error_message"]
    finally:
        await integration_event_bus.close()
