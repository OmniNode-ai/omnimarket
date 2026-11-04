# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Packaged models for node_lab_placement_compute."""

from .model_lab_lane_priority import (
    PRIORITY_OTHER,
    ModelLanePriorityOverlay,
    ModelLanePriorityRequest,
    ModelLanePriorityResult,
    ModelParityRules,
    ModelPluginParityRequest,
    ModelPluginParityResult,
    ModelPriorityRules,
)
from .model_lab_placement import (
    CAPABILITY_COLUMNS,
    ModelHostDefaults,
    ModelHostTableVerdict,
    ModelLabPlacementRequest,
    ModelLabPlacementResult,
    ModelPlacementRules,
    ModelUnitProfile,
)
from .model_lab_pool_place import (
    DEFAULT_MAX_LOAD_PER_CORE,
    DEFAULT_MIN_FREE_BYTES,
    LAB_WORK_WEIGHT_ENV,
    EnumPlacementDecision,
    EnumRefusalReason,
    ModelHostVerdict,
    ModelLabPoolPlaceRequest,
    ModelPlacement,
)

__all__ = [
    "CAPABILITY_COLUMNS",
    "DEFAULT_MAX_LOAD_PER_CORE",
    "DEFAULT_MIN_FREE_BYTES",
    "LAB_WORK_WEIGHT_ENV",
    "PRIORITY_OTHER",
    "EnumPlacementDecision",
    "EnumRefusalReason",
    "ModelHostDefaults",
    "ModelHostTableVerdict",
    "ModelHostVerdict",
    "ModelLabPlacementRequest",
    "ModelLabPlacementResult",
    "ModelLabPoolPlaceRequest",
    "ModelLanePriorityOverlay",
    "ModelLanePriorityRequest",
    "ModelLanePriorityResult",
    "ModelParityRules",
    "ModelPlacement",
    "ModelPlacementRules",
    "ModelPluginParityRequest",
    "ModelPluginParityResult",
    "ModelPriorityRules",
    "ModelUnitProfile",
]
