# SPDX-License-Identifier: MIT
"""Node-local dep-health coverage for handler_demo_drift_detector."""

from omnimarket.nodes.node_demo_drift_detector.handlers.handler_demo_drift_detector import (
    HandlerDemoDriftDetector,
)


def test_handler_demo_drift_detector_importable() -> None:
    assert HandlerDemoDriftDetector is not None
