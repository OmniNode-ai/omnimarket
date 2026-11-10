# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scan the day once (``scan``, ``probe``) or manage its daily schedule (``schedule install|uninstall``)."""

from omnimarket.nodes.node_delegation_events_defect_scan_effect.runtime_delegation_events_defect_scan import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
