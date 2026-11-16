# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the lab disk hygiene tick as a module (python -m ...)."""

from __future__ import annotations

import sys

from omnimarket.nodes.node_lab_disk_hygiene_effect.runtime_lab_disk_hygiene_tick import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
