# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for gate-or-range check classification and range evaluation.

Unified verification plan section 2b and row G1: every check declares its
class, gate or range, and a range carries its acceptance line and method.
"""

from omnimarket.models.ranges.enum_check_class import EnumCheckClass
from omnimarket.models.ranges.enum_comparison_verdict import EnumComparisonVerdict
from omnimarket.models.ranges.enum_incomplete_run_treatment import (
    EnumIncompleteRunTreatment,
)
from omnimarket.models.ranges.enum_range_sample_outcome import (
    EnumRangeSampleOutcome,
)
from omnimarket.models.ranges.enum_range_status import EnumRangeStatus
from omnimarket.models.ranges.enum_range_verdict import EnumRangeVerdict
from omnimarket.models.ranges.model_check_declaration import ModelCheckDeclaration
from omnimarket.models.ranges.model_check_register import ModelCheckRegister
from omnimarket.models.ranges.model_comparison_method import ModelComparisonMethod
from omnimarket.models.ranges.model_comparison_pair import ModelComparisonPair
from omnimarket.models.ranges.model_comparison_result import ModelComparisonResult
from omnimarket.models.ranges.model_range_acceptance_line import (
    ModelRangeAcceptanceLine,
)
from omnimarket.models.ranges.model_range_evaluation import ModelRangeEvaluation
from omnimarket.models.ranges.model_range_method import ModelRangeMethod
from omnimarket.models.ranges.model_range_run import ModelRangeRun
from omnimarket.models.ranges.model_range_sample import ModelRangeSample

__all__ = [
    "EnumCheckClass",
    "EnumComparisonVerdict",
    "EnumIncompleteRunTreatment",
    "EnumRangeSampleOutcome",
    "EnumRangeStatus",
    "EnumRangeVerdict",
    "ModelCheckDeclaration",
    "ModelCheckRegister",
    "ModelComparisonMethod",
    "ModelComparisonPair",
    "ModelComparisonResult",
    "ModelRangeAcceptanceLine",
    "ModelRangeEvaluation",
    "ModelRangeMethod",
    "ModelRangeRun",
    "ModelRangeSample",
]
