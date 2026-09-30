# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Criterion identity binds its required parameter schema."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_cited_lines_params import (
    ModelCitedLinesParams,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_claims_traceable_params import (
    ModelClaimsTraceableParams,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_code_parses_params import (
    ModelCodeParsesParams,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_declared_format_params import (
    ModelDeclaredFormatParams,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_id_coverage_params import (
    ModelIdCoverageParams,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_ids_traceable_params import (
    ModelIdsTraceableParams,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_named_symbols_params import (
    ModelNamedSymbolsParams,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_test_targets_params import (
    ModelTestTargetsParams,
)


class ModelRubricCriterion(BaseModel):
    """A single computable check declared by the rubric contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    criterion_id: str
    description: str = Field(min_length=1)
    params: (
        ModelCitedLinesParams
        | ModelNamedSymbolsParams
        | ModelTestTargetsParams
        | ModelDeclaredFormatParams
        | ModelClaimsTraceableParams
        | ModelIdCoverageParams
        | ModelCodeParsesParams
        | ModelIdsTraceableParams
    )

    @model_validator(mode="after")
    def validate_params(self) -> Self:
        schemas = {
            "cited_lines_exist": ModelCitedLinesParams,
            "named_symbols_exist": ModelNamedSymbolsParams,
            "named_test_passes": ModelTestTargetsParams,
            "stated_test_passes": ModelTestTargetsParams,
            "declared_format_met": ModelDeclaredFormatParams,
            "claims_traceable": ModelClaimsTraceableParams,
            "id_coverage": ModelIdCoverageParams,
            "code_parses": ModelCodeParsesParams,
            "ids_traceable": ModelIdsTraceableParams,
        }
        schema = schemas.get(self.criterion_id)
        if schema is None or not isinstance(self.params, schema):
            raise ValueError("criterion_id must match its parameter schema")
        return self
