# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local identity projection: its pure plan, its bus writer, and the store-neutral fold.

The store-neutral fold lives in ``omnimarket.models.model_local_identity_fold`` so the local
store and this projection fold through one seam without either importing the other.
"""

from omnimarket.models.model_local_identity_fold import (
    LOCAL_IDENTITY_KEY,
    LOCAL_IDENTITY_TABLE,
    apply_local_identity_minted,
    plan_local_identity_write,
)

from .handler_local_identity_writer import (
    HandlerLocalIdentityProjectionRunner,
    LocalIdentityProjectionWriter,
)
from .handler_projection_local_identity import HandlerProjectionLocalIdentity

__all__ = [
    "LOCAL_IDENTITY_KEY",
    "LOCAL_IDENTITY_TABLE",
    "HandlerLocalIdentityProjectionRunner",
    "HandlerProjectionLocalIdentity",
    "LocalIdentityProjectionWriter",
    "apply_local_identity_minted",
    "plan_local_identity_write",
]
