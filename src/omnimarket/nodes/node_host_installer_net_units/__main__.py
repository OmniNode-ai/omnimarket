# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_installer_net_units < request.json``: run one request, print the result."""

import sys

from omnimarket.nodes.node_host_installer_net_units.models.model_net_units import (
    ModelNetUnitsRequest,
)
from omnimarket.nodes.node_host_installer_net_units.node import (
    NodeHostInstallerNetUnits,
)


def main() -> int:
    request = ModelNetUnitsRequest.model_validate_json(sys.stdin.read())
    result = NodeHostInstallerNetUnits().handle(request)
    sys.stdout.write(result.model_dump_json(indent=1) + "\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
