# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the host job claim read: ``python -m omnimarket.nodes.node_host_job_claim_compute``."""

from omnimarket.nodes.node_host_job_claim_compute.runtime_host_job_claim import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
