# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_handshake_policy_gate_effect: the CI caller of the handshake policy gate.

Usage::

    python -m omnimarket.nodes.node_handshake_policy_gate_effect \\
        --repos-conf architecture-handshakes/repos.conf [--strict]

Prints the compliance report to stdout and the INFO lines to stderr, and exits with the gate's
code: 0 compliant or report-only, 1 non-compliant in strict mode, 2 cannot run. The GitHub token
is resolved from the contract-declared ``secrets.GH_TOKEN``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omnimarket.nodes.node_handshake_policy_gate_effect.handlers.handler_handshake_policy_gate_run import (
    HandlerHandshakePolicyGateRun,
)
from omnimarket.nodes.node_handshake_policy_gate_effect.models.model_handshake_policy_gate_run import (
    ModelPolicyGateRunRequest,
)
from omnimarket.nodes.node_handshake_policy_gate_effect.protocols.protocol_handshake_policy_gate_run import (
    PolicyGatePortError,
    ProtocolPolicyGateReader,
    ProtocolPolicyGateSleeper,
)


def main(
    argv: list[str] | None = None,
    *,
    reader: ProtocolPolicyGateReader | None = None,
    sleeper: ProtocolPolicyGateSleeper | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="node_handshake_policy_gate_effect",
        description="Verify every active repo's handshake CI passes on its default branch.",
    )
    parser.add_argument("--repos-conf", required=True, type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--retry-attempts", default=None, metavar="RAW")
    parser.add_argument("--retry-base-delay", default=None, metavar="RAW")
    args = parser.parse_args(argv)
    if not args.repos_conf.is_file():
        sys.stderr.write(f"ERROR: repos.conf not found at {args.repos_conf}\n")
        return 2
    request = ModelPolicyGateRunRequest(
        repos_conf_text=args.repos_conf.read_text(),
        strict=args.strict,
        max_attempts_raw=args.retry_attempts,
        base_delay_raw=args.retry_base_delay,
    )
    try:
        result = HandlerHandshakePolicyGateRun(reader, sleeper).handle(request)
    except PolicyGatePortError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2
    sys.stderr.write(result.stderr)
    sys.stdout.write(result.stdout)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
