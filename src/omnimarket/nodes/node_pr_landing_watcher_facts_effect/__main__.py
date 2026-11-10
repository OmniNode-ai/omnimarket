# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""python -m omnimarket.nodes.node_pr_landing_watcher_facts_effect: one pass on the file backend (OMN-23507)."""

from __future__ import annotations

import sys

from omnimarket.nodes.node_pr_landing_watcher_facts_effect.runtime_watcher_facts import (
    main,
)

raise SystemExit(main(sys.argv[1:]))
