# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canary monitoring-gate compute node (OMN-14735, B10).

Pure COMPUTE. Classifies observed monitoring signal readings (auth, TLS,
broker, lag, RDS) against threshold specs and reports a PASS/WARN/ABORT/
BLOCKED_PENDING_A6 verdict. Scaffold only: the numeric thresholds are a
required contractor input (A6) not yet delivered — every threshold spec
built from real inputs today is unresolved by construction, so this node
reports BLOCKED_PENDING_A6 rather than fabricating a PASS.
"""

from omnimarket.nodes.node_canary_monitoring_gate_compute.handlers.handler_canary_monitoring_gate import (
    HandlerCanaryMonitoringGate,
    evaluate_monitoring_gate,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_monitoring_gate_input import (
    ModelMonitoringGateInput,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_monitoring_gate_result import (
    DEFAULT_ABORT_ACTION,
    ModelMonitoringGateResult,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_signal_reading import (
    ModelSignalReading,
    SignalName,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_signal_verdict import (
    ModelSignalVerdict,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_threshold_spec import (
    UNRESOLVED_SOURCE,
    ModelThresholdSpec,
)


class NodeCanaryMonitoringGateCompute(HandlerCanaryMonitoringGate):
    """ONEX entry-point wrapper for HandlerCanaryMonitoringGate (OMN-14735)."""


__all__ = [
    "DEFAULT_ABORT_ACTION",
    "UNRESOLVED_SOURCE",
    "HandlerCanaryMonitoringGate",
    "ModelMonitoringGateInput",
    "ModelMonitoringGateResult",
    "ModelSignalReading",
    "ModelSignalVerdict",
    "ModelThresholdSpec",
    "NodeCanaryMonitoringGateCompute",
    "SignalName",
    "evaluate_monitoring_gate",
]
