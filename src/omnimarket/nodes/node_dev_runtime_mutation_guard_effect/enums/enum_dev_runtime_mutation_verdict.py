# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The guard's verdict for one Bash command (OMN-21795)."""

from enum import StrEnum


class EnumDevRuntimeMutationVerdict(StrEnum):
    """ALLOW lets the command run; REFUSE blocks it and names the deployment route and the grant."""

    ALLOW = "ALLOW"
    REFUSE = "REFUSE"
