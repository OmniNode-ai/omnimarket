# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The brief a merge-sweep lane is dispatched with (OMN-20676).

A port of the sweep workflow's ``briefFor``: the lane's header, the PRs it covers with any claim it
supersedes, the standing rules, the claim-time recheck every lane runs before it touches a PR, and
what each lane kind does. The text is the workflow's own, byte for byte: the recheck command and
the ticket the lane reports under arrive on the request, so a caller whose recheck is another
tool names that tool, and the default is the old reader's command.
"""

from __future__ import annotations

from ..models import (
    ModelMergeSweepBrief,
    ModelMergeSweepBriefRequest,
)

# The standing rules block the workflow prefixes to every brief and every agent call, as it was
# written (it opens with a newline).
STANDING_RULES = """
Premise: every factual premise in this brief is marked 'verified by: <command> => <observation>' or 'UNVERIFIED -- falsify first'. Read unmarked premises as UNVERIFIED and verify them before acting. Open any prose report with 'Premises falsified: <premise> -- <evidence>' or 'Premises falsified: none'. For schema-only calls, emit any falsified premise to stderr and preserve the response schema and every prescribed verbatim field. (OMN-18397.)

Report to file: write the full prose report to $OMNI_HOME/.claude_scratch/reports/<lane>-<YYYY-MM-DDTHHMMZ>.md — lane-namespaced, because that directory is shared — and return at most 12 lines: a verdict line, that path, then only the facts the orchestrator must act on. For schema-only calls, preserve the response schema and every prescribed verbatim field. (OMN-18397.)

Mint authority: never create a ticket on your own initiative. A brief carrying the literal line 'Mint authority: delegated by the orchestrator for exactly one ticket' grants exactly one — do not refuse it, and do not mint a second. Search Linear first and adopt an existing ticket when one already covers the work; say so instead of minting a duplicate. This brief grants no mint authority. (OMN-18397.)"""


class HandlerMergeSweepBrief:
    """Render the brief of one lane."""

    def handle(self, request: ModelMergeSweepBriefRequest) -> ModelMergeSweepBrief:
        prs = (
            ", ".join(
                p.pr
                + (
                    f" (supersedes-claim={p.supersedes_claim})"
                    if p.supersedes_claim
                    else ""
                )
                for p in request.prs
            )
            or "none"
        )
        repo = f" for {request.repo}" if request.repo else ""
        reasons = f"Reasons: {'; '.join(request.reasons)}.\n" if request.reasons else ""
        text = (
            f"# Lane {request.lane}: merge-sweep {request.kind} lane{repo} ({request.ticket})\n"
            f"Parent: {request.sweep_lane} (orchestrator {request.orchestrator}). "
            f"Kind: {request.kind}. PRs: {prs}.\n"
            f"{reasons}\n"
            f"{STANDING_RULES}\n"
            f"Before you touch any PR, run {request.claim_check_command} for that PR.\n"
            "Exit 0 (FREE, or STALE with a supersedes-claim) means proceed, citing that "
            "supersedes-claim in your CLAIM. Any other exit means another lane owns it or it is "
            "merged or closed: leave that PR alone and say so.\n"
            "Kind fix: fix each PR's red checks by their class and remedy (/omni:ci-watch names the "
            "move per class), then hand the PR to the landing controller by one ledger MSG "
            "(/omni:ledger-msg). Kind land-chain-head: hand the chain head to landing-controller by "
            "one ledger MSG through /omni:ledger-msg (to=landing-controller | pr=<owner/repo#n> | "
            "head=<sha> | needs=land), naming its waiting children in the MSG text, then stop "
            "touching it. Kind escalation: the landing controller gave up on this PR "
            "(escalation_exhausted); find why and fix it. Kind diagnose: read why the controller "
            "dispatched no workers or why the named repos sit under their floor, and report the "
            "cause with evidence; change nothing outside your own ticket.\n"
            "This lane writes no merge, arm, rerun or CI poll, whatever its kind.\n"
            "Report once at terminal with the PRs touched and their head shas.\n"
        )
        return ModelMergeSweepBrief(text=text)
