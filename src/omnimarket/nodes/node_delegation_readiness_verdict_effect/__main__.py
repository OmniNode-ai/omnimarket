# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the readiness verdict once (``run``) or manage the node's hourly timer (``schedule install|uninstall``)."""

from omnimarket.nodes.node_delegation_readiness_verdict_effect.runtime_verdict import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
