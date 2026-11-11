# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Local GPU backends the delegation routing overlay declares (OMN-23871).

A deployment's local rung is a GPU backend served on the machine itself. Its
complete endpoint and the model it serves are deployment facts, so the
package declares only the shape; the values come from the routing overlay
bound through ``DELEGATION_ROUTING_OVERLAY_PATH``. The bifrost loaders bind
the section onto the routing contract's local backends, so a machine with no
machine-local ``bifrost_overrides.yaml`` still resolves the local rung; an
absent section changes nothing.

Properties enforced here:

* every backend's provider is exactly ``local`` and its kind is ``endpoint``;
* every backend states a complete ``endpoint_url`` and a ``model_name``;
* no backend is ``explicit_pin_only``: a local rung must be selectable by the
  ladder, not reachable only by a pin;
* no two backends share a ``backend_id``.
"""

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation.model_delegation_backend_placement import (
    ModelDelegationBackendPlacement,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendKind,
    ModelDelegationBackendConfig,
)


class ModelLocalEndpoint(ModelDelegationBackendConfig):
    """One local backend row: the wire backend plus its optional tier placement.

    A placement is routing configuration the loaders lift off before the wire
    model validates (OMN-19215), so the row carries it as a field of its own.
    """

    placement: ModelDelegationBackendPlacement | None = None


class ModelLocalEndpointsOverlay(BaseModel):
    """The local GPU backends a deployment declares for its routing overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backends: tuple[ModelLocalEndpoint, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _validate_endpoints(self) -> ModelLocalEndpointsOverlay:
        """Report every local endpoint that could not serve or be selected."""
        problems: list[str] = []
        for name, count in Counter(b.backend_id for b in self.backends).items():
            if count > 1:
                problems.append(f"duplicate local endpoint backend_id {name!r}")
        for backend in self.backends:
            bid = backend.backend_id
            if backend.provider != "local":
                problems.append(f"local endpoint {bid!r} provider must be local")
            if backend.kind is not EnumDelegationBackendKind.ENDPOINT:
                problems.append(f"local endpoint {bid!r} kind must be endpoint")
            if not (backend.endpoint_url or "").strip():
                problems.append(f"local endpoint {bid!r} needs endpoint_url")
            if not (backend.model_name or "").strip():
                problems.append(f"local endpoint {bid!r} needs model_name")
            if backend.explicit_pin_only:
                problems.append(
                    f"local endpoint {bid!r} must not be explicit_pin_only; "
                    "a local rung must be selectable"
                )
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def backend_rows(self) -> list[dict[str, object]]:
        """The local endpoints as routing-contract rows.

        A row carries only the fields the overlay states (``exclude_unset``), so
        an override of a contract backend sets exactly those keys and clobbers
        nothing the contract declares.
        """
        return [
            backend.model_dump(mode="json", exclude_unset=True, exclude_none=True)
            for backend in self.backends
        ]


__all__: list[str] = [
    "ModelLocalEndpoint",
    "ModelLocalEndpointsOverlay",
]
