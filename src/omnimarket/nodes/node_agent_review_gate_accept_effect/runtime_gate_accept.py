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
from omnimarket.nodes.node_agent_review_gate_accept_effect.models import (
    ModelGateAcceptRequest,
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
    parser.add_argument("--voter-result-path", required=True)
    parser.add_argument("--step-output-path", required=True)
    parser.add_argument("--step-summary-path", required=True)
    args = parser.parse_args(argv)
    try:
        request = ModelGateAcceptRequest(
            repository=args.repository,
            pull_request=args.pull_request,
            head_sha=args.head_sha,
            voter_result_path=args.voter_result_path,
            step_output_path=args.step_output_path,
            step_summary_path=args.step_summary_path,
        )
    except ValidationError as exc:
        sys.stderr.write(f"gate accept: the input is not accepted: {exc}\n")
        return 2
    result = HandlerGateAccept(reviews=reviews, verifier=verifier).handle(request)
    sys.stderr.write(result.summary_line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
