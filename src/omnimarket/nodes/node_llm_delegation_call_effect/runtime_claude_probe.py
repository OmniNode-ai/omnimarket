# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_llm_delegation_call_effect.runtime_claude_probe``.

One typed ``ModelClaudeCredentialProbeRequest`` as JSON on stdin, one typed receipt as JSON on stdout.
The credential is in the environment of the host that runs it and is never read here;
the child's environment is the one the lane overlay (``--overlay``) binds.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from omnimarket.handlers.container import container_overlay_dependencies as container
from omnimarket.models.delegation.model_claude_credential_probe import (
    ModelClaudeCredentialProbeRequest,
)

from .handlers.handler_claude_credential_probe import HandlerClaudeCredentialProbe

EXIT_INVALID = 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overlay", type=container.bind_overlay_file, required=True)
    parser.parse_args(argv)
    try:
        request = ModelClaudeCredentialProbeRequest.model_validate_json(
            sys.stdin.read()
        )
        receipt = HandlerClaudeCredentialProbe().handle(request)
    except (ValidationError, ValueError, OSError):
        # The request and the host policy are echoed nowhere: the prompt is the caller's.
        sys.stderr.write("claude credential probe request or host policy invalid\n")
        return EXIT_INVALID
    sys.stdout.write(receipt.model_dump_json() + "\n")
    return 0 if receipt.exit_cause == "answered" else 1


if __name__ == "__main__":
    raise SystemExit(main())
