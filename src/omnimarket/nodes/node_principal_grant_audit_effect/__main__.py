# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the principal grant audit tick as a module (python -m ...)."""

from __future__ import annotations

import sys

from omnimarket.nodes.node_principal_grant_audit_effect.runtime_principal_grant_audit_tick import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
