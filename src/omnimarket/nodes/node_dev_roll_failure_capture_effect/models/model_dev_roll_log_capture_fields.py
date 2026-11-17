# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The producer's log capture fields."""

LOG_CAPTURE_FIELDS: tuple[str, ...] = (
    "service",
    "container",
    "bytes_total",
    "bytes_kept",
    "complete",
    "redacted",
    "sha256",
    "artifact_path",
    "error",
    "log",
)
