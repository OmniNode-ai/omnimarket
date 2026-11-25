# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The settings-change trigger of the delegation check regression run.

One settings audit row (who set which setting of which overlay, the value before and after) is one settings-store change.
The row's setting path is slash-joined, the form the key-to-check rules match, so the row becomes one request of change
kind settings_store and runs through HandlerCheckRegression. The lane the checks run against is the settings'
``trigger_target``, never a constant here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from omnimarket.nodes.node_delegation_check_regression_effect.handlers.handler_check_regression import (
    HandlerCheckRegression,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_check_regression import (
    EnumChangeKind,
    ModelCheckRegressionRequest,
    ModelCheckRegressionResult,
)
from omnimarket.nodes.node_delegation_check_regression_effect.overlay import (
    ModelCheckRegressionSettings,
    load_settings,
)
from omnimarket.nodes.node_delegation_check_regression_effect.protocols import (
    ProtocolAlarmDelivery,
    ProtocolCheckHistory,
    ProtocolCheckRunner,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_audit import (
    ModelOverlaySettingAudit,
)


class _NoCheckRunner:
    def run(self, check_id: str, *, since: datetime, timeout_seconds: int) -> None:
        raise RuntimeError(
            f"no check runner is configured: check {check_id} was not run, and an unread check is never read as green"
        )


class _NoCheckHistory:
    def previous(self, check_id: str, *, before: datetime) -> None:
        raise RuntimeError(
            f"no check history is configured: check {check_id} has no previous reading to compare with"
        )


class HandlerCheckRegressionOnSettingSet:
    """Built by the runtime from this class, so every constructor parameter is optional.

    The settings default to the node's tested defaults; a runner or history the deployment did not supply raises on use
    rather than reading every check unread.
    """

    def __init__(
        self,
        *,
        settings: ModelCheckRegressionSettings | None = None,
        runner: ProtocolCheckRunner | None = None,
        history: ProtocolCheckHistory | None = None,
        delivery: ProtocolAlarmDelivery | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings if settings is not None else load_settings()
        self._check_regression = HandlerCheckRegression(
            settings=self._settings,
            runner=runner if runner is not None else _NoCheckRunner(),
            history=history if history is not None else _NoCheckHistory(),
            delivery=delivery,
            clock=clock,
        )

    def handle(self, request: ModelOverlaySettingAudit) -> ModelCheckRegressionResult:
        return self._check_regression.handle(
            ModelCheckRegressionRequest(
                correlation_id=f"check-regression:{request.overlay_id}:{request.document_digest}",
                change_kind=EnumChangeKind.SETTINGS_STORE,
                subject=request.overlay_id,
                changed_keys=(request.setting_path,),
                applied_at=request.set_at,
                actor=request.actor,
                target=self._settings.trigger_target,
            )
        )


__all__ = ["HandlerCheckRegressionOnSettingSet"]
