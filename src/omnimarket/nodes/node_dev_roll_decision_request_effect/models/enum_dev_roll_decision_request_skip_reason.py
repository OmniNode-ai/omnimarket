# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a queued dev roll did not yield a decision request."""

from enum import StrEnum


class EnumDevRollDecisionRequestSkipReason(StrEnum):
    """One typed reason per skipped manifest, in the order the handler checks them."""

    JOB_ID_MISSING = "job_id_missing"
    ISSUER_NOT_A_CI_REQUEST = "issuer_not_a_ci_request"
    GIT_REF_NOT_A_SHA = "git_ref_not_a_sha"
    TIME_MISSING = "time_missing"
    ALREADY_REQUESTED = "already_requested"
    NODE_UNAVAILABLE = "node_unavailable"
