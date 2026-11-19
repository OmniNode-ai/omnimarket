# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_installer_clone_sync_units < request.json``: run one request."""

import sys

from omnimarket.nodes.node_host_installer_clone_sync_units.models.model_clone_sync_units import (
    ModelCloneSyncUnitsRequest,
)
from omnimarket.nodes.node_host_installer_clone_sync_units.node import (
    NodeHostInstallerCloneSyncUnits,
)


def main() -> int:
    request = ModelCloneSyncUnitsRequest.model_validate_json(sys.stdin.read())
    result = NodeHostInstallerCloneSyncUnits().handle(request)
    sys.stdout.write(result.model_dump_json(indent=1) + "\n")
    sys.stderr.write(result.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
