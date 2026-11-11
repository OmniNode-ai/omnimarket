# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_unit_tree_install_effect < request.json``: run one request, print the result."""

import sys

from omnimarket.nodes.node_unit_tree_install_effect.models.model_unit_tree_install import (
    ModelUnitTreeInstallRequest,
)
from omnimarket.nodes.node_unit_tree_install_effect.node import (
    NodeUnitTreeInstallEffect,
)


def main() -> int:
    request = ModelUnitTreeInstallRequest.model_validate_json(sys.stdin.read())
    result = NodeUnitTreeInstallEffect().handle(request)
    sys.stdout.write(result.model_dump_json(indent=1) + "\n")
    return 0 if result.verdict in ("pass", "dry-run") else 1


if __name__ == "__main__":
    raise SystemExit(main())
