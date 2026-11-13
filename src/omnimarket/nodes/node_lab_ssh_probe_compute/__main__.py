# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_lab_ssh_probe_compute``: one JSON request on stdin, one JSON script on stdout."""

from __future__ import annotations

import sys

from omnimarket.nodes.node_lab_ssh_probe_compute.handlers import (
    HandlerLabSshProbe,
)
from omnimarket.nodes.node_lab_ssh_probe_compute.models import (
    ModelLabSshProbeRequest,
)


def main() -> int:
    request = ModelLabSshProbeRequest.model_validate_json(sys.stdin.read() or "{}")
    sys.stdout.write(HandlerLabSshProbe().handle(request).model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
