# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_installer_overlay_env_probe < request.json``: print rows, never values."""

import sys

from omnimarket.nodes.node_host_installer_overlay_env_probe.models.model_overlay_env_probe import (
    ModelOverlayEnvProbeRequest,
)
from omnimarket.nodes.node_host_installer_overlay_env_probe.node import (
    NodeHostInstallerOverlayEnvProbe,
)


def main() -> int:
    request = ModelOverlayEnvProbeRequest.model_validate_json(sys.stdin.read())
    result = NodeHostInstallerOverlayEnvProbe().handle(request)
    sys.stdout.write(result.model_dump_json(indent=1) + "\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
