# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One reading of the fleet from the PR watcher's state, and the claim-time recheck (OMN-20676).

A port of the merge-sweep skill's ``sweep_read.live_reading`` and ``claim_check`` with their
GitHub layer removed: the open PRs, their newest check copies and the merges arrive from the
watcher's state file (gathered by node_merge_sweep_effect), never from a search. The rules are the
skill's own ``sweep_rules``, in ``rules_merge_sweep``. A fact the state does not hold is named under
``unread`` and never read as a zero. The fleet, the non-product repositories and the out-of-scope
rulings arrive on the request. PRs are read in (repository, number) order, every red is classified,
and an out-of-scope repository's PRs are named and never read.
"""

from __future__ import annotations

from typing import Any

from omnimarket.handlers import rules_merge_sweep as rules
from omnimarket.models.merge_sweep import (
    CHECKS_READ,
    CHECKS_ROLLUP_ONLY,
    CHECKS_SKIPPED,
    CHECKS_UNREAD,
    ModelMergeSweepClaimCheckRequest,
    ModelMergeSweepClaimCheckResult,
    ModelMergeSweepReadRequest,
    ModelMergeSweepReadResult,
    ModelMergeSweepUnread,
    ModelSweepOpenPr,
)

BAD = frozenset(
    {
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "startup_failure",
        "error",
    }
)
ticket_of = rules.ticket_of


def _rollup_red(pr: ModelSweepOpenPr) -> bool:
    """Any check copy at the head not passing; the newest-run classification decides after."""
    return any(str(run.conclusion or "").lower() in BAD for run in pr.runs or ())


class HandlerMergeSweepRead:
    """Evaluate one reading of the fleet from gathered facts."""

    def handle(self, request: ModelMergeSweepReadRequest) -> ModelMergeSweepReadResult:
        t = rules.as_utc(request.now)
        rows = rules.parse_rows(request.ledger_lines)
        unread: list[ModelMergeSweepUnread] = []

        non_product = frozenset(request.non_product_repos)
        counted = frozenset(request.fleet) | non_product
        merges = [
            {
                "repo": m.repo,
                "number": m.number,
                "merged_at": m.merged_at,
                "files": m.files,
            }
            for m in request.merges
            if m.repo in counted
        ]
        # A merge with no changed-file list is counted under product.excluded.files_unread.
        product = rules.product_merges(
            merges,
            request.floors,
            t,
            window_min=request.window_min,
            non_product=non_product,
        )

        # A repository ruled out of scope is named, not read: no checks, no classes, no lane.
        out_of_scope_prs = sorted(
            rules.pr_key(f"{p.repo}#{p.number}")
            for p in request.open_prs
            if p.repo in request.out_of_scope
        )
        in_scope = frozenset(request.fleet) - request.out_of_scope.keys()
        reds: list[dict[str, Any]] = []
        open_prs: list[dict[str, Any]] = []
        red_now: dict[str, bool] = {}
        for pr in sorted(
            (p for p in request.open_prs if p.repo in in_scope),
            key=lambda p: (p.repo, p.number),
        ):
            repo = pr.repo
            key = f"{repo}#{pr.number}"
            open_prs.append(
                {
                    "repo": repo,
                    "number": pr.number,
                    "base": pr.base,
                    "head_ref": pr.head_ref,
                    "state": "OPEN",
                }
            )
            red_now[key] = False
            checks = pr.checks_state
            if pr.draft or checks.startswith(CHECKS_SKIPPED):
                continue
            if checks.startswith(CHECKS_UNREAD):
                unread.append(
                    ModelMergeSweepUnread(
                        field=f"checks:{key}", why=checks.removeprefix(CHECKS_UNREAD)
                    )
                )
                continue
            if checks == CHECKS_READ and not _rollup_red(pr):
                continue
            red_now[key] = True
            ticket = ticket_of(pr.title)
            entry: dict[str, Any] = {
                "repo": repo,
                "number": pr.number,
                "state": "OPEN",
                "ticket": ticket,
                "owner": rules.claim_owner(key, ticket, rows, t),
            }
            if checks == CHECKS_ROLLUP_ONLY:
                unread.append(
                    ModelMergeSweepUnread(
                        field=f"checks:{key}",
                        why=pr.runs_why
                        or "the rollup is red and its check detail is unread",
                    )
                )
                entry["classes"] = None
                reds.append(entry)
                continue
            for name in ("ready_at", "files"):
                if name in pr.facts_unread:
                    unread.append(
                        ModelMergeSweepUnread(
                            field=f"{name}:{key}",
                            why="the PR watcher state does not record it",
                        )
                    )
            runs = [run.model_dump() for run in pr.runs or ()]
            entry["ready_at"] = pr.ready_at
            entry["classes"] = rules.classify_checks(runs, pr.ready_at, pr.files)
            if all(c["cls"] == "in-progress" for c in entry["classes"]):
                red_now[key] = False
                continue
            reds.append(entry)

        heads = []
        for h in rules.chain_heads(open_prs):
            key = f"{h['repo']}#{h['number']}"
            heads.append(
                {
                    **h,
                    "state": "OPEN",
                    "cls": "red" if red_now.get(key) else "green-unarmed",
                    "owner": rules.claim_owner(key, None, rows, t),
                }
            )

        ticks = request.ticks if request.ticks is not None else []
        controller = rules.controller_stall(ticks)
        if request.ticks is None:
            unread.append(
                ModelMergeSweepUnread(
                    field="controller",
                    why="the controller's tick log was not read: controller UNKNOWN",
                )
            )
        if not ticks:
            controller["reasons"].append(
                "ticks.jsonl absent or empty on this host: controller UNKNOWN"
            )
        escalations = [
            {
                "pr": k,
                "state": (
                    "OPEN"
                    if any(f"{p['repo']}#{p['number']}" == k for p in open_prs)
                    else "UNKNOWN"
                ),
                "owner": rules.claim_owner(k, None, rows, t),
            }
            for k in rules.escalations(ticks)
        ]
        unread.append(
            ModelMergeSweepUnread(
                field="components",
                why="component sources are not in the PR watcher state; not read",
            )
        )
        return ModelMergeSweepReadResult(
            now=rules.iso(t),
            load1=request.load1,
            cpus=request.cpus,
            product=product,
            controller=controller,
            reds=reds,
            chain_heads=heads,
            escalations=escalations,
            open_prs=len(open_prs),
            scope=sorted(in_scope),
            out_of_scope_prs=out_of_scope_prs,
            unread=unread,
        )


class HandlerMergeSweepClaimCheck:
    """The claim-time recheck: is this PR still open and free for a lane to touch."""

    def handle(
        self, request: ModelMergeSweepClaimCheckRequest
    ) -> ModelMergeSweepClaimCheckResult:
        key = rules.pr_key(request.pr)
        rows = rules.parse_rows(request.ledger_lines)
        state = request.state or "UNREAD"
        owner = rules.claim_owner(
            key,
            ticket_of(request.title),
            rows,
            request.now,
            self_lane=request.self_lane,
        )
        verdict, code = rules.claim_check_verdict(state, owner)
        line = (
            f"CLAIM-CHECK {key} state={state} owner={owner.get('state')} "
            f"lane={owner.get('lane') or '-'} verdict={verdict} "
            f"self-lane={request.self_lane or '-'}"
        )
        return ModelMergeSweepClaimCheckResult(
            key=key,
            state=state,
            owner_state=owner.get("state"),
            lane=owner.get("lane"),
            verdict=verdict,
            exit_code=code,
            line=line,
        )
