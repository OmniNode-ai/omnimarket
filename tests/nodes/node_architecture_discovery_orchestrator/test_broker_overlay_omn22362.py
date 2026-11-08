"""OMN-22362: the discovery callers dial the broker their node's contract overlay declares.

Operator ruling 2026-11-03: "The default broker should be in the contract overlay." Before this the
architecture-discovery CLI and its coding-agent bus adapter read ``KAFKA_BOOTSTRAP_SERVERS`` for the
broker and imported omnibase_core's private ``_resolve_client_security_config``, which read
``KAFKA_SECURITY_PROTOCOL`` and ``KAFKA_SASL_*`` and which omnibase_core deletes when ``onex
run-node`` takes its broker from the overlay. Each test drives the real caller over real overlay and
lane-declaration files in a scratch workspace config root; none would pass at the merge base.
"""

from __future__ import annotations

import json
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
import yaml

from omnimarket.handlers.bus_binding import node_broker
from omnimarket.handlers.bus_binding.node_broker import (
    NodeBrokerResolutionError,
    resolve_dialable_broker,
    resolve_node_broker,
)
from omnimarket.nodes.node_architecture_discovery_orchestrator import (
    runtime_architecture_discovery_cli as cli,
)
from omnimarket.nodes.node_architecture_discovery_orchestrator.adapters import (
    handler_coding_agent_bus,
)
from omnimarket.nodes.node_architecture_discovery_orchestrator.models import (
    ModelDiscoveryTask,
)
from tests.nodes.node_architecture_discovery_orchestrator.broker_overlay_support import (
    DISCOVERY_NODES,
    LANE_BROKER,
    install_broker_overlays,
)

pytestmark = pytest.mark.unit

AMBIENT_BROKER = "ambient-wrong-broker:1"
SASL_MARKER = "credential-marker-never-printed"
KAFKA_DEV = {"type": "kafka", "lane": "dev"}


@pytest.fixture(autouse=True)
def _allowed_workspace_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_CODING_AGENT_WORKSPACE_ROOT", "/allowed")


def _task(workspace: str = "/allowed/requested") -> ModelDiscoveryTask:
    return ModelDiscoveryTask(
        label="discovery-linear",
        phase="Scan",
        model="sonnet",
        effort="high",
        prompt="Inspect the workspace",
        schema_definition={},
        workspace_path=workspace,
        correlation_id=uuid4(),
        timeout_ms=1000,
    )


def _record_publish(calls: list[dict[str, Any]]) -> Any:
    def publish(**kwargs: Any) -> dict[str, object] | None:
        calls.append(kwargs)
        if kwargs["node_id"] == "node_coding_agent_workspace_compute":
            return {
                "payload": {
                    "correlation_id": str(uuid4()),
                    "valid": False,
                    "resolved_path": "/refused",
                    "rejection_reason": "outside allowed roots",
                    "invoke_command": kwargs["payload"]["invoke_command"],
                }
            }
        return None

    return publish


# ------------------------------------------------- the overlay selects the broker


def test_redpanda_overlay_lane_is_dialed_for_both_nodes_the_adapter_reaches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_broker_overlays(tmp_path, monkeypatch, KAFKA_DEV)
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", AMBIENT_BROKER)
    monkeypatch.setenv("KAFKA_SECURITY_PROTOCOL", "SASL_SSL")
    calls: list[dict[str, Any]] = []
    # The workspace refusal stops the run after the first publish; the second node's broker is
    # resolved before it, so both are read.
    seen: list[str] = []
    real = handler_coding_agent_bus.resolve_dialable_broker

    def resolving(node: str) -> Any:
        seen.append(node)
        return real(node)

    adapter = handler_coding_agent_bus.HandlerCodingAgentBus(
        publish=_record_publish(calls), resolve_broker=resolving
    )
    with pytest.raises(ValueError, match="workspace rejected"):
        adapter.execute(_task())

    assert seen == [
        "node_coding_agent_workspace_compute",
        "node_coding_agent_invoke_effect",
    ]
    assert [call["bootstrap_servers"] for call in calls] == [LANE_BROKER]
    assert calls[0]["client_security"] == {"security.protocol": "PLAINTEXT"}


def test_the_default_adapter_resolves_through_the_overlay_not_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_broker_overlays(tmp_path, monkeypatch, KAFKA_DEV)
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", AMBIENT_BROKER)
    calls: list[dict[str, Any]] = []
    with pytest.raises(ValueError, match="workspace rejected"):
        handler_coding_agent_bus.HandlerCodingAgentBus(
            publish=_record_publish(calls)
        ).execute(_task())
    assert calls[0]["bootstrap_servers"] == LANE_BROKER
    assert AMBIENT_BROKER not in json.dumps(calls, default=str)


def test_cli_dials_the_orchestrators_overlay_lane_and_strips_event_bus_from_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_broker_overlays(tmp_path, monkeypatch, KAFKA_DEV)
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", AMBIENT_BROKER)
    overlay_file = tmp_path / "caller-overlay.yaml"
    overlay_file.write_text(
        yaml.safe_dump(
            {
                "substrate_probe_url": "http://probe.test/v1/models",
                "registry_dir": "example",
                "event_bus": KAFKA_DEV,
            }
        ),
        encoding="utf-8",
    )
    calls: list[dict[str, Any]] = []

    def publish(**kwargs: Any) -> dict[str, object]:
        calls.append(kwargs)
        return {
            "payload": {
                "report": "beta/tracking/report.md",
                "phases": [],
            }
        }

    monkeypatch.setattr(cli, "publish_and_poll", publish)
    with patch.object(cli.ModelDiscoveryResult, "model_validate") as validated:
        validated.return_value.legacy_payload.return_value = {"report": "r"}
        status = cli.main(
            [
                "--date",
                "2026-11-03",
                "--workspace",
                str(tmp_path),
                "--overlay",
                str(overlay_file),
            ]
        )

    assert status == 0
    assert calls[0]["node_id"] == "node_architecture_discovery_orchestrator"
    assert calls[0]["bootstrap_servers"] == LANE_BROKER
    assert calls[0]["client_security"] == {"security.protocol": "PLAINTEXT"}
    assert calls[0]["payload"]["overlay"] == {
        "substrate_probe_url": "http://probe.test/v1/models",
        "registry_dir": "example",
    }


def test_a_sasl_lane_takes_the_identity_of_the_host_without_printing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_broker_overlays(tmp_path, monkeypatch, {"type": "kafka", "lane": "scram"})
    monkeypatch.setenv("KAFKA_SASL_USERNAME", "dev-principal")
    monkeypatch.setenv("KAFKA_SASL_PASSWORD", SASL_MARKER)
    monkeypatch.setenv("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT")

    broker = resolve_dialable_broker("node_coding_agent_invoke_effect")

    assert broker.bootstrap_servers == LANE_BROKER
    assert broker.client_security() == {
        "security.protocol": "SASL_PLAINTEXT",
        "sasl.mechanism": "SCRAM-SHA-256",
        "sasl.username": "dev-principal",
        "sasl.password": SASL_MARKER,
    }
    assert SASL_MARKER not in repr(broker)


# ------------------------------------------------------------- refusals


def test_inmemory_overlay_hosts_nothing_and_dials_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_root = install_broker_overlays(tmp_path, monkeypatch, {"type": "inmemory"})
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", AMBIENT_BROKER)
    overlay = (
        config_root
        / "config/onex/overlays/node_coding_agent_workspace_compute/overlay.yaml"
    )

    broker = resolve_node_broker("node_coding_agent_workspace_compute")
    assert broker.kind == "inmemory"
    assert broker.bootstrap_servers is None
    assert broker.client_security() == {}
    assert broker.overlay_path == overlay

    calls: list[dict[str, Any]] = []
    with pytest.raises(NodeBrokerResolutionError, match="inmemory") as refused:
        handler_coding_agent_bus.HandlerCodingAgentBus(
            publish=_record_publish(calls)
        ).execute(_task())
    assert str(overlay) in str(refused.value)
    assert calls == []


def test_missing_overlay_is_refused_naming_the_path_even_with_the_old_variable_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_root = install_broker_overlays(tmp_path, monkeypatch, KAFKA_DEV, nodes=())
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", AMBIENT_BROKER)
    overlay = (
        config_root
        / "config/onex/overlays/node_coding_agent_invoke_effect/overlay.yaml"
    )
    calls: list[dict[str, Any]] = []

    with pytest.raises(NodeBrokerResolutionError) as refused:
        handler_coding_agent_bus.HandlerCodingAgentBus(
            publish=_record_publish(calls)
        ).execute(_task())

    assert "declares no event_bus" in str(refused.value)
    assert str(overlay.parent.parent / "node_coding_agent_workspace_compute") in str(
        refused.value
    ) or str(overlay) in str(refused.value)
    assert calls == []


def test_overlay_without_event_bus_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_broker_overlays(tmp_path, monkeypatch, None)
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", AMBIENT_BROKER)
    with pytest.raises(NodeBrokerResolutionError, match="declares no event_bus"):
        resolve_dialable_broker(DISCOVERY_NODES[0])


def test_cli_refuses_with_a_usage_error_naming_the_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_broker_overlays(tmp_path, monkeypatch, None)
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", AMBIENT_BROKER)
    monkeypatch.setattr(cli, "publish_and_poll", pytest.fail)
    overlay_file = tmp_path / "caller-overlay.yaml"
    overlay_file.write_text(
        "substrate_probe_url: x\nregistry_dir: y\n", encoding="utf-8"
    )

    with pytest.raises(SystemExit) as exited:
        cli.main(
            [
                "--date",
                "2026-11-03",
                "--workspace",
                str(tmp_path),
                "--overlay",
                str(overlay_file),
            ]
        )

    assert exited.value.code == 2
    assert (
        "node_architecture_discovery_orchestrator/overlay.yaml"
        in capsys.readouterr().err
    )


def test_an_undeclared_lane_is_refused_naming_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_root = install_broker_overlays(
        tmp_path, monkeypatch, {"type": "kafka", "lane": "missing-lane"}
    )
    with pytest.raises(NodeBrokerResolutionError, match="not usable") as refused:
        resolve_dialable_broker("node_coding_agent_invoke_effect")
    assert str(config_root) in str(refused.value)


# -------------------------------------------- the registered resolver, when installed


class _Binding:
    """What omnibase_core's ``ModelNodeBrokerBinding`` answers, read structurally."""

    class _Kind:
        requires_broker = True

    bus_type = _Kind()
    overlay_path = Path("/registered/overlay.yaml")
    lane = "dev"
    bootstrap_servers = "registered-broker.test:19092"

    def client_security(self) -> dict[str, str]:
        return {"security.protocol": "SASL_PLAINTEXT", "sasl.password": SASL_MARKER}


class _Resolver:
    def resolve_node_broker(self, node_id: str) -> _Binding:
        return _Binding()


def _registered(*resolvers: type) -> Any:
    points = [
        EntryPoint(
            name=f"p{i}",
            value=f"x.y:{r.__name__}",
            group=node_broker.NODE_BROKER_RESOLVER_ENTRY_POINT_GROUP,
        )
        for i, r in enumerate(resolvers)
    ]
    loaded = {f"p{i}": r for i, r in enumerate(resolvers)}

    def entry_points(*, group: str) -> list[Any]:
        assert group == node_broker.NODE_BROKER_RESOLVER_ENTRY_POINT_GROUP
        wrapped = []
        for point in points:
            stub = type(
                "Stub",
                (),
                {
                    "name": point.name,
                    "value": point.value,
                    "load": staticmethod(lambda name=point.name: loaded[name]),
                },
            )()
            wrapped.append(stub)
        return wrapped

    return patch.object(node_broker, "entry_points", entry_points)


def test_a_registered_resolver_answers_and_the_overlay_reader_is_not_consulted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_broker_overlays(tmp_path, monkeypatch, None, nodes=())
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", AMBIENT_BROKER)
    with _registered(_Resolver):
        broker = resolve_dialable_broker("node_coding_agent_invoke_effect")
    assert broker.bootstrap_servers == "registered-broker.test:19092"
    assert broker.client_security()["sasl.password"] == SASL_MARKER
    assert SASL_MARKER not in repr(broker)


def test_two_registered_resolvers_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_broker_overlays(tmp_path, monkeypatch, KAFKA_DEV)

    class _Other(_Resolver):
        pass

    with (
        _registered(_Resolver, _Other),
        pytest.raises(
            NodeBrokerResolutionError, match="more than one node broker resolver"
        ),
    ):
        resolve_node_broker("node_coding_agent_invoke_effect")


# ------------------------------------------------------------ the private helper is gone


def test_neither_caller_imports_the_private_core_helper_or_reads_the_broker_variables() -> (
    None
):
    root = Path(handler_coding_agent_bus.__file__).parent.parent
    for relative in (
        "adapters/handler_coding_agent_bus.py",
        "runtime_architecture_discovery_cli.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "_resolve_client_security_config" not in source
        assert "KAFKA_BOOTSTRAP_SERVERS" not in source
        assert "KAFKA_SASL" not in source
