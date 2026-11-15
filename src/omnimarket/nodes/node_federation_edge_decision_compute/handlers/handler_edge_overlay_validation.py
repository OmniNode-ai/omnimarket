# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerEdgeOverlayValidation: the overlay's claims checked against the tables.

The edge overlay pins itself to one runtime binding, one lane, one topic and
one key binding; before an edge step is decided, those claims are
cross-checked against the deployment's own tables -- the runtime binding
table, the lane table and the topic registry -- all of which arrive whole
inside the request. ``handle(self, request: ModelOverlayValidationRequest)
-> ModelOverlayValidationResult`` is sync and pure: no I/O, no clock, no
env, and it answers with every refusal that holds the overlay open, not
only the first one found.

The refusals, each a value of ``EnumOverlayRefusal``:

1. The binding table holds no entry equal to the overlay's binding (the
   same ``runtime_ref`` and every other field equal):
   ``binding-not-in-table``.
2. Two entries of the binding table with different ``runtime_ref`` share a
   ``principal_ref``: ``principal-shared``.
3. The lane table declares no lane for ``direction.to.lane``:
   ``lane-unknown``, and the checks that read a declaration are skipped.
   When one exists: its ``auth_mechanism`` is None or blank
   (``lane-unauthenticated``); the lane is not one the binding table's
   entry for the runtime permits (``lane-not-permitted`` -- the overlay's
   own claim is already enforced when the file loads, so the table's entry
   is the one that can disagree; with no table entry for the runtime the
   overlay's own binding stands, already agreed at the model boundary);
   and the binding's ``principal_ref`` is not among the declaration's
   ``granted_write_principals`` (``principal-not-granted-on-lane``).
4. The registry holds no entry whose topic is ``direction.allowed_topic``:
   ``topic-not-registered``, and the checks after it are skipped. When one
   exists: ``internal`` not among its classes
   (``topic-class-not-internal``); its ``atomic_group`` set
   (``topic-atomic-group``); ``direction.key_binding`` names no field of
   kind string among its fields (``key-binding-field-missing``); and its
   fields are empty (``model-not-closed`` -- a field of a kind other than
   string, integer or boolean cannot occur, the field model refuses it at
   the boundary).

The answer carries every refusal found, deduplicated, in
``EnumOverlayRefusal``'s declaration order; ``ok`` is exactly none found.
The same request always gives the same answer, whatever order the tables
arrive in.
"""

from __future__ import annotations

from omnimarket.nodes.node_federation_edge_decision_compute.models.enum_edge_decision import (
    EnumOverlayRefusal,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.model_edge_overlay_validation import (
    ModelOverlayValidationRequest,
    ModelOverlayValidationResult,
)

# Where each refusal stands in the enum's declaration: the order the answer
# carries them in, whatever order the checks found them.
_DECLARATION_ORDER: dict[EnumOverlayRefusal, int] = {
    refusal: index for index, refusal in enumerate(EnumOverlayRefusal)
}


def _is_blank(text: str) -> bool:
    """Whether an auth mechanism name is blank: empty or whitespace only."""
    return not text.strip()


def validate_edge_overlay(
    request: ModelOverlayValidationRequest,
) -> ModelOverlayValidationResult:
    """The overlay's claims against the tables: every refusal, in declaration order."""
    overlay = request.overlay
    binding = overlay.binding
    direction = overlay.direction
    found: set[EnumOverlayRefusal] = set()

    # 1. The binding table: the overlay's binding whole, and no principal
    # shared between two different runtimes.
    governing = next(
        (
            entry
            for entry in request.bindings
            if entry.runtime_ref == binding.runtime_ref
        ),
        None,
    )
    if not any(entry == binding for entry in request.bindings):
        found.add(EnumOverlayRefusal.BINDING_NOT_IN_TABLE)
    runtime_by_principal: dict[str, str] = {}
    for entry in request.bindings:
        first_runtime = runtime_by_principal.setdefault(
            entry.principal_ref, entry.runtime_ref
        )
        if first_runtime != entry.runtime_ref:
            found.add(EnumOverlayRefusal.PRINCIPAL_SHARED)
            break

    # 2. The lane table: the destination lane declared, authenticated,
    # permitted for the runtime and granting the binding's principal.
    declaration = next(
        (lane for lane in request.lane_declarations if lane.lane == direction.to.lane),
        None,
    )
    if declaration is None:
        found.add(EnumOverlayRefusal.LANE_UNKNOWN)
    else:
        if declaration.auth_mechanism is None or _is_blank(declaration.auth_mechanism):
            found.add(EnumOverlayRefusal.LANE_UNAUTHENTICATED)
        permitted_lanes = (
            governing.permitted_lanes
            if governing is not None
            else binding.permitted_lanes
        )
        if direction.to.lane not in permitted_lanes:
            found.add(EnumOverlayRefusal.LANE_NOT_PERMITTED)
        if binding.principal_ref not in declaration.granted_write_principals:
            found.add(EnumOverlayRefusal.PRINCIPAL_NOT_GRANTED_ON_LANE)

    # 3. The topic registry: the allowed topic registered, internal, not
    # atomic-grouped, and closed over the field the key is bound to.
    registry_entry = next(
        (
            entry
            for entry in request.registry_entries
            if entry.topic == direction.allowed_topic
        ),
        None,
    )
    if registry_entry is None:
        found.add(EnumOverlayRefusal.TOPIC_NOT_REGISTERED)
    else:
        if "internal" not in registry_entry.classes:
            found.add(EnumOverlayRefusal.TOPIC_CLASS_NOT_INTERNAL)
        if registry_entry.atomic_group:
            found.add(EnumOverlayRefusal.TOPIC_ATOMIC_GROUP)
        if not any(
            field.name == direction.key_binding and field.kind == "string"
            for field in registry_entry.fields
        ):
            found.add(EnumOverlayRefusal.KEY_BINDING_FIELD_MISSING)
        if not registry_entry.fields:
            found.add(EnumOverlayRefusal.MODEL_NOT_CLOSED)

    refusals = tuple(sorted(found, key=_DECLARATION_ORDER.__getitem__))
    return ModelOverlayValidationResult(ok=not refusals, refusals=refusals)


class HandlerEdgeOverlayValidation:
    """The edge overlay's validation: pure compute, the tables in, the refusals out."""

    def handle(
        self, request: ModelOverlayValidationRequest
    ) -> ModelOverlayValidationResult:
        """Cross-check one overlay against the declared tables and answer every
        refusal that holds it open, in ``EnumOverlayRefusal`` declaration order."""
        return validate_edge_overlay(request)


__all__: list[str] = [
    "HandlerEdgeOverlayValidation",
    "validate_edge_overlay",
]
