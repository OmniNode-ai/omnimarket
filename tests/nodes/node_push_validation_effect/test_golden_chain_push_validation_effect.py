# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_push_validation_effect (gateway P2 tenant #1, OMN-14920).

Satisfies golden-chain-coverage-gate (OMN-12691) and the "Golden Chain Suite"
CI job for node_push_validation_effect: contract + metadata structural
validation, the exact topic/event_type seam strings shared with the
omninode_infra gateway (workflow_type "push-validation"), the committed
cross-repo wire fixture that the omninode_infra tenant-#1 PR vendors
byte-identically, and the ``handler_routing`` binding of the def-B handler
(``HandlerPushValidationEffect``, operation ``run_push_validation`` —
hand-written under the 2026-07-22 documented-exception grant; contract +
acceptance suite are the future RSD regeneration target).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.nodes.node_push_validation_effect.models.model_push_validation_request import (
    ModelPushValidationRequest,
)

COMMAND_TOPIC = "onex.cmd.omnimarket.push-validation-requested.v1"
SUCCESS_TOPIC = "onex.evt.omnimarket.push-validation-completed.v1"
FAILURE_TOPIC = "onex.evt.omnimarket.push-validation-failed.v1"
DLQ_TOPIC = "onex.dlq.omnimarket.push-validation.v1"
EVENT_TYPE = "omnimarket.push-validation-requested"

# Mirror of omninode_infra docker/onex-api/models/model_workflow_contracts.py
# EVENT_TYPE_PATTERN: event_type MUST be namespaced <producer>.<event-name>;
# un-namespaced = silent no_dispatcher drop (the OMN-12912 defect).
EVENT_TYPE_PATTERN = re.compile(
    r"^[a-z][a-z0-9-]*[a-z0-9](\.[a-z][a-z0-9-]*[a-z0-9])+$"
)

# The 11 golden top-level wire keys pinned by omninode_infra
# tests/test_wire_envelope_golden.py (GOLDEN_ENVELOPE_KEYS), in insertion
# order — key order is construction order (no sort_keys) and is byte-load-bearing.
GOLDEN_ENVELOPE_KEYS = [
    "payload",
    "envelope_id",
    "envelope_timestamp",
    "correlation_id",
    "source_tool",
    "metadata",
    "event_type",
    "priority",
    "retry_count",
    "onex_version",
    "envelope_version",
]

# The 9 golden gateway audit tag keys (ALL-STRING metadata.tags).
GOLDEN_TAG_KEYS = [
    "workflow_id",
    "workflow_type",
    "contract_id",
    "schema_version",
    "causation_id",
    "source_tenant_id",
    "source_tenant_principal_id",
    "authenticated_user_id",
    "source_gateway_instance",
]

# Canonical fields that are None-valued for this seam and therefore ABSENT
# from the wire (exclude_none serialization).
ABSENT_NONE_FIELDS = [
    "target_tool",
    "security_context",
    "payload_type",
    "payload_schema_version",
    "timeout_seconds",
    "request_id",
    "trace_id",
    "span_id",
]


@pytest.fixture
def node_dir() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_push_validation_effect"
    )


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


@pytest.fixture
def metadata_path(node_dir: Path) -> Path:
    return node_dir / "metadata.yaml"


@pytest.fixture
def fixture_path(node_dir: Path) -> Path:
    return node_dir / "fixtures" / "push_validation_requested_wire_envelope.json"


@pytest.fixture
def wire_line(fixture_path: Path) -> str:
    """The single byte-canonical wire line (fixture file minus trailing newline)."""
    raw = fixture_path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    assert len(lines) == 1, "wire fixture must be a single canonical JSON line"
    return lines[0]


@pytest.fixture
def wire(wire_line: str) -> dict:
    return json.loads(wire_line)


class TestContractYaml:
    def test_contract_exists(self, contract_path: Path) -> None:
        assert contract_path.exists()

    def test_contract_loads(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        assert isinstance(data, dict)
        assert data["name"] == "node_push_validation_effect"
        assert data["lifecycle"] == "experimental"
        assert data["node_type"] == "EFFECT_GENERIC"
        assert data["runtime_profiles"] == ["effects"]

    def test_contract_declares_io_models(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        assert data["input_model"]["name"] == "ModelPushValidationRequest"
        assert data["input_model"]["module"] == (
            "omnimarket.nodes.node_push_validation_effect.models."
            "model_push_validation_request"
        )
        assert data["output_model"]["name"] == "ModelPushValidationReceipt"
        assert data["output_model"]["module"] == (
            "omnimarket.nodes.node_push_validation_effect.models."
            "model_push_validation_receipt"
        )

    def test_contract_declares_runtime_dispatch_topics(
        self, contract_path: Path
    ) -> None:
        """OMN-14920: the seam topic strings, exactly as the gateway publishes."""
        data = yaml.safe_load(contract_path.read_text())
        assert data["runtime_dispatch"] == {
            "command_topic": COMMAND_TOPIC,
            "terminal_events": {
                "success": SUCCESS_TOPIC,
                "failure": FAILURE_TOPIC,
            },
        }
        assert data["event_bus"] == {
            "subscribe_topics": [COMMAND_TOPIC],
            "publish_topics": [SUCCESS_TOPIC, FAILURE_TOPIC],
            "dlq_topics": [DLQ_TOPIC],
        }
        assert data["terminal_event"] == SUCCESS_TOPIC

    def test_contract_declares_idempotency_key(self, contract_path: Path) -> None:
        """At-least-once redelivery must not double-push (OMN-14920 semantics #5)."""
        data = yaml.safe_load(contract_path.read_text())
        side_effects = data["side_effects"]
        assert side_effects["duplicate_key_fields"] == [
            "repo",
            "branch",
            "expected_head_sha",
        ]
        assert side_effects["duplicate_handling"] == "idempotent"

    def test_contract_routes_command_to_handler(self, contract_path: Path) -> None:
        """The contract-topic-graph gate rejects subscribe-without-handler:
        the command topic must route to the def-B handler."""
        data = yaml.safe_load(contract_path.read_text())
        routing = data["handler_routing"]
        assert routing["routing_strategy"] == "operation_match"
        assert routing["handlers"] == [
            {
                "operation": "run_push_validation",
                "handler": {
                    "name": "HandlerPushValidationEffect",
                    "module": (
                        "omnimarket.nodes.node_push_validation_effect."
                        "handlers.handler_push_validation_effect"
                    ),
                },
            }
        ]

    def test_declared_handler_is_boot_resolvable_def_b(
        self, contract_path: Path
    ) -> None:
        """The routed handler imports, constructs from defaults alone
        (boot-resolvable, OMN-13551), and exposes the canonical def-B
        ``handle`` shape: one positional BaseModel-typed ``request`` param."""
        import importlib
        import inspect

        from pydantic import BaseModel

        data = yaml.safe_load(contract_path.read_text())
        spec = data["handler_routing"]["handlers"][0]["handler"]
        module = importlib.import_module(spec["module"])
        handler_cls = getattr(module, spec["name"])
        # Constructor: every param has a default -> resolver-constructable.
        for name, param in inspect.signature(handler_cls.__init__).parameters.items():
            if name == "self":
                continue
            assert param.default is not inspect.Parameter.empty
        # def-B handle: exactly one positional param, concrete-BaseModel typed.
        signature = inspect.signature(handler_cls.handle, eval_str=True)
        positional = [
            p
            for n, p in signature.parameters.items()
            if n != "self"
            and p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        assert len(positional) == 1
        assert positional[0].name == "request"
        assert issubclass(positional[0].annotation, BaseModel)
        # Canon-shape C-core: the handler module never references the
        # event-envelope type (the envelope boundary is the runtime adapter).
        source = inspect.getsource(module)
        assert "ModelEventEnvelope" not in source

    def test_contract_text_encodes_acceptance_criteria(
        self, contract_path: Path
    ) -> None:
        """OMN-14920 acceptance criteria are load-bearing contract text."""
        description = yaml.safe_load(contract_path.read_text())["description"]
        # (1) expected_head_sha FAIL-CLOSED — no silent refetch/retry.
        assert "FAIL-CLOSED" in description
        assert "no silent refetch" in description
        assert "stale_head" in description
        # (2) red suite NEVER pushes; suite failure is a COMPLETED-topic receipt.
        assert "suite_failed" in description
        assert "NEVER pushes" in description
        # (3) zero bypass flags; hook readback BEFORE any push.
        assert "--no-verify" in description
        assert "hook_id_readback" in description
        assert "BEFORE any push" in description
        # (4) protected branches refused.
        assert "refused" in description
        # (5) idempotent redelivery.
        assert "already_pushed" in description

    def test_contract_topics_resolve_via_shared_helpers(
        self, contract_path: Path
    ) -> None:
        """Handlers read topics via contract_topics helpers — never hardcoded."""
        assert contract_subscribe_topics(contract_path) == (COMMAND_TOPIC,)
        assert contract_publish_topics(contract_path) == (
            SUCCESS_TOPIC,
            FAILURE_TOPIC,
        )


class TestMetadataYaml:
    def test_metadata_exists(self, metadata_path: Path) -> None:
        assert metadata_path.exists()

    def test_metadata_loads(self, metadata_path: Path) -> None:
        data = yaml.safe_load(metadata_path.read_text())
        assert data["name"] == "node_push_validation_effect"
        assert "version" in data
        assert data["entry_points"]["onex.nodes"]["node_push_validation_effect"] == (
            "omnimarket.nodes.node_push_validation_effect"
        )

    def test_metadata_declares_write_network_required(
        self, metadata_path: Path
    ) -> None:
        data = yaml.safe_load(metadata_path.read_text())
        caps = data["capabilities"]
        assert caps["side_effect_class"] == "write"
        assert caps["requires_network"] is True


class TestEventTypeSeam:
    def test_event_type_matches_gateway_pattern(self) -> None:
        assert EVENT_TYPE_PATTERN.match(EVENT_TYPE)

    def test_event_type_equals_runtime_topic_derivation(self) -> None:
        """The runtime derives event_type as {producer}.{event-name} from
        topic onex.{kind}.{producer}.{event-name}.v{n}
        (omnibase_infra event_bus_subcontract_wiring._derive_event_type_from_topic).
        """
        parts = COMMAND_TOPIC.split(".")
        assert parts[0] == "onex"
        assert parts[1] == "cmd"
        derived = f"{parts[2]}.{parts[3]}"
        assert derived == EVENT_TYPE

    def test_terminal_event_types_match_pattern(self) -> None:
        for topic in (SUCCESS_TOPIC, FAILURE_TOPIC):
            parts = topic.split(".")
            assert EVENT_TYPE_PATTERN.match(f"{parts[2]}.{parts[3]}")


class TestWireFixture:
    """The committed cross-repo seam fixture, vendored byte-identically by the
    omninode_infra tenant-#1 PR (asserted there byte-equal against
    serialize_envelope output for the fixture inputs)."""

    def test_fixture_is_byte_canonical(self, wire_line: str, wire: dict) -> None:
        """serialize_envelope is json.dumps with defaults: separators
        (", ", ": "), ensure_ascii=True, NO sort_keys (insertion order)."""
        assert json.dumps(wire) == wire_line

    def test_golden_top_level_key_set_and_order(self, wire: dict) -> None:
        assert list(wire.keys()) == GOLDEN_ENVELOPE_KEYS

    def test_none_valued_canonical_fields_are_absent(self, wire: dict) -> None:
        for field_name in ABSENT_NONE_FIELDS:
            assert field_name not in wire

    def test_pinned_envelope_constants(self, wire: dict) -> None:
        assert wire["envelope_version"] == {"major": 2, "minor": 1, "patch": 0}
        assert wire["onex_version"] == {"major": 1, "minor": 0, "patch": 0}
        assert wire["priority"] == 5
        assert wire["retry_count"] == 0
        assert wire["event_type"] == EVENT_TYPE

    def test_payload_parses_into_request_model(self, wire: dict) -> None:
        """extra='forbid' round-trip: the wire payload IS the request model."""
        request = ModelPushValidationRequest(**wire["payload"])
        assert request.repo == "OmniNode-ai/omnibase_core"
        assert request.branch == "jonah/omn-14920-sample"
        assert request.expected_head_sha == ("0123456789abcdef0123456789abcdef01234567")
        assert request.requester == "session:fable-dogfood-0722"
        assert request.tenant_id == "push-farm"

    def test_contract_v2_fixture_carries_mode_and_source_identity(
        self, wire: dict
    ) -> None:
        """Contract v2 (OMN-14976, seam-mismatch fix): the fixture is
        RE-PINNED (not left unchanged) to carry mode=validate_only and a
        commit source_identity, byte-identical to the omninode_infra
        gateway's re-pinned fixture. Default-value coverage for an
        omitted mode/source_identity submission (mode=validate_and_push,
        source_identity=None) lives directly on the model in
        test_contract_v2_omn14976.py::TestModeField.test_default_mode_is_validate_and_push
        / TestSourceIdentityInvariants.test_absent_source_identity_is_valid
        — this fixture no longer needs to double as that proof."""
        from omnimarket.nodes.node_push_validation_effect.models.model_push_validation_request import (
            EnumPushValidationMode,
            EnumSourceIdentityType,
        )

        request = ModelPushValidationRequest(**wire["payload"])
        assert request.mode == EnumPushValidationMode.VALIDATE_ONLY
        assert request.source_identity is not None
        assert request.source_identity.identity_type == EnumSourceIdentityType.COMMIT
        assert request.source_identity.expected_head_sha == request.expected_head_sha

    def test_payload_key_order_matches_model_field_order(self, wire: dict) -> None:
        """The RE-PINNED fixture carries the exact field SET the model
        declares, and the relative order WITHIN each of the two seam
        generations agrees — but full positional equality does NOT hold,
        by design: the wire inserts mode/source_identity right after
        requester (the catalog's payload_schema property declaration
        order in workflow-contracts.yaml), while the model APPENDS them
        at the end of its field list (the frozen-seam invariant proven by
        test_contract_v2_fields_are_appended_after_the_frozen_seam below).
        That interleaving asymmetry is harmless — Pydantic validates
        extra="forbid" payloads by field NAME, never position — but is
        asserted explicitly here so it stays a documented, intentional
        divergence rather than an unexamined one."""
        wire_keys = list(wire["payload"].keys())
        model_field_names = list(ModelPushValidationRequest.model_fields)
        assert set(wire_keys) == set(model_field_names)

        v2_fields = {"mode", "source_identity"}
        wire_v1_fields = [k for k in wire_keys if k not in v2_fields]
        model_v1_fields = [k for k in model_field_names if k not in v2_fields]
        assert wire_v1_fields == model_v1_fields

        wire_v2_fields = [k for k in wire_keys if k in v2_fields]
        model_v2_fields = [k for k in model_field_names if k in v2_fields]
        assert wire_v2_fields == model_v2_fields

    def test_contract_v2_fields_are_appended_after_the_frozen_seam(self) -> None:
        """mode/source_identity must come AFTER every OMN-14920 field, never
        interleaved — that is what keeps the prefix check above meaningful."""
        model_field_names = list(ModelPushValidationRequest.model_fields)
        frozen_seam_fields = [
            "repo",
            "branch",
            "expected_head_sha",
            "requester",
            "correlation_id",
            "emitted_at",
            "tenant_id",
            "tenant_principal_id",
        ]
        assert model_field_names[: len(frozen_seam_fields)] == frozen_seam_fields
        assert model_field_names[len(frozen_seam_fields) :] == [
            "mode",
            "source_identity",
        ]

    def test_payload_correlation_id_equals_envelope_correlation_id(
        self, wire: dict
    ) -> None:
        """Gateway invariant: the projection consumer must assert this and
        DLQ on divergence — never read correlation from a transport header."""
        assert wire["payload"]["correlation_id"] == wire["correlation_id"]

    def test_payload_emitted_at_equals_envelope_timestamp(self, wire: dict) -> None:
        assert wire["payload"]["emitted_at"] == wire["envelope_timestamp"]

    def test_golden_tag_keys_all_strings(self, wire: dict) -> None:
        tags = wire["metadata"]["tags"]
        assert list(tags.keys()) == GOLDEN_TAG_KEYS
        assert all(isinstance(value, str) for value in tags.values())
        assert wire["metadata"]["headers"] == {}

    def test_causation_id_tag_is_envelope_id(self, wire: dict) -> None:
        """The fixture is a root submission: causation_id == envelope_id."""
        assert wire["metadata"]["tags"]["causation_id"] == wire["envelope_id"]

    def test_tenant_principal_derivation(self, wire: dict) -> None:
        """principal_id == 't-' + tenant_id.hex (slug-independent by
        construction; tenant_identity.derive_principal_id)."""
        tags = wire["metadata"]["tags"]
        expected_principal = "t-" + UUID(tags["source_tenant_id"]).hex
        assert tags["source_tenant_principal_id"] == expected_principal
        assert wire["payload"]["tenant_principal_id"] == expected_principal

    def test_contract_id_tag_binds_to_this_contract(
        self, wire: dict, contract_path: Path
    ) -> None:
        contract = yaml.safe_load(contract_path.read_text())
        version = contract["contract_version"]
        contract_id = (
            f"{contract['name']}:"
            f"{version['major']}.{version['minor']}.{version['patch']}"
        )
        assert wire["metadata"]["tags"]["contract_id"] == contract_id
        assert contract_id == "node_push_validation_effect:1.1.0"
        assert wire["metadata"]["tags"]["workflow_type"] == "push-validation"
