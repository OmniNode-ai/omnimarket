# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the hourly exposure reading, a probe, or the schedule verbs on this host."""

from omnimarket.nodes.node_runner_group_exposure_effect.runtime_exposure_probe import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
