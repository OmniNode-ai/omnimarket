# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why one dev runtime start happened."""

from enum import StrEnum


class EnumDevStartCause(StrEnum):
    """The cause the deploy agent declares at each dev runtime start."""

    ROLL_RECREATE = "roll_recreate"
    BRANCH_INSTALL_REAPPLY = "branch_install_reapply"
    OTHER = "other"
