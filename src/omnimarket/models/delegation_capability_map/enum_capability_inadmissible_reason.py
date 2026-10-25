# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a verdict row was kept out of every cell (OMN-20169)."""

from enum import StrEnum


class EnumCapabilityInadmissibleReason(StrEnum):
    """The reason one verdict row folds into no cell.

    no_configuration: the row names no configuration identity (AC5).
    identity_field_missing: one of the six identity fields is absent.
    no_serving_assignment: no serving assignment covers the row's model, backend and configuration.
    identity_mismatch: the row differs from its serving assignment on an identity field (AC3).
    """

    NO_CONFIGURATION = "no_configuration"
    IDENTITY_FIELD_MISSING = "identity_field_missing"
    NO_SERVING_ASSIGNMENT = "no_serving_assignment"
    IDENTITY_MISMATCH = "identity_mismatch"
