# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the read-only principal command line."""

from __future__ import annotations

from .runtime_read_only_principal import main

if __name__ == "__main__":
    raise SystemExit(main())
