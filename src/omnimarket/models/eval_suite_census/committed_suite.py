# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the committed code eval suite document packaged in the wheel."""

from importlib.resources import files

from omnimarket.models.eval_suite_census.model_suite_census_request import (
    ModelCodeEvalSuiteCensusRequest,
)

_RESOURCE_FILE = "code_eval_suite.v1.json"


def load_committed_suite() -> ModelCodeEvalSuiteCensusRequest:
    """Parse the packaged suite document into the census request model."""
    return ModelCodeEvalSuiteCensusRequest.model_validate_json(
        files("omnimarket")
        .joinpath("configs", _RESOURCE_FILE)
        .read_text(encoding="utf-8")
    )


__all__ = [
    "load_committed_suite",
]
