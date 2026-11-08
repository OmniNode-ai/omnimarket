# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# unimported-handler-allow: contract-wired by module path from contract.yaml handler_routing
"""Envelope-shaped handlers for runtime routing of the iLO node.

The real handlers (``HandlerIloRedfish``, ``IloSetupSequence``) take lab-private ports (overlay,
secret store, transports) that the shared runtime cannot inject, so the node is hosted in process
by the operator's CLI. These wrappers take only the container, resolve at boot, and refuse any
request that reaches them through a shared runtime instead of acting on it.
"""

from __future__ import annotations

from typing import Any

from omnimarket.models.ilo_redfish.enum_ilo_redfish import (
    EnumIloOutcome,
    EnumIloRefusal,
)
from omnimarket.models.ilo_redfish.model_ilo_redfish_request import (
    ModelIloRedfishRequest,
)
from omnimarket.models.ilo_redfish.model_ilo_redfish_result import (
    ModelIloRedfishResult,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_setup import (
    EnumIloSetupRefusal,
    ModelIloSetupHostResult,
    ModelIloSetupRequest,
    ModelIloSetupResult,
)

_DETAIL = (
    "in_process_only: this node is hosted by the operator CLI, not a shared runtime"
)


class HandlerIloRedfishEnvelope:
    """Runtime-routable face of the read and write verbs; always refuses."""

    def __init__(self, container: Any = None) -> None:
        self._container = container

    def handle(self, request: ModelIloRedfishRequest) -> ModelIloRedfishResult:
        return ModelIloRedfishResult(
            verb=request.verb,
            host=request.host,
            outcome=EnumIloOutcome.REFUSED,
            refusal=EnumIloRefusal.HOST_NOT_CONFIGURED,
            detail=_DETAIL,
        )


class HandlerIloSetupEnvelope:
    """Runtime-routable face of the setup verb; always refuses."""

    def __init__(self, container: Any = None) -> None:
        self._container = container

    def handle(self, request: ModelIloSetupRequest) -> ModelIloSetupResult:
        return ModelIloSetupResult(
            hosts=tuple(
                ModelIloSetupHostResult(
                    host=name,
                    status="refused",
                    refusal=EnumIloSetupRefusal.ENDPOINT_MISSING,
                    detail=_DETAIL,
                )
                for name in request.hosts
            )
        )
