# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for node_dev_roll_failure_capture_effect overlay (OMN-24013)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_dev_roll_failure_capture_effect.overlay import (
    ModelDevRollCaptureSettings,
    load_settings,
)

pytestmark = pytest.mark.unit


class TestOverlay:
    def test_absent_overlay_refuses(self) -> None:
        from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError

        with pytest.raises(NodeOverlayNotFoundError):
            load_settings(environ={})

    def test_present_overlay_loads(self) -> None:
        result = load_settings(
            environ={
                "ONEX_NODE_DEV_ROLL_FAILURE_CAPTURE_EFFECT_OVERLAY": '{"artifact_root": "/tmp/a", "retention_days": 7, "log_byte_cap": 1024}'
            }
        )
        assert result.artifact_root == "/tmp/a"
        assert result.retention_days == 7
        assert result.log_byte_cap == 1024

    def test_extra_key_refuses(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            load_settings(
                environ={
                    "ONEX_NODE_DEV_ROLL_FAILURE_CAPTURE_EFFECT_OVERLAY": '{"artifact_root": "/tmp/a", "retention_days": 7, "log_byte_cap": 1024, "extra": 1}'
                }
            )
