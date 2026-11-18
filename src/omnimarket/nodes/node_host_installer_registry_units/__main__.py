# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_installer_registry_units < request.json``: run one request, print the result."""

import sys

from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)
from omnimarket.nodes.node_host_installer_registry_units.runtime_registry_units_cli import (
    run_request,
)


def main() -> int:
    request = ModelRegistryUnitsRequest.model_validate_json(sys.stdin.read())
    result = run_request(request)
    sys.stdout.write(result.model_dump_json(indent=1) + "\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
