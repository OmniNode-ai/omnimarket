# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the job-start source: read once, or probe without storing."""

from omnimarket.nodes.node_job_start_source_effect.runtime_job_start_source import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
