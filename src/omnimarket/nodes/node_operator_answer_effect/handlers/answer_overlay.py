# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load answer policy; all relation validation is performed by the core checker."""

from importlib.resources import files
from pathlib import Path

import yaml
from omnibase_core.models.contracts.subcontracts.model_validator_subcontract import (
    ModelValidatorSubcontract,
)
from omnibase_core.models.overlay_constraints.model_overlay_constraints_check_input import (
    ModelOverlayConstraintsCheckInput,
)
from omnibase_core.models.overlay_constraints.model_overlay_constraints_declaration import (
    ModelOverlayConstraintsDeclaration,
)
from omnibase_core.validation.checker_overlay_constraints import (
    check_overlay_constraints,
)

from omnimarket.models.operator_answer import ModelAnswerConfig

CONTRACT = Path(__file__).parents[1] / "contract.yaml"


def load_answer_config(overlay: Path) -> ModelAnswerConfig:
    contract = yaml.safe_load(CONTRACT.read_text())
    values = dict(contract["answer_policy"])
    values.update(yaml.safe_load(overlay.read_text())["answer_policy"])
    rules = yaml.safe_load(
        files("omnibase_core.validation")
        .joinpath("contracts/overlay_constraints.validation.yaml")
        .read_text()
    )["validation"]
    result = check_overlay_constraints(
        ModelOverlayConstraintsCheckInput(
            source_name=str(overlay),
            declaration=ModelOverlayConstraintsDeclaration.model_validate(
                contract["overlay_constraints"]
            ),
            values=values,
            contract=ModelValidatorSubcontract.model_validate(rules),
        )
    )
    if result.exit_code:
        raise ValueError("; ".join(issue.message for issue in result.issues))
    return ModelAnswerConfig.model_validate(values)
