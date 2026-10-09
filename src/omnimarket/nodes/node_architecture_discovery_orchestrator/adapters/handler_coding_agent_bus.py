"""Reuse the shared Kafka client and the sanctioned tool-enabled invocation effect."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from omnibase_core.cli.cli_run_node import (
    _resolve_client_security_config,
    publish_and_poll,
)
from omnibase_infra.models.coding_agent import (
    EnumAgentSandbox,
    EnumAgentStatus,
    EnumCodingAgent,
    ModelCodingAgentInvokeCommand,
    ModelCodingAgentResult,
    ModelWorkspaceValidateCommand,
    ModelWorkspaceValidateResult,
)
from omnibase_infra.nodes.node_coding_agent_orchestrator.contract_topics import (
    contract_allowed_workspace_roots,
)

from ..handlers.handler_architecture_discovery import load_contract
from ..models.model_discovery_result import (
    ModelDiscoveryPhaseResult,
    require_delegation,
)
from ..models.model_discovery_task import ModelDiscoveryTask

TARGET_NODE = "node_coding_agent_invoke_effect"


def phase_result(task: ModelDiscoveryTask, output: str) -> ModelDiscoveryPhaseResult:
    """Accept the requested JSON object, including the agent CLI's JSON result frame."""
    value: Any = json.loads(output)
    if isinstance(value, dict) and value.get("type") == "result":
        if value.get("is_error"):
            raise ValueError(f"{task.label}: coding agent reported an error")
        value = json.loads(value["result"])
    if not isinstance(value, dict):
        raise ValueError(f"{task.label}: expected a JSON phase result object")
    require_delegation(task.label, value)
    jsonschema.validate(value, task.schema_definition)
    return ModelDiscoveryPhaseResult.model_validate(value)


class HandlerCodingAgentBus:
    """No model subprocess here: the target effect owns invocation and authentication."""

    def __init__(
        self, publish: Callable[..., dict[str, object] | None] | None = None
    ) -> None:
        self._publish = publish if publish is not None else publish_and_poll

    def execute(self, task: ModelDiscoveryTask) -> ModelDiscoveryPhaseResult:
        bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "").strip()
        if not bootstrap:
            raise ValueError(
                "KAFKA_BOOTSTRAP_SERVERS is required for discovery phase execution"
            )
        target: Any = yaml.safe_load(
            files("omnibase_infra.nodes.node_coding_agent_invoke_effect")
            .joinpath("contract.yaml")
            .read_text(encoding="utf-8")
        )
        command_topic = target["event_bus"]["subscribe_topics"][0]
        terminal_topic = target["terminal_event"]
        settings = load_contract().invocation
        if (settings.node, settings.command_topic, settings.terminal_topic) != (
            TARGET_NODE,
            command_topic,
            terminal_topic,
        ):
            raise ValueError(
                "discovery invocation topics differ from the installed target contract"
            )
        workspace_node = "node_coding_agent_workspace_compute"
        workspace_contract: Any = yaml.safe_load(
            files("omnibase_infra.nodes." + workspace_node)
            .joinpath("contract.yaml")
            .read_text(encoding="utf-8")
        )
        validation_command = ModelWorkspaceValidateCommand(
            correlation_id=task.correlation_id,
            workspace_path=task.workspace_path,
            allowed_roots=contract_allowed_workspace_roots(
                Path(
                    str(
                        files(
                            "omnibase_infra.nodes.node_coding_agent_orchestrator"
                        ).joinpath("contract.yaml")
                    )
                )
            ),
            sandbox=EnumAgentSandbox(settings.sandbox),
            prompt=task.prompt,
        )
        if not validation_command.allowed_roots:
            raise ValueError(
                "coding-agent contract declares no resolved allowed workspace roots"
            )
        workspace_topic = workspace_contract["event_bus"]["subscribe_topics"][0]
        if workspace_topic not in load_contract().event_bus["publish_topics"]:
            raise ValueError(
                "workspace validation command is absent from discovery contract"
            )
        check_payload = validation_command.model_dump(mode="json")
        check_payload.pop("correlation_id")
        validation_response = self._publish(
            node_id=workspace_node,
            payload=check_payload,
            timeout=settings.delivery_margin_seconds,
            bootstrap_servers=bootstrap,
            command_topic=workspace_topic,
            response_topic=workspace_contract["event_bus"]["publish_topics"][0],
            inject_payload_correlation_id=True,
            client_security=_resolve_client_security_config(workspace_node),
        )
        if validation_response is None:
            raise TimeoutError(f"{task.label}: no workspace validation terminal")
        validation = ModelWorkspaceValidateResult.model_validate(
            validation_response.get("payload", validation_response)
        )
        if not validation.valid:
            raise ValueError(
                f"{task.label}: workspace rejected: {validation.rejection_reason}"
            )
        command = ModelCodingAgentInvokeCommand(
            correlation_id=task.correlation_id,
            agent=EnumCodingAgent(settings.agent),
            sandbox=EnumAgentSandbox(settings.sandbox),
            workspace_path=validation.resolved_path,
            allow_dirty_tree=True,
            network=True,
            model=task.model,
            timeout_ms=task.timeout_ms,
            prompt=task.prompt
            + "\n\nReturn ONLY the JSON object matching this schema:\n"
            + json.dumps(task.schema_definition, ensure_ascii=False)
            + "\n"
            "Execution authority: work in ticket worktrees, never a canonical clone. "
            "Create a KB worktree from the declared KB clone before writing or "
            "committing the report. "
            "Read PR state only from the PR watcher. After opening the report PR, hand it to "
            "landing-controller; never merge, arm, requeue or rerun CI. These instructions "
            "override "
            "any older operational recipe quoted above. Preserve the report format "
            "and classifications.",
        )
        payload = command.model_dump(mode="json")
        # The shared transport owns the wire correlation, including its matching payload id.
        payload.pop("correlation_id")
        response = self._publish(
            node_id=TARGET_NODE,
            payload=payload,
            timeout=task.timeout_ms // 1000 + settings.delivery_margin_seconds,
            bootstrap_servers=bootstrap,
            command_topic=command_topic,
            response_topic=terminal_topic,
            inject_payload_correlation_id=True,
            client_security=_resolve_client_security_config(TARGET_NODE),
        )
        if response is None:
            raise TimeoutError(
                f"{task.label}: no coding-agent terminal before the deadline"
            )
        result = ModelCodingAgentResult.model_validate(
            response.get("payload", response)
        )
        if result.status is not EnumAgentStatus.COMPLETED or result.exit_code != 0:
            raise RuntimeError(
                f"{task.label}: coding-agent invocation {result.status.value}: "
                f"{result.error_class.value}"
            )
        return phase_result(task, result.output)
