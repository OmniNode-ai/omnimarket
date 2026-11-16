# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Entry point: ``python -m omnimarket.nodes.node_host_install_orchestrator``."""

from __future__ import annotations

from omnimarket.nodes.node_host_install_orchestrator.cli import host_install_group


def main() -> None:
    host_install_group()


if __name__ == "__main__":
    main()
