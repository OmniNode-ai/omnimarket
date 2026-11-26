# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the install-silence alert effect (OMN-26658)."""

from .model_hook_install_silence import (
    EnumSilenceOutcome,
    ModelHookInstallSilenceResult,
    ModelHookInstallSilenceSettings,
    ModelHookInstallSilenceTrigger,
    ModelOwnedInstall,
)

__all__ = [
    "EnumSilenceOutcome",
    "ModelHookInstallSilenceResult",
    "ModelHookInstallSilenceSettings",
    "ModelHookInstallSilenceTrigger",
    "ModelOwnedInstall",
]
