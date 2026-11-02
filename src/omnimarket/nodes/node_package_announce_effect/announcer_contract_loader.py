# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Loads the announcer's contract: its announce lifecycle (OMN-21597)."""

from pathlib import Path

from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode
from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_core.utils.util_safe_yaml_loader import load_yaml_mapping_no_duplicates

from omnimarket.nodes.node_package_announce_effect.models.model_announcer_contract import (
    ModelAnnouncerContract,
)

__all__ = ["ANNOUNCER_CONTRACT_PATH", "load_announcer_contract"]

ANNOUNCER_CONTRACT_PATH = Path(__file__).with_name("contract.yaml")


def load_announcer_contract(
    path: Path = ANNOUNCER_CONTRACT_PATH,
) -> ModelAnnouncerContract:
    """Read the announce lifecycle from the contract file."""
    raw = load_yaml_mapping_no_duplicates(
        path.read_text(encoding="utf-8"), source=str(path)
    )
    lifecycle = raw.get("state_transitions")
    if not isinstance(lifecycle, dict):
        raise ModelOnexError(
            f"{path}: state_transitions block is missing",
            error_code=EnumCoreErrorCode.VALIDATION_ERROR,
        )
    return ModelAnnouncerContract(
        lifecycle=ModelFSMSubcontract.model_validate(lifecycle)
    )
