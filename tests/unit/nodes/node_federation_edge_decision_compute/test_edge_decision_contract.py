# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node is a pure definition-B COMPUTE: contract, handler shape, purity.

Golden: ``contract.yaml`` names ``node_federation_edge_decision_compute``, a
``COMPUTE_GENERIC`` with ``purity: pure``, and its input and output model
paths import to exactly ``ModelEdgeDecisionRequest`` and
``ModelEdgeDecision``, its handler path to exactly
``HandlerFederationEdgeDecision``. The handler is sync, takes one
``request: ModelEdgeDecisionRequest`` and answers one ``ModelEdgeDecision``,
the node's ``__init__`` exports the wrapper class, and the wrapper decides a
real request. Purity: the handler source imports none of ``os``,
``subprocess``, ``socket``, ``time``, ``random``, ``requests``, ``httpx``,
``pathlib`` or ``datetime``, and never calls ``datetime.now``, ``utcnow``,
``time.time`` or ``monotonic`` — the ``now`` arrives in the request.
"""

from __future__ import annotations

import ast
import base64
import importlib
import inspect
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_federation_edge_decision_compute import (
    NodeFederationEdgeDecisionCompute,
)
from omnimarket.nodes.node_federation_edge_decision_compute.handlers import (
    handler_federation_edge_decision,
)
from omnimarket.nodes.node_federation_edge_decision_compute.handlers.handler_federation_edge_decision import (
    HandlerFederationEdgeDecision,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.model_edge_decision import (
    ModelAclProbeRecord,
    ModelAclProbeStep,
    ModelEdgeDecision,
    ModelEdgeDecisionRequest,
    ModelLaneDeclaration,
    ModelRecordedEvent,
    ModelRegistryTopicEntry,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.model_edge_overlay import (
    ModelEdgeOverlayFile,
)
from omnimarket.nodes.node_federation_egress_gate_compute.models.enum_egress_gate import (
    EnumEgressOutcome,
)
from omnimarket.nodes.node_federation_egress_gate_compute.models.model_egress_gate import (
    ModelEgressField,
    ModelWireHeader,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(handler_federation_edge_decision.__file__).resolve().parents[1]

OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_federation_edge_decision_compute/overlay.example.yaml"
)

# The fixed decision moment; the handler reads no clock, so ``now`` is input.
NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)

# One fixed uuid string: the recorded event's command_id and key bytes.
COMMAND_ID = "0f7e1a9c-5b2d-4c8e-a1d3-9f6b2e7c4a50"

# The message-id value: a uuid derived from COMMAND_ID, so it fits the
# overlay's ``^[0-9a-f-]{36}$`` header rule and never equals the key text.
MESSAGE_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, COMMAND_ID))

# The connection's mechanism, as the lane declaration declares it.
MECHANISM = "example-mechanism"

_OVERLAY: ModelEdgeOverlayFile | None = None


def _overlay() -> ModelEdgeOverlayFile:
    """The example overlay, loaded once and shared (frozen, so immutable)."""
    global _OVERLAY
    if _OVERLAY is None:
        document = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
        _OVERLAY = ModelEdgeOverlayFile.model_validate(document)
    return _OVERLAY


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _value_bytes(command_id: str) -> bytes:
    """The event's raw value bytes: one JSON object naming the command."""
    return b'{"command_id":"' + command_id.encode("ascii") + b'"}'


def _event(**overrides: Any) -> ModelRecordedEvent:
    """The native unstamped recording of one event on the allowed topic."""
    direction = _overlay().direction
    values: dict[str, Any] = {
        "topic": direction.allowed_topic,
        "key_b64": _b64(COMMAND_ID.encode("ascii")),
        "value_b64": _b64(_value_bytes(COMMAND_ID)),
        "headers": (
            ModelWireHeader(name=direction.message_id_header, value=MESSAGE_ID),
        ),
    }
    values.update(overrides)
    return ModelRecordedEvent(**values)


def _probe(**overrides: Any) -> ModelAclProbeRecord:
    """This direction's fresh probe: a b allowed, c d e denied, 60s old."""
    to = _overlay().direction.to
    values: dict[str, Any] = {
        "probe_ref": to.acl_probe_ref,
        "lane": to.lane,
        "principal_ref": _overlay().binding.principal_ref,
        "recorded_at": NOW - timedelta(seconds=60),
        "steps": (
            ModelAclProbeStep(step="a", expected="allowed", observed="allowed"),
            ModelAclProbeStep(step="b", expected="allowed", observed="allowed"),
            ModelAclProbeStep(step="c", expected="denied", observed="denied"),
            ModelAclProbeStep(step="d", expected="denied", observed="denied"),
            ModelAclProbeStep(step="e", expected="denied", observed="denied"),
        ),
    }
    values.update(overrides)
    return ModelAclProbeRecord(**values)


def _registry_entry(**overrides: Any) -> ModelRegistryTopicEntry:
    """The allowed topic's registry entry: internal, closed one-field shape."""
    direction = _overlay().direction
    values: dict[str, Any] = {
        "topic": direction.allowed_topic,
        "classes": ("internal",),
        "atomic_group": False,
        "fields": (
            ModelEgressField(
                name=direction.key_binding,
                kind="string",
                required=True,
                pattern=direction.key_pattern,
                max_length=36,
            ),
        ),
    }
    values.update(overrides)
    return ModelRegistryTopicEntry(**values)


def _lane_declaration(**overrides: Any) -> ModelLaneDeclaration:
    """The destination lane as the deployment declares it."""
    values: dict[str, Any] = {
        "lane": _overlay().direction.to.lane,
        "auth_mechanism": MECHANISM,
        "granted_write_principals": (_overlay().binding.principal_ref,),
    }
    values.update(overrides)
    return ModelLaneDeclaration(**values)


def _request(**overrides: Any) -> ModelEdgeDecisionRequest:
    """The one passing request, with whole-field ``overrides`` applied."""
    values: dict[str, Any] = {
        "recording": (_event(),),
        "overlay": _overlay(),
        "authenticated_identity": _overlay().binding.principal_ref,
        "connection_mechanism": MECHANISM,
        "lane_declaration": _lane_declaration(),
        "registry_entry": _registry_entry(),
        "acl_probe": _probe(),
        "now": NOW,
    }
    values.update(overrides)
    return ModelEdgeDecisionRequest(**values)


class TestContract:
    def test_the_contract_declares_a_pure_compute_with_the_typed_models(
        self,
    ) -> None:
        contract = yaml.safe_load(
            (NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
        )
        assert contract["name"] == "node_federation_edge_decision_compute"
        assert contract["node_type"] == "COMPUTE_GENERIC"
        assert contract["descriptor"]["purity"] == "pure"
        for side, model in (
            ("input_model", ModelEdgeDecisionRequest),
            ("output_model", ModelEdgeDecision),
        ):
            declared = contract[side]
            module = importlib.import_module(declared["module"])
            assert getattr(module, declared["name"]) is model
        handler = contract["handler"]
        assert (
            getattr(importlib.import_module(handler["module"]), handler["class"])
            is HandlerFederationEdgeDecision
        )

    def test_the_contract_declares_both_terminal_events_and_publishes_them(
        self,
    ) -> None:
        contract = yaml.safe_load(
            (NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
        )
        completed = "onex.evt.omnimarket.federation-edge-decision-completed.v1"
        failed = "onex.evt.omnimarket.federation-edge-decision-failed.v1"
        assert contract["runtime_dispatch"]["terminal_events"] == {
            "success": completed,
            "failure": failed,
        }
        assert contract["event_bus"]["publish_topics"] == [completed, failed]


class TestHandlerShape:
    def test_the_handler_is_definition_b(self) -> None:
        signature = inspect.signature(HandlerFederationEdgeDecision.handle)
        params = list(signature.parameters.values())
        assert [p.name for p in params] == ["self", "request"]
        assert params[1].annotation in (
            ModelEdgeDecisionRequest,
            "ModelEdgeDecisionRequest",
        )
        assert signature.return_annotation in (
            ModelEdgeDecision,
            "ModelEdgeDecision",
        )
        assert not inspect.iscoroutinefunction(HandlerFederationEdgeDecision.handle)
        assert issubclass(
            NodeFederationEdgeDecisionCompute, HandlerFederationEdgeDecision
        )
        decision = NodeFederationEdgeDecisionCompute().handle(_request())
        assert isinstance(decision, ModelEdgeDecision)
        assert decision.outcome is EnumEgressOutcome.PASS
        assert decision.not_selected == 0


class TestPurity:
    def test_the_handler_does_no_io_and_reads_no_clock(self) -> None:
        source = Path(handler_federation_edge_decision.__file__).read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
                imported.add(node.module)
        forbidden = {
            "os",
            "subprocess",
            "socket",
            "time",
            "random",
            "requests",
            "httpx",
            "pathlib",
            "datetime",
        }
        assert not imported & forbidden, imported & forbidden
        assert "ModelEventEnvelope" not in source
        assert "ModelHandlerOutput" not in source
        assert ".now(" not in source
        assert "utcnow" not in source
        assert "monotonic" not in source
