"""Legacy traces captured before deleting the workflow (OMN-20679)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
NODE = REPO / "src/omnimarket/nodes/node_architecture_discovery_orchestrator"
OVERLAY = {
    "substrate_probe_url": "OVERLAY_SUBSTRATE_PROBE_URL",
    "registry_dir": "OVERLAY_REGISTRY_DIR",
}
FIXTURES = REPO / "tests/fixtures/architecture_discovery"


def test_discovery_is_a_contract_node_with_its_topics_declared() -> None:
    import yaml

    assert (NODE / "contract.yaml").is_file()
    assert (NODE / "node.py").is_file()
    assert (NODE / "metadata.yaml").is_file()
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    command = "onex.cmd.omnimarket.architecture-discovery-requested.v1"
    completed = "onex.evt.omnimarket.architecture-discovery-completed.v1"
    invoke = "onex.cmd.omnibase-infra.coding-agent-effect-invoke.v1"
    validate = "onex.cmd.omnibase-infra.coding-agent-workspace-validate.v1"
    assert contract["runtime_dispatch"]["command_topic"] == command
    assert contract["terminal_event"] == completed
    assert contract["event_bus"]["subscribe_topics"] == [command]
    assert contract["event_bus"]["publish_topics"] == [completed, invoke, validate]
    assert {completed, invoke, validate} <= set(contract["externally_consumed_topics"])
    handler_source = (NODE / "handlers/handler_architecture_discovery.py").read_text()
    assert "onex.cmd." not in handler_source
    assert "onex.evt." not in handler_source


@pytest.mark.parametrize("name", ["default", "configured"])
def test_discovery_matches_captured_legacy_trace(name: str) -> None:
    """Every rendered phase equals the old tool's, byte for byte, except the overlay values.

    ``prompt_sha256`` is the old tool's prompt with its two operator-private literals replaced by
    the sentinels in ``OVERLAY``; rendering here with those sentinels must reproduce it exactly.
    ``legacy_prompt_sha256`` is the hash of the old tool's prompt itself; it is checked where the
    real overlay lives, since this public tree must not carry the private values.
    """
    from omnimarket.nodes.node_architecture_discovery_orchestrator.handlers import (
        HandlerArchitectureDiscovery,
    )
    from omnimarket.nodes.node_architecture_discovery_orchestrator.models import (
        ModelDiscoveryPhaseResult,
        ModelDiscoveryRequest,
    )

    golden = json.loads((FIXTURES / f"{name}.json").read_text())
    observed: dict[str, dict[str, object]] = {}

    def execute(task: object) -> ModelDiscoveryPhaseResult:
        from omnimarket.nodes.node_architecture_discovery_orchestrator.models import (
            ModelDiscoveryTask,
        )

        assert isinstance(task, ModelDiscoveryTask)
        observed[task.label] = {
            "label": task.label,
            "phase": task.phase,
            "model": task.model,
            "effort": task.effort,
            "schema": task.schema_definition,
            "prompt": task.prompt,
        }
        # Actual fixed phase outputs used by the legacy oracle.
        result = golden["result"]
        phase = (
            result["scan"][
                task.label.removeprefix("discovery-").replace("marketplace", "market")
            ]
            if task.phase == "Scan"
            else result["adjudication"]
            if task.phase == "Adjudicate"
            else {
                "report_path": result["report"],
                "state_path": result["state"],
                "commit_sha": result["commit"],
                "slate_summary": result["slate"],
                "ledger_rows": [],
                "delegation": result["delegation"],
            }
        )
        return ModelDiscoveryPhaseResult.model_validate(phase)

    request = ModelDiscoveryRequest.model_validate(
        {**golden["args"], "workspace_path": "/fixture/worktree", "overlay": OVERLAY}
    )
    result = HandlerArchitectureDiscovery(execute=execute).handle(request)
    assert result.legacy_payload() == golden["result"]
    for call in golden["calls"]:
        actual = observed[call["label"]]
        prompt = str(actual.pop("prompt")).replace(
            "src/omnimarket/nodes/node_architecture_discovery_orchestrator/handlers/handler_architecture_discovery.py",
            "src/omnibase_internal/handlers/architecture_work_discovery/architecture-work-discovery.js",
        )
        assert hashlib.sha256(prompt.encode()).hexdigest() == call["prompt_sha256"]
        assert actual == {
            k: v
            for k, v in call.items()
            if k not in {"prompt_sha256", "raw_prompt_sha256", "legacy_prompt_sha256"}
        }


def _fixture_phase(label: str) -> dict[str, object]:
    from typing import Any

    golden: Any = json.loads((FIXTURES / "default.json").read_text())["result"]
    if label in {
        "discovery-linear",
        "discovery-marketplace",
        "discovery-plans",
        "discovery-process",
    }:
        return dict(
            golden["scan"][
                label.removeprefix("discovery-").replace("marketplace", "market")
            ]
        )
    if label == "discovery-adjudicate":
        return dict(golden["adjudication"])
    return {
        "report_path": golden["report"],
        "state_path": golden["state"],
        "commit_sha": golden["commit"],
        "slate_summary": golden["slate"],
        "delegation": golden["delegation"],
        "ledger_rows": [],
    }


def test_discovery_lab_inprocess_bus_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real shared bus adapters drive discovery and all six existing effect invocations.

    In-process reason: this branch is not deployed, and replaying fixed tool stdout avoids
    an organization-wide scan and a new KB publication during pre-PR verification.
    """
    import asyncio
    import re
    from concurrent.futures import ThreadPoolExecutor
    from typing import Any, cast
    from uuid import uuid4

    from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_core.protocols.runtime.protocol_local_runtime_bus import (
        ProtocolLocalRuntimeBus,
    )
    from omnibase_core.protocols.runtime.protocol_local_runtime_callable_target import (
        ProtocolLocalRuntimeCallableTarget,
    )
    from omnibase_core.protocols.runtime.protocol_local_runtime_message import (
        ProtocolLocalRuntimeMessage,
    )
    from omnibase_core.runtime.runtime_local_adapter import LocalRuntimeBusAdapter
    from omnibase_infra.models.coding_agent import (
        ModelCodingAgentInvokeCommand,
        ModelSubprocessOutcome,
        ModelWorkspaceValidateCommand,
    )
    from omnibase_infra.nodes.node_coding_agent_invoke_effect.handlers import (
        handler_coding_agent_invoke,
    )
    from omnibase_infra.nodes.node_coding_agent_workspace_compute.handlers import (
        HandlerWorkspaceValidate,
    )
    from pydantic import BaseModel

    from omnimarket.nodes.node_architecture_discovery_orchestrator.adapters import (
        handler_coding_agent_bus,
    )
    from omnimarket.nodes.node_architecture_discovery_orchestrator.handlers import (
        HandlerArchitectureDiscovery,
        load_contract,
    )
    from omnimarket.nodes.node_architecture_discovery_orchestrator.models import (
        ModelDiscoveryRequest,
    )

    contract = load_contract()
    observed: list[str] = []
    validations: list[str] = []

    async def roundtrip(
        handler: object,
        model: type[BaseModel],
        payload: dict[str, object],
        command_topic: str,
        terminal_topic: str,
    ) -> dict[str, object]:
        bus = EventBusInmemory()
        runtime_bus = cast(ProtocolLocalRuntimeBus, bus)
        replies: list[dict[str, object]] = []
        errors: list[bool] = []
        adapter = LocalRuntimeBusAdapter(
            handler=cast(ProtocolLocalRuntimeCallableTarget, handler),
            handler_name=type(handler).__name__,
            input_model_cls=model,
            output_topic=terminal_topic,
            bus=cast(ProtocolLocalRuntimeBus, bus),
            on_error=lambda: errors.append(True),
        )

        async def reply(message: ProtocolLocalRuntimeMessage) -> None:
            assert isinstance(message.value, bytes)
            replies.append(json.loads(message.value))

        await runtime_bus.start()
        try:
            await runtime_bus.subscribe(
                command_topic, on_message=adapter.on_message, group_id="lab-request"
            )
            await runtime_bus.subscribe(
                terminal_topic, on_message=reply, group_id="lab-terminal"
            )
            await runtime_bus.publish(command_topic, None, json.dumps(payload).encode())
            assert not errors
            assert len(replies) == 1
            return replies[0]
        finally:
            await runtime_bus.close()

    def publish(**kwargs: Any) -> dict[str, object]:
        if kwargs["node_id"] == "node_coding_agent_workspace_compute":
            payload = dict(kwargs["payload"])
            payload["correlation_id"] = str(uuid4())
            validations.append(str(payload["workspace_path"]))
            with ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(
                    lambda: asyncio.run(
                        roundtrip(
                            HandlerWorkspaceValidate(),
                            ModelWorkspaceValidateCommand,
                            payload,
                            kwargs["command_topic"],
                            kwargs["response_topic"],
                        )
                    )
                ).result()
        assert kwargs["node_id"] == contract.invocation.node
        assert kwargs["command_topic"] == contract.invocation.command_topic
        assert kwargs["response_topic"] == contract.invocation.terminal_topic
        assert kwargs["inject_payload_correlation_id"] is True
        payload = dict(kwargs["payload"])
        payload["correlation_id"] = str(uuid4())
        command = ModelCodingAgentInvokeCommand.model_validate(payload)
        label_match = re.search(r"LANE: arch-discovery-(\w+)", command.prompt)
        assert label_match is not None
        label = "discovery-" + label_match[1]
        observed.append(label)
        phase = _fixture_phase(label)
        stdout = json.dumps(
            {"type": "result", "is_error": False, "result": json.dumps(phase)}
        )
        effect = handler_coding_agent_invoke.HandlerCodingAgentInvoke(
            run_subprocess=lambda _: ModelSubprocessOutcome(
                returncode=0, stdout=stdout, stderr="", timed_out=False
            ),
            probe_head_sha=lambda _: "a" * 40,
            capture_diff=lambda _: ((), ""),
            which=lambda _: "/fixture/agent",
            agent_credential_home=str(tmp_path),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                lambda: asyncio.run(
                    roundtrip(
                        effect,
                        ModelCodingAgentInvokeCommand,
                        payload,
                        kwargs["command_topic"],
                        kwargs["response_topic"],
                    )
                )
            ).result()

    monkeypatch.setattr(handler_coding_agent_bus, "publish_and_poll", publish)
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "fixture:9092")
    monkeypatch.setenv("ONEX_CODING_AGENT_WORKSPACE_ROOT", str(tmp_path))
    request = ModelDiscoveryRequest(
        date="2026-10-06", workspace_path=str(tmp_path), overlay=OVERLAY
    )
    result = asyncio.run(
        roundtrip(
            HandlerArchitectureDiscovery(),
            ModelDiscoveryRequest,
            request.model_dump(mode="json", by_alias=True),
            contract.runtime_dispatch["command_topic"],
            contract.terminal_event,
        )
    )
    assert result["report"] == "beta/tracking/2026-10-06-architecture-work-discovery.md"
    assert set(observed[:4]) == {
        "discovery-linear",
        "discovery-marketplace",
        "discovery-plans",
        "discovery-process",
    }
    assert observed[4:] == ["discovery-adjudicate", "discovery-report"]
    assert validations == [str(tmp_path)] * 6


def test_discovery_bus_refuses_missing_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnimarket.nodes.node_architecture_discovery_orchestrator.handlers import (
        HandlerArchitectureDiscovery,
    )
    from omnimarket.nodes.node_architecture_discovery_orchestrator.models import (
        ModelDiscoveryRequest,
    )

    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    with pytest.raises(ValueError, match="KAFKA_BOOTSTRAP_SERVERS is required"):
        HandlerArchitectureDiscovery().handle(
            ModelDiscoveryRequest(
                date="2026-10-06", workspace_path="/fixture/worktree", overlay=OVERLAY
            )
        )


def test_workspace_rejection_stops_agent_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any
    from uuid import uuid4

    from omnimarket.nodes.node_architecture_discovery_orchestrator.adapters import (
        handler_coding_agent_bus,
    )
    from omnimarket.nodes.node_architecture_discovery_orchestrator.models import (
        ModelDiscoveryTask,
    )

    def publish(**kwargs: Any) -> dict[str, object]:
        assert kwargs["node_id"] == "node_coding_agent_workspace_compute"
        return {
            "payload": {
                "correlation_id": str(uuid4()),
                "valid": False,
                "resolved_path": "/refused",
                "rejection_reason": "outside allowed roots",
            }
        }

    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "fixture:9092")
    monkeypatch.setenv("ONEX_CODING_AGENT_WORKSPACE_ROOT", "/allowed")
    task = ModelDiscoveryTask(
        label="discovery-linear",
        phase="Scan",
        model="sonnet",
        effort="high",
        prompt="fixture",
        schema_definition={},
        workspace_path="/refused",
        correlation_id=uuid4(),
        timeout_ms=1000,
    )
    with pytest.raises(ValueError, match="workspace rejected"):
        handler_coding_agent_bus.HandlerCodingAgentBus(publish=publish).execute(task)


def test_templates_hold_no_private_literals_and_every_placeholder_is_supplied() -> None:
    import re

    from omnimarket.nodes.node_architecture_discovery_orchestrator.handlers import (
        handler_architecture_discovery as handler,
    )
    from omnimarket.nodes.node_architecture_discovery_orchestrator.models import (
        ModelDiscoveryRequest,
    )

    private = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|omni_home")
    templates = sorted((NODE / "handlers/prompts").glob("*.txt"))
    assert len(templates) == 12
    assert [
        t.name for t in templates if private.search(t.read_text(encoding="utf-8"))
    ] == []
    # Positive control: the scan above does bite on a literal of each class.
    assert all(private.search(s) for s in ("probe http://10.1.2.3:8000", "omni_home"))
    request = ModelDiscoveryRequest(
        date="2026-10-06", workspace_path="/w", overlay=OVERLAY
    )
    values = handler.prompt_values(request)
    placeholders = {
        m[1]
        for t in templates
        for m in re.finditer(r"\{\{([a-z_]+)\}\}", t.read_text(encoding="utf-8"))
    }
    assert placeholders - set(values) == {
        "linear",
        "marketplace",
        "plans",
        "process",
        "adjudication",
    }
    filled = {**dict.fromkeys(placeholders, "x"), **values}
    rendered = "\n".join(handler._render(t.name, filled) for t in templates)
    assert "OVERLAY_SUBSTRATE_PROBE_URL" in rendered
    assert "OVERLAY_REGISTRY_DIR" in rendered


def test_request_refuses_a_missing_or_partial_overlay() -> None:
    from pydantic import ValidationError

    from omnimarket.nodes.node_architecture_discovery_orchestrator.models import (
        ModelDiscoveryRequest,
    )

    with pytest.raises(ValidationError):
        ModelDiscoveryRequest.model_validate(
            {"date": "2026-10-06", "workspace_path": "/w"}
        )
    with pytest.raises(ValidationError):
        ModelDiscoveryRequest.model_validate(
            {
                "date": "2026-10-06",
                "workspace_path": "/w",
                "overlay": {"substrate_probe_url": "OVERLAY_SUBSTRATE_PROBE_URL"},
            }
        )
