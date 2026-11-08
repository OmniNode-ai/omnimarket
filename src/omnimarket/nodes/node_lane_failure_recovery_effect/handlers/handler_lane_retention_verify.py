# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only post-install retention falsifier; empty/unreadable is unmeasured."""

import json
import re
from datetime import datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_lane_failure import (
    ModelLaneFailureReceipt,
    ModelLaneRetentionInstallRecord,
    ModelLaneRetentionVerifyOverlay,
    ModelLaneRetentionVerifyRequest,
    ModelLaneRetentionVerifyResult,
)


class HandlerLaneRetentionVerify:
    def __init__(self, overlay: ModelLaneRetentionVerifyOverlay | None = None) -> None:
        self._overlay = overlay

    @property
    def overlay(self) -> ModelLaneRetentionVerifyOverlay:
        if self._overlay is None:
            self._overlay = load_structured_overlay(
                "lane_retention_verify", ModelLaneRetentionVerifyOverlay
            )
        return self._overlay

    def handle(
        self, request: ModelLaneRetentionVerifyRequest
    ) -> ModelLaneRetentionVerifyResult:
        installed = ModelLaneRetentionInstallRecord.model_validate_json(
            Path(request.install_record).read_text()
        )
        if installed.installed_at.tzinfo is None or request.now.tzinfo is None:
            raise ValueError("install and observation times require a timezone")
        if request.check_after_seconds not in self.overlay.check_after_seconds:
            raise ValueError("requested checkpoint is not declared by the overlay")
        elapsed = (request.now - installed.installed_at).total_seconds()
        if elapsed < request.check_after_seconds:
            return ModelLaneRetentionVerifyResult(
                status="not-due", installed=installed, checked_receipts=0
            )
        root = Path(request.receipt_directory)
        if not root.is_dir():
            raise ValueError("receipt directory is unreadable")
        checked = 0
        violations, unreadable = [], []
        for path in sorted(root.glob(self.overlay.receipt_glob)):
            try:
                receipt = ModelLaneFailureReceipt.model_validate_json(path.read_text())
                started = datetime.fromisoformat(receipt.started_at)
                if started.tzinfo is None:
                    raise ValueError("receipt started_at requires a timezone")
                if not installed.installed_at < started <= request.now:
                    continue
                # The install record scopes a host; other hosts do not falsify its fix.
                if receipt.host != installed.host:
                    continue
                checked += 1
                if re.search(
                    self.overlay.error_pattern,
                    receipt.retention.error + "\n" + receipt.retention.reading,
                ):
                    violations.append(str(path))
            except (OSError, ValueError, json.JSONDecodeError):
                # Pydantic's ValidationError is a ValueError too. No silent zero.
                unreadable.append(str(path))
        return ModelLaneRetentionVerifyResult(
            status="failed"
            if violations
            else "unmeasured"
            if unreadable or not checked
            else "clear",
            installed=installed,
            checked_receipts=checked,
            violating_receipts=tuple(violations),
            unreadable_receipts=tuple(unreadable),
        )
