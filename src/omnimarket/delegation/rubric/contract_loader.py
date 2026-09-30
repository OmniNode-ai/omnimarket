# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""I/O boundary: resolve packaged or caller-provided YAML before computing."""

from importlib.resources import files
from pathlib import Path

import yaml

from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_delegation_class_rubrics import (
    ModelDelegationClassRubrics,
)


def load_delegation_class_rubrics(
    path: Path | None = None,
) -> ModelDelegationClassRubrics:
    text = (
        files("omnimarket")
        .joinpath("configs/delegation_class_rubrics.v1.yaml")
        .read_text(encoding="utf-8")
        if path is None
        else path.read_text(encoding="utf-8")
    )
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError("rubric contract must be a mapping")
    # "x-" keys hold YAML anchors shared between classes; they are not rubric fields.
    return ModelDelegationClassRubrics.model_validate(
        {key: value for key, value in raw.items() if not str(key).startswith("x-")}
    )
