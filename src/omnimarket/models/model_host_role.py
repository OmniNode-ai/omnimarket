# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The role a lab host is declared with (OMN-24263): one baseline per role, differences only by declared role."""

from enum import StrEnum


class EnumHostRole(StrEnum):
    """What a host is for. Every host of a role is checked against the same baseline."""

    LAB_WORKER = "lab-worker"
    GPU_MODEL = "gpu-model"
    RUNTIME_HOST = "runtime-host"
    OPERATOR_MAC = "operator-mac"
