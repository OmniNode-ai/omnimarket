# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The machinery tier's policy, read from the node's own contract (OMN-22413).

The window, the recurrence floors, the tier order and the priority class are data of
``config.lab_fill_plan.machinery`` in contract.yaml; the handlers hold none of them.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from ..models.model_lab_fill_machinery import ModelLabFillMachineryPolicy

_CONTRACT = Path(__file__).parents[1] / "contract.yaml"


@lru_cache(maxsize=1)
def machinery_policy() -> ModelLabFillMachineryPolicy:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return ModelLabFillMachineryPolicy.model_validate(
        data["config"]["lab_fill_plan"]["machinery"]
    )
