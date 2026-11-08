# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Drain a host, or stop an idle runner, from the command line."""

from omnimarket.nodes.node_host_drain_effect.runtime_host_drain import main

if __name__ == "__main__":
    raise SystemExit(main())
