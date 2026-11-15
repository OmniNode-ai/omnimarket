# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay's claims against the deployment's tables: every refusal, ordered.

Golden: the example overlay with a matching binding table, the lane declared
with an auth mechanism and granting the binding's principal, and the allowed
topic registered internal, not atomic-grouped and closed over the bound
``command_id`` field gives ok with no refusals. Error chain: exactly one
thing made wrong per refusal name of ``EnumOverlayRefusal``, and a request
with several things wrong at once answers every refusal together, in the
enum's declaration order. Determinism: the same request twice, and the same
tables in another order, give the same answer. The handler's own source
imports none of socket, subprocess, os, time or datetime.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from omnimarket.models.federation_egress.model_egress_gate import (
    ModelEgressField,
)
from omnimarket.nodes.node_federation_edge_decision_compute.handlers.handler_edge_overlay_validation import (
    HandlerEdgeOverlayValidation,
    validate_edge_overlay,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.enum_edge_decision import (
    EnumOverlayRefusal,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.model_edge_decision import (
    ModelLaneDeclaration,
    ModelRegistryTopicEntry,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.model_edge_overlay import (
    ModelEdgeOverlayFile,
    ModelRuntimeBinding,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.model_edge_overlay_validation import (
    ModelOverlayValidationRequest,
)

pytestmark = pytest.mark.unit

HANDLER_SOURCE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_federation_edge_decision_compute"
    "/handlers/handler_edge_overlay_validation.py"
)
OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_federation_edge_decision_compute"
    "/overlay.example.yaml"
)

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
ALLOWED_TOPIC = "onex.cmd.omnimarket.lifecycle-start-requested.v1"


def _overlay() -> ModelEdgeOverlayFile:
    return ModelEdgeOverlayFile.model_validate(
        yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    )


def _binding(**overrides: object) -> ModelRuntimeBinding:
    values: dict[str, object] = {
        "runtime_ref": "example-runtime-a",
        "principal_ref": "example-edge-writer",
        "tenant_slug_ref": "example-tenant-a",
        "origin_lane": "example-lane",
        "permitted_lanes": ("example-lane",),
    }
    values.update(overrides)
    return ModelRuntimeBinding(**values)


def _other_runtime_binding() -> ModelRuntimeBinding:
    """A second runtime's entry, sharing the principal unless told not to."""
    return _binding(
        runtime_ref="example-runtime-b",
        tenant_slug_ref="example-tenant-b",
        origin_lane="example-lane-b",
        permitted_lanes=("example-lane-b",),
    )


def _lane_declaration(**overrides: object) -> ModelLaneDeclaration:
    values: dict[str, object] = {
        "lane": "example-lane",
        "auth_mechanism": "example-mechanism",
        "granted_write_principals": ("example-edge-writer",),
    }
    values.update(overrides)
    return ModelLaneDeclaration(**values)


def _registry_entry(**overrides: object) -> ModelRegistryTopicEntry:
    values: dict[str, object] = {
        "topic": ALLOWED_TOPIC,
        "classes": ("internal",),
        "atomic_group": False,
        "fields": (
            ModelEgressField(
                name="command_id",
                kind="string",
                required=True,
                pattern=UUID_PATTERN,
                max_length=36,
            ),
        ),
    }
    values.update(overrides)
    return ModelRegistryTopicEntry(**values)


def _request(
    *,
    overlay: ModelEdgeOverlayFile | None = None,
    bindings: tuple[ModelRuntimeBinding, ...] | None = None,
    lane_declarations: tuple[ModelLaneDeclaration, ...] | None = None,
    registry_entries: tuple[ModelRegistryTopicEntry, ...] | None = None,
) -> ModelOverlayValidationRequest:
    return ModelOverlayValidationRequest(
        overlay=overlay if overlay is not None else _overlay(),
        bindings=bindings if bindings is not None else (_binding(),),
        lane_declarations=(
            lane_declarations
            if lane_declarations is not None
            else (_lane_declaration(),)
        ),
        registry_entries=(
            registry_entries if registry_entries is not None else (_registry_entry(),)
        ),
    )


def _broken_request(refusal: EnumOverlayRefusal) -> ModelOverlayValidationRequest:
    """The golden request with exactly one thing made wrong for ``refusal``."""
    if refusal is EnumOverlayRefusal.BINDING_NOT_IN_TABLE:
        return _request(bindings=(_binding(tenant_slug_ref="example-tenant-z"),))
    if refusal is EnumOverlayRefusal.PRINCIPAL_SHARED:
        return _request(bindings=(_binding(), _other_runtime_binding()))
    if refusal is EnumOverlayRefusal.LANE_UNKNOWN:
        return _request(lane_declarations=())
    if refusal is EnumOverlayRefusal.LANE_UNAUTHENTICATED:
        return _request(lane_declarations=(_lane_declaration(auth_mechanism=None),))
    if refusal is EnumOverlayRefusal.LANE_NOT_PERMITTED:
        # The table's entry for the runtime permits only another lane, so the
        # destination lane is not permitted (and the binding no longer equals
        # the overlay's, which is binding-not-in-table's own case).
        return _request(
            bindings=(
                _binding(
                    origin_lane="example-other-lane",
                    permitted_lanes=("example-other-lane",),
                ),
            )
        )
    if refusal is EnumOverlayRefusal.PRINCIPAL_NOT_GRANTED_ON_LANE:
        return _request(
            lane_declarations=(
                _lane_declaration(granted_write_principals=("example-other-writer",)),
            )
        )
    if refusal is EnumOverlayRefusal.TOPIC_NOT_REGISTERED:
        return _request(registry_entries=())
    if refusal is EnumOverlayRefusal.TOPIC_CLASS_NOT_INTERNAL:
        return _request(registry_entries=(_registry_entry(classes=("tenant",)),))
    if refusal is EnumOverlayRefusal.TOPIC_ATOMIC_GROUP:
        return _request(registry_entries=(_registry_entry(atomic_group=True),))
    if refusal is EnumOverlayRefusal.KEY_BINDING_FIELD_MISSING:
        # The registry's closed shape names no command_id field to bind the key to.
        return _request(
            registry_entries=(
                _registry_entry(
                    fields=(
                        ModelEgressField(
                            name="reason_code",
                            kind="string",
                            required=True,
                            pattern=r"^[a-z-]{1,32}$",
                            max_length=32,
                        ),
                    ),
                ),
            )
        )
    # EnumOverlayRefusal.MODEL_NOT_CLOSED: an entry closed over no fields at
    # all (which also leaves the key binding nothing to name).
    return _request(registry_entries=(_registry_entry(fields=()),))


class TestGoldenChain:
    def test_the_example_overlay_closes_against_matching_tables(self) -> None:
        result = validate_edge_overlay(_request())
        assert result.ok is True
        assert result.refusals == ()


class TestErrorChain:
    @pytest.mark.parametrize("refusal", list(EnumOverlayRefusal))
    def test_one_broken_thing_names_its_refusal(
        self,
        refusal: EnumOverlayRefusal,
    ) -> None:
        result = HandlerEdgeOverlayValidation().handle(_broken_request(refusal))
        assert refusal in result.refusals
        assert result.ok is False

    def test_several_broken_things_answer_every_refusal_in_declaration_order(
        self,
    ) -> None:
        request = ModelOverlayValidationRequest(
            overlay=_overlay(),
            bindings=(
                _binding(
                    origin_lane="example-other-lane",
                    permitted_lanes=("example-other-lane",),
                ),
                _other_runtime_binding(),
                _binding(
                    runtime_ref="example-runtime-c",
                    tenant_slug_ref="example-tenant-c",
                    origin_lane="example-lane-c",
                    permitted_lanes=("example-lane-c",),
                ),
            ),
            lane_declarations=(
                _lane_declaration(
                    auth_mechanism=None,
                    granted_write_principals=("example-other-writer",),
                ),
            ),
            registry_entries=(
                _registry_entry(classes=("tenant",), atomic_group=True, fields=()),
            ),
        )
        result = validate_edge_overlay(request)
        assert result.refusals == (
            EnumOverlayRefusal.BINDING_NOT_IN_TABLE,
            EnumOverlayRefusal.PRINCIPAL_SHARED,
            EnumOverlayRefusal.LANE_UNAUTHENTICATED,
            EnumOverlayRefusal.LANE_NOT_PERMITTED,
            EnumOverlayRefusal.PRINCIPAL_NOT_GRANTED_ON_LANE,
            EnumOverlayRefusal.TOPIC_CLASS_NOT_INTERNAL,
            EnumOverlayRefusal.TOPIC_ATOMIC_GROUP,
            EnumOverlayRefusal.KEY_BINDING_FIELD_MISSING,
            EnumOverlayRefusal.MODEL_NOT_CLOSED,
        )
        assert result.ok is False


class TestDeterminism:
    def test_the_same_request_twice_gives_the_same_answer(self) -> None:
        request = _broken_request(EnumOverlayRefusal.PRINCIPAL_NOT_GRANTED_ON_LANE)
        first = HandlerEdgeOverlayValidation().handle(request)
        second = HandlerEdgeOverlayValidation().handle(request)
        assert first == second
        assert first.model_dump_json() == second.model_dump_json()

    def test_the_tables_in_another_order_give_the_same_refusals(self) -> None:
        bindings = (_binding(), _other_runtime_binding())
        lane_declarations = (
            _lane_declaration(),
            _lane_declaration(
                lane="example-lane-b",
                auth_mechanism="example-mechanism-b",
                granted_write_principals=("example-lane-b-writer",),
            ),
        )
        registry_entries = (_registry_entry(),)
        first = validate_edge_overlay(
            _request(
                bindings=bindings,
                lane_declarations=lane_declarations,
                registry_entries=registry_entries,
            )
        )
        second = validate_edge_overlay(
            _request(
                bindings=tuple(reversed(bindings)),
                lane_declarations=tuple(reversed(lane_declarations)),
                registry_entries=tuple(reversed(registry_entries)),
            )
        )
        assert first == second
        assert first.refusals == (EnumOverlayRefusal.PRINCIPAL_SHARED,)


class TestPurity:
    def test_the_handler_source_imports_no_io_or_clock_module(self) -> None:
        tree = ast.parse(HANDLER_SOURCE.read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        assert roots.isdisjoint({"socket", "subprocess", "os", "time", "datetime"})
