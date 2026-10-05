# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule ``dlq``: a projection handler that can raise ValidationError needs a DLQ route.

Ported from ``scripts/ci/check_projection_dlq_path.py`` (OMN-13548, D-03). A
``ValidationError`` on a malformed inbound event must not be logged and dropped;
it must reach the contract-declared DLQ topic. A module that references
``ValidationError`` without any canonical DLQ surface symbol is flagged. A
handler that legitimately propagates the error carries the existing
``dlq-path-not-required`` marker; no new marker is introduced.
"""

from collections.abc import Sequence

from omnibase_core.models.nodes.no_utcnow_check.model_source_file import ModelSourceFile
from omnibase_core.models.validation.model_validation_finding import (
    ModelValidationFinding,
)

from omnimarket.nodes.node_contract_projection_check_compute.handlers.findings import (
    RULE_DLQ,
    make_finding,
)

_DLQ_ROUTE_MARKERS = ("route_to_dlq", "_route_malformed_to_dlq", "dlq_topics")
_VALIDATION_MARKER = "ValidationError"
_ALLOW_MARKER = "# dlq-path-not" + "-required:"


def check_dlq(handlers: Sequence[ModelSourceFile]) -> list[ModelValidationFinding]:
    findings: list[ModelValidationFinding] = []
    for handler in handlers:
        text = handler.source
        if _VALIDATION_MARKER not in text:
            continue
        if any(marker in text for marker in _DLQ_ROUTE_MARKERS):
            continue
        if _ALLOW_MARKER in text:
            continue
        findings.append(
            make_finding(
                rule_id=RULE_DLQ,
                message=(
                    f"{handler.path}: references {_VALIDATION_MARKER} but wires no DLQ route "
                    f"(expected one of {', '.join(_DLQ_ROUTE_MARKERS)} — see "
                    f"omnimarket.projection.dlq.route_to_dlq, OMN-13548)"
                ),
                location=handler.path,
            )
        )
    return findings
