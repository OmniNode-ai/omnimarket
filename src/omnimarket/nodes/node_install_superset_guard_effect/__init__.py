# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verify that an install contains the live commits or declares each missing commit as an intentional drop."""

from pathlib import Path

from omnimarket.handlers.install_file_list import SupersetError

from .handlers.handler_install_superset import HandlerInstallSuperset
from .models.model_install_superset import ModelSupersetRequest


class NodeInstallSupersetGuardEffect(HandlerInstallSuperset):
    """ONEX entry point for the local handler."""

    contract_path = Path(__file__).with_name("contract.yaml")


__all__ = [
    "HandlerInstallSuperset",
    "ModelSupersetRequest",
    "NodeInstallSupersetGuardEffect",
    "SupersetError",
]
