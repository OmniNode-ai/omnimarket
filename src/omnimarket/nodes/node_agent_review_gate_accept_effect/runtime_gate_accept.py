# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line of the gate-accept node, the form the Hostile Reviewer workflow runs.

    python -m omnimarket.nodes.node_agent_review_gate_accept_effect.runtime_gate_accept \
        --repository OWNER/NAME --pull-request N --head-sha SHA \
        --voter-result-path FILE --step-output-path FILE --step-summary-path FILE

The workflow runs this from a worktree of the pull request's base commit, so the code
that decides is never the pull request's own. The decision (``unchanged``, ``pass_agent``
or ``refuse``), its typed reason and the receipt id are appended to the step output file,
and the summary line to the step summary file.

Exit 0 when the node decided, whatever the decision; 2 when the input is not one the
node accepts. The caller then lets the voters' verdict stand.
"""

from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError

from omnimarket.nodes.node_agent_review_gate_accept_effect.handlers.handler_gate_accept import (
    HandlerGateAccept,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.handlers.handler_receipt_gate import (
    HandlerReceiptGate,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.models import (
    ModelGateAcceptRequest,
    ModelReceiptGateRequest,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.protocols import (
    ProtocolReceiptVerifier,
    ProtocolReviewReader,
)


def main(
    argv: list[str] | None = None,
    *,
    reviews: ProtocolReviewReader | None = None,
    verifier: ProtocolReceiptVerifier | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pull-request", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--voter-result-path")
    parser.add_argument("--receipt-only", action="store_true")
    parser.add_argument("--wait-seconds", type=float, default=0)
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--step-output-path", required=True)
    parser.add_argument("--step-summary-path", required=True)
    args = parser.parse_args(argv)
    handler = HandlerGateAccept(reviews=reviews, verifier=verifier)
    try:
        if args.receipt_only:
            receipt_request = ModelReceiptGateRequest(
                repository=args.repository,
                pull_request=args.pull_request,
                head_sha=args.head_sha,
                step_output_path=args.step_output_path,
                step_summary_path=args.step_summary_path,
                wait_seconds=args.wait_seconds,
                poll_seconds=args.poll_seconds,
            )
            result = HandlerReceiptGate(handler).handle(receipt_request)
        else:
            if not args.voter_result_path:
                parser.error("--voter-result-path is required unless --receipt-only")
            request = ModelGateAcceptRequest(
                repository=args.repository,
                pull_request=args.pull_request,
                head_sha=args.head_sha,
                step_output_path=args.step_output_path,
                step_summary_path=args.step_summary_path,
                voter_result_path=args.voter_result_path,
            )
            result = handler.handle(request)
    except ValidationError as exc:
        sys.stderr.write(f"gate accept: the input is not accepted: {exc}\n")
        return 2
    sys.stderr.write(result.summary_line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
