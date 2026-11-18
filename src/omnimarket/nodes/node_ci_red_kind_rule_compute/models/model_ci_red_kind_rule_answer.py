# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.nodes.node_ci_red_kind_rule_compute.models.enum_ci_red_kind_rule import (
    EnumCiRedKindRuleRefusal,
)


class ModelCiRedKindRuleAnswer(BaseModel):
    """The rule table's decision for one failed check's name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    matched: bool
    kind: str | None = None
    rule_index: int | None = None
    model_call_required: bool
    refusal: EnumCiRedKindRuleRefusal | None = None
    detail: str = ""

    @model_validator(mode="after")
    def _validate_shape(self) -> ModelCiRedKindRuleAnswer:
        if self.ok:
            if self.refusal is not None:
                msg = "ok answer must not carry a refusal"
                raise ValueError(msg)
            if self.matched:
                if self.kind is None or self.rule_index is None:
                    msg = "matched answer must carry kind and rule_index"
                    raise ValueError(msg)
                if self.model_call_required:
                    msg = "matched answer must not require a model call"
                    raise ValueError(msg)
            else:
                if self.kind is not None or self.rule_index is not None:
                    msg = "unmatched answer must not carry kind or rule_index"
                    raise ValueError(msg)
                if not self.model_call_required:
                    msg = "unmatched answer must require a model call"
                    raise ValueError(msg)
        else:
            if self.matched:
                msg = "refusal answer must not be matched"
                raise ValueError(msg)
            if self.kind is not None or self.rule_index is not None:
                msg = "refusal answer must not carry kind or rule_index"
                raise ValueError(msg)
            if self.model_call_required:
                msg = "refusal answer must not require a model call"
                raise ValueError(msg)
            if self.refusal is None:
                msg = "refusal answer must carry a refusal"
                raise ValueError(msg)
        return self


__all__: list[str] = ["ModelCiRedKindRuleAnswer"]
