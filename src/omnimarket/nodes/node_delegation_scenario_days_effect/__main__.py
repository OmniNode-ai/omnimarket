# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the scenarios' green days once (``run``) or manage the node's hourly timer (``schedule install|uninstall``)."""

from omnimarket.nodes.node_delegation_scenario_days_effect.runtime_scenario_days import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
