# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_ci_summary_registry_compute.runtime_ci_summary_registry`` (OMN-23505).

Reads one ``ModelCiSummaryRegistryRequest`` as JSON on stdin, resolves it through
``HandlerCiSummaryRegistry`` and writes the ``ModelCiSummaryRegistry`` as JSON on stdout.
It opens no file: the workflow step composes the request from the registry files with
``jq`` and redirects the answer, so a missing or unparsable file stops that step and
the gate, which finds no resolved registry, refuses. Exit 0 when the result was written
(refusals included: the gate reads them and fails closed); exit 2 when stdin is not a
valid request.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from pydantic import ValidationError

from omnimarket.nodes.node_ci_summary_registry_compute.handlers.handler_ci_summary_registry import (
    HandlerCiSummaryRegistry,
)
from omnimarket.nodes.node_ci_summary_registry_compute.models.model_ci_summary_registry import (
    ModelCiSummaryRegistryRequest,
)

EXIT_INVALID_REQUEST = 2


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    try:
        request = ModelCiSummaryRegistryRequest.model_validate_json(sys.stdin.read())
    except ValidationError as exc:
        sys.stderr.write(f"invalid ci summary registry request: {exc}\n")
        return EXIT_INVALID_REQUEST
    result = HandlerCiSummaryRegistry().handle(request)
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
