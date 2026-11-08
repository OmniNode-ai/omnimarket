# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""python -m omnimarket.nodes.node_job_log_filler_effect run [--profile NAME]."""

from omnimarket.nodes.node_job_log_filler_effect.handlers.adapter_job_log_filler_cli import (
    job_log_fill_group,
)

if __name__ == "__main__":
    job_log_fill_group()
