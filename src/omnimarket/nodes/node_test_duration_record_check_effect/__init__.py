# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Test duration record check effect node (OMN-23560)."""

from .handlers.handler_test_duration_record_check import (
    HandlerTestDurationRecordCheck,
)


class NodeTestDurationRecordCheckEffect(HandlerTestDurationRecordCheck):
    """ONEX entrypoint for the test duration record check."""


__all__ = [
    "HandlerTestDurationRecordCheck",
    "NodeTestDurationRecordCheckEffect",
]
