# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pipeline-touching PR classification reasons (OMN-9577)."""

from enum import StrEnum


class EnumPipelineTouchReason(StrEnum):
    """Why a PR was (or was not) classified as pipeline-touching.

    Reasons are ordered by evaluation priority per plan OMN-7621 line 203:
    file paths first, then ticket labels, then contract declaration.
    """

    FILE_PATH_MATCH = "file_path_match"
    TICKET_LABEL_MATCH = "ticket_label_match"
    CONTRACT_DECLARATION = "contract_declaration"
    NONE = "none"
