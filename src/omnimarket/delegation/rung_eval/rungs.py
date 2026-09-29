# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load environment-only rung configuration."""

from importlib.resources import files

import yaml
from pydantic import TypeAdapter

from omnimarket.delegation.rung_eval.models import ModelRungSpec


def load_rungs() -> list[ModelRungSpec]:
    config = files("omnimarket").joinpath("configs/delegation_rung_eval.v1.yaml")
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    return TypeAdapter(list[ModelRungSpec]).validate_python(raw["rungs"])
