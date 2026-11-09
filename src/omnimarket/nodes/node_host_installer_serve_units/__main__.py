# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_installer_serve_units < request.json``: run one request, print the result."""

import sys

from omnimarket.nodes.node_host_installer_serve_units.models.model_serve_units import (
    ModelServeUnitsRequest,
)
from omnimarket.nodes.node_host_installer_serve_units.node import (
    NodeHostInstallerServeUnits,
)


def main() -> int:
    request = ModelServeUnitsRequest.model_validate_json(sys.stdin.read())
    result = NodeHostInstallerServeUnits().handle(request)
    sys.stdout.write(result.model_dump_json(indent=1) + "\n")
    sys.stderr.write(result.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
