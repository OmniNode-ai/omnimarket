# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 OmniNode Team
"""Invocation-only ADK adapter (OMN-13611, WS-C Phase 2.1).

AUTHORITY BOUNDARY (operator mandate, OMN-13611)
------------------------------------------------
This adapter owns **invocation only**. It MUST NOT:

* select providers, models, tiers, or escalation paths,
* hold routing logic, or
* override delegation decisions.

Routing authority remains with the delegation reducer, the routing reducer, and
the model-selection authority. This adapter consumes an *already-routed*
``ModelInvocationCommand`` (produced by ``node_delegation_routing_reducer`` and
dispatched by ``node_delegation_orchestrator``) and binds it to the canonical
remote-agent invoke transport.

Canonical invoke surface (resolved, not assumed — OMN-13611)
------------------------------------------------------------
There is no ``node_remote_agent_invoke_effect`` directory in omnimarket. The
canonical invoke surface is:

1. ``node_delegation_orchestrator`` (omnimarket) publishes the typed
   ``InvocationCommand`` event to the topic
   ``onex.cmd.omnibase-infra.remote-agent-invoke.v1`` (declared in its
   ``contract.yaml`` ``published_events`` and ``externally_consumed_topics``).
2. ``node_remote_agent_invoke_effect`` (omnibase_infra) consumes that topic and
   performs the protocol-specific submit/watch via ``HandlerA2ATask``.

This adapter therefore resolves the invoke topic *from the delegation
orchestrator contract* (never hardcoded) and produces a dispatch envelope onto
that canonical surface.

ADK O4 credential/model-injection probe (recorded for provenance)
-----------------------------------------------------------------
google-adk 1.33.0 SUPPORTS non-env credential and model injection on the
``Runner``/run-agent path: ``Runner.__init__`` accepts a constructor
``credential_service: BaseCredentialService`` and ``LlmAgent.model`` accepts a
constructor-injected ``str | BaseLlm`` (``Gemini`` exposes ``base_url``). The
adapter therefore declares credentials by ``secret_ref`` resolved at the effect
boundary and never reads ``GOOGLE_*`` / ``ADK_*`` environment variables.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import yaml
from omnibase_core.enums.enum_agent_protocol import EnumAgentProtocol
from omnibase_core.enums.enum_invocation_kind import EnumInvocationKind
from omnibase_core.models.delegation.model_invocation_command import (
    ModelInvocationCommand,
)
from pydantic import ValidationError

from omnimarket.adapters.adk.models import (
    ModelAdkInvocationDispatch,
    ModelAdkInvokeConfig,
    ModelAdkRunnerBinding,
)
from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    deep_merge_bifrost_delegation_config,
)

# Adapter config lives in the installed package, not the working directory.
_CONFIG_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "configs" / "adk_invoke.yaml"
)
_OVERLAY_PATH: Final[Path] = (
    Path.home() / ".omninode" / "delegation" / "adk_invoke_overrides.yaml"
)

# The delegation orchestrator contract is the authority for the invoke topic.
_DELEGATION_ORCHESTRATOR_CONTRACT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2]
    / "nodes"
    / "node_delegation_orchestrator"
    / "contract.yaml"
)

# The event_type the orchestrator publishes onto the canonical invoke topic.
_INVOCATION_COMMAND_EVENT_TYPE: Final[str] = "InvocationCommand"


def resolve_adk_invoke_topic(contract_path: Path | None = None) -> str:
    """Resolve the canonical remote-agent invoke topic.

    Reads the topic from the ``node_delegation_orchestrator`` contract's
    ``published_events`` entry whose ``event_type`` is ``InvocationCommand``.
    Fails loudly (``KeyError``/``ValueError``) if the contract or the entry is
    missing — never falls back to a hardcoded topic string.
    """
    path = contract_path or _DELEGATION_ORCHESTRATOR_CONTRACT_PATH
    contract: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    published = contract["published_events"]
    for entry in published:
        if entry["event_type"] == _INVOCATION_COMMAND_EVENT_TYPE:
            topic = entry["topic"]
            if not isinstance(topic, str) or not topic:
                msg = (
                    "InvocationCommand published_events entry has an empty/invalid "
                    f"topic in {path}"
                )
                raise ValueError(msg)
            return topic
    msg = (
        "No published_events entry with event_type "
        f"{_INVOCATION_COMMAND_EVENT_TYPE!r} found in {path}; cannot resolve the "
        "canonical remote-agent invoke topic."
    )
    raise ValueError(msg)


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"Expected YAML mapping at root for {path}, got {type(data).__name__}"
        raise ValueError(msg)
    return data


def load_adk_invoke_config(
    config_path: Path | None = None,
    overlay_path: Path | None = None,
) -> ModelAdkInvokeConfig:
    """Load and validate the ADK invoke transport binding from contract + overlay.

    The repo default ``adk_invoke.yaml`` is deep-merged with the optional
    endpoint overlay (mirroring ``bifrost_delegation.yaml`` semantics) and
    validated into a frozen ``ModelAdkInvokeConfig``. The config records the agent
    protocol and the ADK runner binding only; the canonical invoke topic is not
    recorded here — it is the sole property of the orchestrator contract and is
    resolved by ``resolve_adk_invoke_topic`` (one source of truth, no drift).

    Raises:
        FileNotFoundError: if the config file is absent.
        ValueError: on YAML/schema validation failure.
    """
    resolved_config = config_path or _CONFIG_PATH
    overlay = overlay_path or _OVERLAY_PATH

    if not resolved_config.exists():
        msg = f"ADK invoke config not found at {resolved_config}"
        raise FileNotFoundError(msg)

    data = _read_yaml_mapping(resolved_config)
    if overlay.exists():
        overlay_data = _read_yaml_mapping(overlay)
        data = deep_merge_bifrost_delegation_config(data, overlay_data)

    try:
        return ModelAdkInvokeConfig.model_validate(data)
    except ValidationError as exc:
        msg = f"ADK invoke config schema validation failed: {exc}"
        raise ValueError(msg) from exc


def build_adk_invocation_dispatch(
    command: ModelInvocationCommand,
    *,
    config: ModelAdkInvokeConfig | None = None,
    contract_path: Path | None = None,
) -> ModelAdkInvocationDispatch:
    """Bind an already-routed invocation command to the canonical invoke transport.

    This is the adapter's only transformation. It copies the routing authority's
    resolved fields verbatim and attaches the resolved transport binding. It does
    NOT select a provider, model, tier, or escalation path, and it does NOT read
    or re-derive any routing decision.

    The adapter only handles AGENT-kind commands. A MODEL-kind command is a
    routing classification this adapter does not own; it is rejected loudly
    rather than re-routed.

    Raises:
        ValueError: if the command is not AGENT-kind, carries no agent protocol,
            or its agent protocol does not match the bound protocol.
    """
    if command.invocation_kind is not EnumInvocationKind.AGENT:
        msg = (
            "adapter_adk_invoke handles AGENT-kind invocation only; refusing to "
            f"act on invocation_kind={command.invocation_kind.name}. Routing "
            "classification is owned by the routing authority, not this adapter."
        )
        raise ValueError(msg)

    if command.agent_protocol is None:
        msg = (
            "AGENT-kind invocation command is missing agent_protocol; the routing "
            "authority must resolve it before invocation."
        )
        raise ValueError(msg)

    resolved_config = config if config is not None else load_adk_invoke_config()

    if command.agent_protocol is not resolved_config.agent_protocol:
        msg = (
            f"command agent_protocol={command.agent_protocol.name} does not match "
            f"the adapter-bound protocol={resolved_config.agent_protocol.name}; "
            "this adapter does not re-route across protocols."
        )
        raise ValueError(msg)

    binding: ModelAdkRunnerBinding = resolved_config.adk_runner
    invoke_topic = resolve_adk_invoke_topic(contract_path)
    return ModelAdkInvocationDispatch(
        invoke_topic=invoke_topic,
        task_id=command.task_id,
        correlation_id=command.correlation_id,
        agent_protocol=command.agent_protocol,
        target_ref=command.target_ref,
        credential_secret_ref=binding.credential_secret_ref,
        endpoint_url=binding.endpoint_url,
        payload=dict(command.payload),
    )


__all__ = [
    "EnumAgentProtocol",
    "build_adk_invocation_dispatch",
    "load_adk_invoke_config",
    "resolve_adk_invoke_topic",
]
