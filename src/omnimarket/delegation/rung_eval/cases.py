# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load packaged admission fixtures."""

from importlib.resources import files

from omnimarket.delegation.rung_eval.models import ModelRungEvalCase


def load_cases() -> list[ModelRungEvalCase]:
    fixtures = files("omnimarket.delegation.rung_eval").joinpath("fixtures")
    return sorted(
        (
            ModelRungEvalCase.model_validate_json(path.read_text(encoding="utf-8"))
            for path in fixtures.iterdir()
            if path.is_file() and path.name.endswith(".json")
        ),
        key=lambda case: case.case_id,
    )
