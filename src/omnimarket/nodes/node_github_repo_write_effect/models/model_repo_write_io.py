# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compatibility exports for shared repo write io models.

``ModelRepoWriteFailed`` carries ``terminal_failure_cause``, which the runtime
publishes on the contract's declared failure terminal.
"""

from omnimarket.models.model_repo_write_io import (
    MAX_BODY_CHARS,
    MAX_DISPATCH_INPUTS,
    EnumRepoWriteMode,
    EnumRepoWriteOperation,
    EnumRepoWriteRefusal,
    ModelCheckRunOutput,
    ModelRepoWriteClaim,
    ModelRepoWriteCompleted,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
    parse_claim_stamp,
)

__all__ = [
    "MAX_BODY_CHARS",
    "MAX_DISPATCH_INPUTS",
    "EnumRepoWriteMode",
    "EnumRepoWriteOperation",
    "EnumRepoWriteRefusal",
    "ModelCheckRunOutput",
    "ModelRepoWriteClaim",
    "ModelRepoWriteCompleted",
    "ModelRepoWriteFailed",
    "ModelRepoWriteRequest",
    "parse_claim_stamp",
]
