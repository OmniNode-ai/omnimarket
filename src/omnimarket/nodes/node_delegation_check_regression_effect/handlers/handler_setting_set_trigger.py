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
)
from omnimarket.nodes.node_delegation_check_regression_effect.protocols import (
    ProtocolAlarmDelivery,
    ProtocolCheckHistory,
    ProtocolCheckRunner,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_audit import (
    ModelOverlaySettingAudit,
)


class HandlerCheckRegressionOnSettingSet:
    """Auto-wiring builds this with no arguments; the deployment supplies the settings, runner and history.

    A missing dependency is a RuntimeError when an audit row arrives, never a silent skip.
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
        self._settings = settings
        self._runner = runner
        self._history = history
        self._delivery = delivery
        self._clock = clock

    def handle(self, request: ModelOverlaySettingAudit) -> ModelCheckRegressionResult:
        settings, runner, history = self._settings, self._runner, self._history
        missing = [
            name
            for name, value in (
                ("settings", settings),
                ("runner", runner),
                ("history", history),
            )
            if value is None
        ]
        if settings is None or runner is None or history is None:
            raise RuntimeError(
                "HandlerCheckRegressionOnSettingSet was built without "
                f"{', '.join(missing)}; the deployment must supply them before a settings audit row can run the checks"
            )
        return HandlerCheckRegression(
            settings=settings,
            runner=runner,
            history=history,
            delivery=self._delivery,
            clock=self._clock,
        ).handle(
            ModelCheckRegressionRequest(
                correlation_id=f"check-regression:{request.overlay_id}:{request.document_digest}",
                change_kind=EnumChangeKind.SETTINGS_STORE,
                subject=request.overlay_id,
                changed_keys=(request.setting_path,),
                applied_at=request.set_at,
                actor=request.actor,
                target=settings.trigger_target,
            )
        )


__all__ = ["HandlerCheckRegressionOnSettingSet"]
