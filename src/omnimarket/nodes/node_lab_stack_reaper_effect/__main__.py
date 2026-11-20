# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the lab stack reaper command line as a module (python -m ...)."""

from __future__ import annotations

from omnimarket.nodes.node_lab_stack_reaper_effect.handlers.handler_lab_stack_reaper_cli import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
