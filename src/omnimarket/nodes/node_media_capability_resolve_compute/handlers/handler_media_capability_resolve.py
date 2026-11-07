# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve a typed media capability to its one serving backend, without I/O (OMN-22774).

The caller reads the lab's media backends from the overlay home and injects them; this handler
judges them. A backend whose declared capability and endpoint path disagree is refused wherever
it sits in the list, so a node asking for speech is never handed a transcription URL.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from omnimarket.nodes.node_media_capability_resolve_compute.models import (
    EnumMediaCapability,
    EnumMediaCapabilityRefusal,
    ModelMediaBackend,
    ModelMediaCapabilityRequest,
    ModelMediaCapabilityResolution,
)

_KNOWN = {capability.value for capability in EnumMediaCapability}


def _refuse(
    request: ModelMediaCapabilityRequest,
    refusal: EnumMediaCapabilityRefusal,
    named: tuple[str, ...],
    detail: str,
) -> ModelMediaCapabilityResolution:
    return ModelMediaCapabilityResolution(
        capability=request.capability,
        status="refused",
        refusal=refusal,
        named_backends=named,
        detail=detail,
    )


def _shape_disagrees(backend: ModelMediaBackend) -> bool:
    if backend.capability not in _KNOWN:
        return False
    expected = EnumMediaCapability(backend.capability).endpoint_path
    return urlsplit(backend.endpoint_url).path != expected


class HandlerMediaCapabilityResolve:
    """Judge injected media backends and return the one that serves a capability."""

    def handle(
        self, request: ModelMediaCapabilityRequest
    ) -> ModelMediaCapabilityResolution:
        if request.capability not in _KNOWN:
            return _refuse(
                request,
                EnumMediaCapabilityRefusal.UNKNOWN_CAPABILITY,
                (),
                f"{request.capability!r} is not one of {sorted(_KNOWN)}",
            )
        wanted = EnumMediaCapability(request.capability)
        mismatched = tuple(
            backend.backend_id
            for backend in request.backends
            if _shape_disagrees(backend)
        )
        if mismatched:
            return _refuse(
                request,
                EnumMediaCapabilityRefusal.SHAPE_DISAGREES,
                mismatched,
                "a backend's endpoint path is not the one its capability is served at",
            )
        serving = tuple(
            backend
            for backend in request.backends
            if backend.capability == wanted.value and backend.serving
        )
        if len(serving) > 1:
            return _refuse(
                request,
                EnumMediaCapabilityRefusal.DUPLICATE_SERVING,
                tuple(backend.backend_id for backend in serving),
                "a node asks for a capability, so exactly one backend serves it",
            )
        if not serving:
            declared = tuple(
                backend.backend_id
                for backend in request.backends
                if backend.capability == wanted.value
            )
            return _refuse(
                request,
                EnumMediaCapabilityRefusal.NONE_SERVING,
                declared,
                "declared but not serving" if declared else "no backend is declared",
            )
        backend = serving[0]
        return ModelMediaCapabilityResolution(
            capability=wanted.value,
            status="resolved",
            backend_id=backend.backend_id,
            endpoint_url=backend.endpoint_url,
            served_model_id=backend.served_model_id,
            named_backends=(backend.backend_id,),
        )
