# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Finding constructor shared by the three projection contract rules."""

from typing import Final, Literal

from omnibase_core.models.validation.model_validation_finding import (
    ModelValidationFinding,
)

VALIDATOR_ID: Final[str] = "projection-contract-check"

RULE_ACCESS: Final[str] = "projection-contract-access"
RULE_DLQ: Final[str] = "projection-dlq-path"
RULE_CURSOR_MISSING: Final[str] = "projection-cursor-declared"
RULE_CURSOR_MEMBERSHIP: Final[str] = "projection-cursor-membership"
RULE_CURSOR_SHRINKABLE: Final[str] = "projection-cursor-baseline-shrinkable"
RULE_UNPARSEABLE: Final[str] = "projection-contract-unparseable"
RULE_ZERO_SCAN: Final[str] = "projection-contract-zero-scan"


def make_finding(
    *,
    rule_id: str,
    message: str,
    location: str | None,
    severity: Literal["WARN", "FAIL", "ERROR"] = "FAIL",
) -> ModelValidationFinding:
    return ModelValidationFinding(
        validator_id=VALIDATOR_ID,
        severity=severity,
        location=location,
        message=message,
        rule_id=rule_id,
    )
