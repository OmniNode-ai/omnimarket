# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Repo-local import seam for the concrete v2 delegation terminal classes.

OMN-17802. The three classes are owned by ``omnibase_core`` (OMN-17841, core
PR #1653) and are consumed here exactly as ``model_delegation_result`` consumes
the v1 pair: re-exported through one node-local module so every construction
site and every dispatcher class-to-topic map names the same import path, and a
core-side relocation is a one-line change here rather than a sweep.

No subclassing, no widening and no local shape: these are the released wire
contracts verbatim. The v1 classes are untouched and nothing upcasts between the
two families.
"""

from omnibase_core.models.delegation.wire.model_delegation_terminal_v2 import (
    ModelDelegationProviderFailureCause,
    ModelDelegationQualityGateRejection,
    ModelDelegationTerminalCompletedV2,
    ModelDelegationTerminalFailedRoutedV2,
    ModelDelegationTerminalFailedUnroutedV2,
    ModelQualityBarEvaluation,
)

__all__: list[str] = [
    "ModelDelegationProviderFailureCause",
    "ModelDelegationQualityGateRejection",
    "ModelDelegationTerminalCompletedV2",
    "ModelDelegationTerminalFailedRoutedV2",
    "ModelDelegationTerminalFailedUnroutedV2",
    "ModelQualityBarEvaluation",
]
