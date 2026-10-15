# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-supplied documentation draw: the answer text and the original function."""

import ast
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DOC_CHECK_FUNCTION = "retry_delay"


class ModelDocCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation: Literal["delegation_doc_check"] = "delegation_doc_check"
    answer_text: str = Field(min_length=1)
    reference_source: str = Field(min_length=1)
    run_directory: str = Field(min_length=1)

    @field_validator("reference_source")
    @classmethod
    def _reference_is_the_original_function(cls, value: str) -> str:
        """The reference is the walker's own original: one function, literal defaults."""
        try:
            body = ast.parse(value).body
        except SyntaxError as exc:
            raise ValueError(f"reference_source does not parse: {exc.msg}") from exc
        if len(body) != 1 or not isinstance(body[0], ast.FunctionDef):
            raise ValueError("reference_source must be exactly one function definition")
        function = body[0]
        if function.name != DOC_CHECK_FUNCTION:
            raise ValueError(f"reference_source must define {DOC_CHECK_FUNCTION}")
        if function.decorator_list:
            raise ValueError("reference_source function must not be decorated")
        if not all(
            isinstance(d, ast.Constant)
            and isinstance(d.value, int | float)
            and not isinstance(d.value, bool)
            for d in function.args.defaults
        ):
            raise ValueError("reference_source defaults must be numeric literals")
        return value
