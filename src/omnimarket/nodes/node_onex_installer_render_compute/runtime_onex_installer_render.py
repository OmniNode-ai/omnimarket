# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_onex_installer_render_compute.runtime_onex_installer_render`` (OMN-25070).

Reads one ``ModelOnexInstallerRequest`` as JSON on stdin (the release's pinned
values, packages/omninode-onex/installer.json), renders it through
``HandlerOnexInstallerRender`` and writes the installer to stdout. It opens no
file: the release step redirects stdin and stdout. Exit 0 when the script was
written; exit 2 when stdin is not a valid request, with the reason on stderr.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from pydantic import ValidationError

from omnimarket.nodes.node_onex_installer_render_compute.handlers.handler_onex_installer_render import (
    HandlerOnexInstallerRender,
)
from omnimarket.nodes.node_onex_installer_render_compute.models.model_onex_installer import (
    ModelOnexInstallerRequest,
)

EXIT_INVALID_REQUEST = 2


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    try:
        request = ModelOnexInstallerRequest.model_validate_json(sys.stdin.read())
    except ValidationError as exc:
        sys.stderr.write(f"invalid onex installer request: {exc}\n")
        return EXIT_INVALID_REQUEST
    sys.stdout.write(HandlerOnexInstallerRender().handle(request).script)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
