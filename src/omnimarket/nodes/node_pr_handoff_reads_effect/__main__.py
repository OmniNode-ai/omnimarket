# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_pr_handoff_reads_effect < request.json`` prints the one install-proof line.

Exit 0 when a line was printed (``OK``, ``NA`` or ``NO``; a refused declaration prints ``NO``), 2 when the request is not
valid JSON for the request model.
"""

import sys

from pydantic import ValidationError

from omnimarket.nodes.node_pr_handoff_reads_effect.handlers.handler_install_proof_read import (
    HandlerInstallProofRead,
)
from omnimarket.nodes.node_pr_handoff_reads_effect.models.model_install_proof_read import (
    ModelInstallProofReadRequest,
)


def main() -> int:
    try:
        request = ModelInstallProofReadRequest.model_validate_json(sys.stdin.read())
    except ValidationError as exc:
        sys.stderr.write(
            f"install-proof request is invalid ({len(exc.errors())} fields)\n"
        )
        return 2
    sys.stdout.write(HandlerInstallProofRead().handle(request).line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
