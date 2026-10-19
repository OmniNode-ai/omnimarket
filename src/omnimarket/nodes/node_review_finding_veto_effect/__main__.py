# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Entry point of the review finding veto (OMN-17427).

    python -m omnimarket.nodes.node_review_finding_veto_effect \
        --result-file FILE --head-sha SHA [--repo-root DIR]

Reads the hostile reviewer's result document, verifies each blocking finding that
claims a Python, YAML or JSON file is invalid by parsing that file at ``SHA`` in
the checkout at ``DIR``, and rewrites FILE without the findings the parse
disproves. Each dropped finding is printed to stderr with its reason.

Exit 0 when the document was read (rewritten only if something was dropped), 2
when it could not be read or written; the caller then keeps the reviewer's
verdict as it stands.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from omnimarket.nodes.node_review_finding_veto_effect.handlers.adapter_git_head import (
    GitHeadFileReader,
)
from omnimarket.nodes.node_review_finding_veto_effect.handlers.handler_review_finding_veto import (
    HandlerReviewFindingVeto,
)
from omnimarket.nodes.node_review_finding_veto_effect.models import (
    ModelReviewFindingVetoRequest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)

    path = Path(args.result_file)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        outcome = HandlerReviewFindingVeto(GitHeadFileReader(args.repo_root)).handle(
            ModelReviewFindingVetoRequest(result=document, head_sha=args.head_sha)
        )
        for line in outcome.kept_unverified:
            sys.stderr.write(
                f"review finding veto: syntax claim stays blocking: {line}" + "\n"
            )
        for veto in outcome.vetoed:
            sys.stderr.write(
                f"review finding veto: dropped blocking finding {veto.file_path}:"
                f"{veto.line_start}: {veto.reason}" + "\n"
            )
        if outcome.vetoed:
            path.write_text(
                json.dumps(outcome.result, indent=2) + "\n", encoding="utf-8"
            )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"review finding veto: not applied: {exc!r}" + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
