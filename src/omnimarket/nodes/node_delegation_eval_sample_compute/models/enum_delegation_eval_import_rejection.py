# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Values for EnumDelegationEvalImportRejection."""

from enum import StrEnum


class EnumDelegationEvalImportRejection(StrEnum):
    HOLDOUT_BUCKET = "holdout_bucket"
    CUSTOMER_TENANT = "customer_tenant"
    UNKNOWN_KEY = "unknown_key"
