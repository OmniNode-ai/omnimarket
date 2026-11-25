# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The settings-change trigger of the delegation check regression run.

One settings audit row (who set which setting of which overlay, the value before and after) is one settings-store change.
The row's setting path is slash-joined, the form the key-to-check rules match, so the row becomes one request of change
kind settings_store and runs through HandlerCheckRegression. The lane the checks run against is the settings'
``trigger_target``, never a constant here.
"""

from __future__ import annotations

from omnimarket.nodes.node_delegation_check_regression_effect.handlers.handler_check_regression import (
    HandlerCheckRegression,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_check_regression import (
    EnumChangeKind,
    ModelCheckRegressionRequest,
    ModelCheckRegressionResult,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_audit import (
    ModelOverlaySettingAudit,
)


class HandlerCheckRegressionOnSettingSet(HandlerCheckRegression):
    def handle(  # type: ignore[override]
        self, request: ModelOverlaySettingAudit
    ) -> ModelCheckRegressionResult:
        return super().handle(
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
