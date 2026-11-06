# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Server-owned overlay and factory bindings for the delivery effect."""

import importlib
import os
from pathlib import Path
from typing import cast

import yaml

from omnimarket.models.error_rule_delivery import (
    ModelDeliveryBinding,
    ModelDeliveryFailure,
    ModelDeliveryMatch,
    ModelDeliveryOverlay,
)
from omnimarket.nodes.node_exception_queue_effect import create_handler_exception_emit
from omnimarket.nodes.node_silent_failure_alarm_effect import HandlerDeliveryHookAlarm
from omnimarket.protocols.protocol_error_rule_delivery import (
    ProtocolDeliveryMatcher,
)

from ..handlers.command_delivery_path import CommandDeliveryPath
from ..handlers.handler_delivery_hook import HandlerDeliveryHook


class UnreadMatcher:
    """Missing matcher/table bindings are a loud result, never a no-match."""

    def __init__(self, detail: str) -> None:
        self.detail = detail

    def handle(self, request: ModelDeliveryFailure) -> ModelDeliveryMatch:
        return ModelDeliveryMatch(status="UNREAD", detail=self.detail)


def _load(binding: ModelDeliveryBinding) -> object:
    factory = getattr(importlib.import_module(binding.module), binding.factory)
    return factory()


def create_handler_delivery_hook() -> HandlerDeliveryHook:
    pointer = os.environ.get("ONEX_ERROR_RULE_DELIVERY_OVERLAY")
    if not pointer:
        raise ValueError("UNREAD: ONEX_ERROR_RULE_DELIVERY_OVERLAY is not bound")
    overlay = ModelDeliveryOverlay.model_validate(
        yaml.safe_load(Path(pointer).read_text())
    )
    try:
        matcher = (
            cast(ProtocolDeliveryMatcher, _load(overlay.matcher))
            if overlay.matcher
            else UnreadMatcher("matcher/table overlay is absent")
        )
    except (ImportError, AttributeError, OSError, ValueError) as exc:
        matcher = UnreadMatcher(f"matcher/table binding unread: {exc}")
    paths = {
        surface: CommandDeliveryPath(binding)
        for surface, binding in overlay.delivery_paths.items()
    }
    return HandlerDeliveryHook(
        overlay,
        matcher=matcher,
        alarm=HandlerDeliveryHookAlarm(),
        queue=create_handler_exception_emit(),
        paths=paths,
    )
