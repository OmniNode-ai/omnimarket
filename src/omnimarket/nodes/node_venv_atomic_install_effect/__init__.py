# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install only the packages whose locked version moved, in place, with a journal."""

from pathlib import Path

from .handlers.handler_venv_atomic_install import HandlerVenvAtomicInstall
from .models.model_venv_atomic_install import ModelVenvAtomicInstallRequest


class NodeVenvAtomicInstallEffect(HandlerVenvAtomicInstall):
    """ONEX entry point for the local handler."""

    contract_path = Path(__file__).with_name("contract.yaml")


__all__ = [
    "HandlerVenvAtomicInstall",
    "ModelVenvAtomicInstallRequest",
    "NodeVenvAtomicInstallEffect",
]
