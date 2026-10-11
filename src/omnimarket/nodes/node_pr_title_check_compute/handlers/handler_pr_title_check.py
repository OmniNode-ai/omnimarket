# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure port of the change-control reusable's check-title bash decision."""

import re

from omnimarket.nodes.node_pr_title_check_compute.models.model_pr_title_check import (
    EnumPrTitleCheckReason,
    ModelPrTitleCheckRequest,
    ModelPrTitleCheckResult,
)

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_DEPENDENCY_BUMP = re.compile(r"^(chore\(deps|build\(deps|bump )")
_RELEASE = re.compile(r"^(chore: release|chore\(release\)|release:)")
_TICKET_REFERENCE = re.compile(r"OMN-[0-9]+")


class HandlerPrTitleCheck:
    """Decide PR title admission without I/O, clock or environment access."""

    def handle(self, request: ModelPrTitleCheckRequest) -> ModelPrTitleCheckResult:
        """Preserve the bash branch order and byte-identical output."""
        title_lower = request.title.translate(_ASCII_LOWER)
        if not request.title:
            reason = EnumPrTitleCheckReason.EMPTY_TITLE
            output = (
                "[FAIL] PR_TITLE is empty — this workflow must be called "
                "from a pull_request event."
            )
        elif request.author.endswith("[bot]"):
            reason = EnumPrTitleCheckReason.BOT_AUTHOR
            output = f"[PASS] Exempt: GitHub App bot ({request.author})"
        elif _DEPENDENCY_BUMP.search(title_lower):
            reason = EnumPrTitleCheckReason.DEPENDENCY_BUMP
            output = "[PASS] Exempt: dependency bump"
        elif _RELEASE.search(title_lower):
            reason = EnumPrTitleCheckReason.RELEASE
            output = "[PASS] Exempt: release PR"
        elif tickets := _TICKET_REFERENCE.findall(request.title):
            reason = EnumPrTitleCheckReason.TICKET_REFERENCE
            output = "[PASS] PR title contains ticket reference: " + "\n".join(tickets)
        else:
            reason = EnumPrTitleCheckReason.MISSING_TICKET_REFERENCE
            output = (
                "[FAIL] PR title must contain an OMN-XXXX ticket reference.\n"
                "\n"
                f"Title: {request.title}\n"
                "\n"
                "Exempt categories (no ticket needed):\n"
                "  - Any GitHub App bot (author ends with [bot])\n"
                "  - Titles starting with: chore(deps, build(deps, Bump\n"
                "  - Titles starting with: chore: release, chore(release), release:\n"
                "\n"
                "To fix: add [OMN-XXXX] or OMN-XXXX to the PR title."
            )

        admitted = reason not in (
            EnumPrTitleCheckReason.EMPTY_TITLE,
            EnumPrTitleCheckReason.MISSING_TICKET_REFERENCE,
        )
        return ModelPrTitleCheckResult(
            admitted=admitted,
            reason=reason,
            exit_code=0 if admitted else 1,
            output_lines=tuple(output.split("\n")),
        )
