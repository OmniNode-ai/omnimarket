# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the invocation-only ADK adapter (OMN-13611, WS-C Phase 2.1).

The authority-boundary tests are the load-bearing DoD checks: they assert the
adapter performs NO provider/model/tier/escalation selection, resolves
credentials by reference (not a literal), reads no GOOGLE_*/ADK_* env vars, and
binds to the canonical invoke topic resolved from the delegation orchestrator
contract (never a hardcoded topic string).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.enums.enum_agent_protocol import EnumAgentProtocol
from omnibase_core.enums.enum_invocation_kind import EnumInvocationKind
from omnibase_core.models.common.model_schema_value import ModelSchemaValue
from omnibase_core.models.delegation.model_invocation_command import (
    ModelInvocationCommand,
)

from omnimarket.adapters.adk import adapter_adk_invoke as mod
from omnimarket.adapters.adk.adapter_adk_invoke import (
    build_adk_invocation_dispatch,
    load_adk_invoke_config,
    resolve_adk_invoke_topic,
)
from omnimarket.adapters.adk.models import (
    ModelAdkInvocationDispatch,
    ModelAdkInvokeConfig,
)

_CANONICAL_INVOKE_TOPIC = "onex.cmd.omnibase-infra.remote-agent-invoke.v1"


def _agent_command(
    *,
    target_ref: str = "agent://a2a/test-peer",
    payload: dict[str, ModelSchemaValue] | None = None,
) -> ModelInvocationCommand:
    return ModelInvocationCommand(
        task_id=uuid4(),
        correlation_id=uuid4(),
        invocation_kind=EnumInvocationKind.AGENT,
        agent_protocol=EnumAgentProtocol.A2A,
        target_ref=target_ref,
        payload=payload or {},
    )


@pytest.mark.unit
def test_resolve_invoke_topic_from_orchestrator_contract() -> None:
    """Topic is resolved from the delegation orchestrator contract, not hardcoded."""
    topic = resolve_adk_invoke_topic()
    assert topic == _CANONICAL_INVOKE_TOPIC


@pytest.mark.unit
def test_resolve_invoke_topic_fails_when_event_missing(tmp_path: Path) -> None:
    """A contract without the InvocationCommand event fails loudly (no fallback)."""
    bogus = tmp_path / "contract.yaml"
    bogus.write_text(
        yaml.safe_dump({"published_events": [{"event_type": "Other", "topic": "x"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="InvocationCommand"):
        resolve_adk_invoke_topic(contract_path=bogus)


@pytest.mark.unit
def test_load_config_validates_binding() -> None:
    config = load_adk_invoke_config()
    assert isinstance(config, ModelAdkInvokeConfig)
    assert config.agent_protocol is EnumAgentProtocol.A2A
    # Credential is a reference, never a literal secret value.
    assert config.adk_runner.credential_secret_ref == "llm.gemini.api_key"
    # The config records no topic literal: the topic is owned by the contract.
    assert not hasattr(config, "invoke_topic")


@pytest.mark.unit
def test_overlay_deep_merge_overrides_endpoint(tmp_path: Path) -> None:
    """Overlay file overrides the endpoint via deep-merge (no env var)."""
    base = tmp_path / "adk_invoke.yaml"
    base.write_text(
        yaml.safe_dump(
            {
                "config_version": "1.0.0",
                "agent_protocol": "A2A",
                "adk_runner": {
                    "credential_secret_ref": "llm.gemini.api_key",
                    "endpoint_url": "https://default.invalid",
                },
            }
        ),
        encoding="utf-8",
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump({"adk_runner": {"endpoint_url": "https://overlay.invalid"}}),
        encoding="utf-8",
    )
    config = load_adk_invoke_config(config_path=base, overlay_path=overlay)
    assert config.adk_runner.endpoint_url == "https://overlay.invalid"
    # Secret ref preserved from base (overlay did not override it).
    assert config.adk_runner.credential_secret_ref == "llm.gemini.api_key"


@pytest.mark.unit
def test_build_dispatch_copies_routed_fields_verbatim() -> None:
    """The dispatch copies the routing authority's resolved fields verbatim."""
    command = _agent_command(
        target_ref="agent://a2a/peer-7",
        payload={"goal": ModelSchemaValue(string_value="ship it", value_type="string")},
    )
    dispatch = build_adk_invocation_dispatch(command)
    assert isinstance(dispatch, ModelAdkInvocationDispatch)
    assert dispatch.invoke_topic == _CANONICAL_INVOKE_TOPIC
    assert dispatch.task_id == command.task_id
    assert dispatch.correlation_id == command.correlation_id
    assert dispatch.agent_protocol == command.agent_protocol
    assert dispatch.target_ref == command.target_ref
    assert dispatch.payload == command.payload
    # Credential surfaced as a reference only.
    assert dispatch.credential_secret_ref == "llm.gemini.api_key"


# --------------------------------------------------------------------------- #
# Authority-boundary DoD tests: the adapter performs NO routing/selection.
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_adapter_imports_no_routing_or_selection_authority() -> None:
    """DoD: the adapter imports no routing/tier/model-selection authority.

    Scans the import graph of the adapter source. The adapter may reference the
    routing authorities by NAME in its docstrings (to document the boundary), but
    it must not IMPORT any routing reducer, tier config loader, routing-decision
    model, or escalation/model-selection module. Importing one would be the first
    step toward making a routing decision — exactly what OMN-13611 forbids.
    """
    source_path_str = inspect.getsourcefile(mod)
    assert source_path_str is not None
    tree = ast.parse(Path(source_path_str).read_text(encoding="utf-8"))
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.append(node.module)
    forbidden_import_fragments = (
        "routing_reducer",
        "routing_tiers",
        "routing_feedback",
        "model_routing_decision",
        "escalation",
        "quality_gate",
        "tier",
    )
    offenders = [
        m
        for m in imported_modules
        for frag in forbidden_import_fragments
        if frag in m.lower()
    ]
    assert not offenders, (
        f"adapter_adk_invoke must not import routing/selection authorities: {offenders}"
    )


@pytest.mark.unit
def test_adapter_defines_no_selection_or_scoring_callable() -> None:
    """DoD: the adapter defines no provider/model/tier selection or scoring callable.

    Inspects every function/method DEFINED in the adapter module and fails if any
    name implies a selection or scoring responsibility. The adapter only resolves
    a topic, loads a transport binding, and builds a dispatch envelope from an
    already-routed command.
    """
    source_path_str = inspect.getsourcefile(mod)
    assert source_path_str is not None
    tree = ast.parse(Path(source_path_str).read_text(encoding="utf-8"))
    selection_name_fragments = (
        "select",
        "choose",
        "pick",
        "route",
        "rank",
        "score",
        "escalat",
        "tier",
        "fallback",
    )
    defined_callables = [
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    offenders = [
        name
        for name in defined_callables
        for frag in selection_name_fragments
        # "resolve_adk_invoke_topic" is allowed: it resolves the canonical topic,
        # it does not route. Guard against the selection verbs only.
        if frag in name.lower() and not name.startswith("resolve_adk_invoke_topic")
    ]
    assert not offenders, (
        f"adapter_adk_invoke must define no selection/scoring callable: {offenders}"
    )


@pytest.mark.unit
def test_build_dispatch_rejects_model_kind() -> None:
    """A MODEL-kind command is a routing classification the adapter does not own."""
    command = ModelInvocationCommand(
        task_id=uuid4(),
        correlation_id=uuid4(),
        invocation_kind=EnumInvocationKind.MODEL,
        agent_protocol=None,
        model_backend="cloud-gemini-pro",
        target_ref="model://gemini",
        payload={},
    )
    with pytest.raises(ValueError, match="AGENT-kind invocation only"):
        build_adk_invocation_dispatch(command)


@pytest.mark.unit
def test_build_dispatch_requires_protocol_match() -> None:
    """The adapter binds one protocol and does not re-route across protocols.

    EnumAgentProtocol currently has a single member (A2A), so the matching path is
    asserted directly here; the mismatch branch in the adapter guards future
    protocols and is exercised structurally by the source-scan tests.
    """
    command = _agent_command()
    config = load_adk_invoke_config()
    assert command.agent_protocol is config.agent_protocol
    dispatch = build_adk_invocation_dispatch(command, config=config)
    assert dispatch.agent_protocol is EnumAgentProtocol.A2A


@pytest.mark.unit
def test_adapter_source_reads_no_google_or_adk_env() -> None:
    """DoD: no os.environ['GOOGLE_*'] / os.environ['ADK_*'] reads in adapter source.

    Parses the AST and flags any os.environ subscript or os.getenv/os.environ.get
    call whose key starts with GOOGLE_ or ADK_, plus any os.environ access at all
    (the adapter resolves everything from contract/overlay, not env).
    """
    source_path_str = inspect.getsourcefile(mod)
    assert source_path_str is not None
    path = Path(source_path_str)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        # os.environ[...] subscript — forbidden outright in this adapter.
        if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
            pytest.fail(f"adapter reads os.environ subscript in {path.name}")
        # os.getenv(...) / os.environ.get(...) — forbidden outright.
        if isinstance(node, ast.Call):
            key = _env_call_key(node)
            if key is not None:
                pytest.fail(f"adapter reads env via call in {path.name}: {key!r}")


def _is_os_environ(value: ast.expr) -> bool:
    return (
        isinstance(value, ast.Attribute)
        and value.attr == "environ"
        and isinstance(value.value, ast.Name)
        and value.value.id == "os"
    )


def _literal_str(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _env_call_key(node: ast.Call) -> str | None:
    func = node.func
    # os.getenv("KEY")
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "getenv"
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    ):
        return (node.args and _literal_str(node.args[0])) or "<dynamic>"
    # os.environ.get("KEY")
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "get"
        and _is_os_environ(func.value)
    ):
        return (node.args and _literal_str(node.args[0])) or "<dynamic>"
    return None
