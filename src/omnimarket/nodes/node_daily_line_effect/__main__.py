# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the daily line once (``daily``) or manage its 05:30 Eastern schedule (``schedule install|uninstall``)."""

from omnimarket.nodes.node_daily_line_effect.runtime_daily_line import main

if __name__ == "__main__":
    raise SystemExit(main())
