# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared compaction command, reply and receipt models (OMN-21113 TA.11, OMN-21112 TA.10)."""

from pydantic import BaseModel

from omnimarket.models.context_compaction.enum_context_compaction_failure_class import (
    EnumContextCompactionFailureClass,
)
from omnimarket.models.context_compaction.enum_context_compaction_trigger import (
    EnumContextCompactionTrigger,
)
from omnimarket.models.context_compaction.enum_context_install_uncertain_reason import (
    EnumContextInstallUncertainReason,
)
from omnimarket.models.context_compaction.model_context_compaction_activated import (
    ModelContextCompactionActivated,
)
from omnimarket.models.context_compaction.model_context_compaction_build_outcome import (
    ModelContextCompactionBuildOutcome,
    ModelContextCompactionBuiltPackage,
)
from omnimarket.models.context_compaction.model_context_compaction_fallback import (
    ModelContextCompactionFallback,
)
from omnimarket.models.context_compaction.model_context_compaction_install_mismatch import (
    ModelContextCompactionInstallMismatch,
)
from omnimarket.models.context_compaction.model_context_compaction_install_uncertain import (
    ModelContextCompactionInstallUncertain,
)
from omnimarket.models.context_compaction.model_context_compaction_installed import (
    ModelContextCompactionInstalled,
)
from omnimarket.models.context_compaction.model_context_compaction_package_reply import (
    ModelContextCompactionPackageReply,
    installed_messages_hash,
    installed_messages_json,
)
from omnimarket.models.context_compaction.model_context_compaction_prepared import (
    ModelContextCompactionPrepared,
)
from omnimarket.models.context_compaction.model_context_compaction_requested import (
    ModelContextCompactionRequested,
)
from omnimarket.models.context_compaction.model_context_compaction_result import (
    ModelContextCompactionResult,
)

COMPACTION_RECEIPT_MODELS: tuple[type[BaseModel], ...] = (
    ModelContextCompactionPrepared,
    ModelContextCompactionActivated,
    ModelContextCompactionFallback,
    ModelContextCompactionInstalled,
    ModelContextCompactionInstallMismatch,
    ModelContextCompactionInstallUncertain,
)

__all__ = [
    "COMPACTION_RECEIPT_MODELS",
    "EnumContextCompactionFailureClass",
    "EnumContextCompactionTrigger",
    "EnumContextInstallUncertainReason",
    "ModelContextCompactionActivated",
    "ModelContextCompactionBuildOutcome",
    "ModelContextCompactionBuiltPackage",
    "ModelContextCompactionFallback",
    "ModelContextCompactionInstallMismatch",
    "ModelContextCompactionInstallUncertain",
    "ModelContextCompactionInstalled",
    "ModelContextCompactionPackageReply",
    "ModelContextCompactionPrepared",
    "ModelContextCompactionRequested",
    "ModelContextCompactionResult",
    "installed_messages_hash",
    "installed_messages_json",
]
