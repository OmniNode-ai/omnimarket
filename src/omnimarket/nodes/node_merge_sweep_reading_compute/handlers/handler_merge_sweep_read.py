# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One reading of the fleet from the PR watcher's state, and the claim-time recheck (OMN-20676).

A port of the merge-sweep skill's ``sweep_read.live_reading`` and ``claim_check`` with their
GitHub layer removed: the open PRs, their newest check copies and the merges arrive from the
watcher's state file (gathered by node_merge_sweep_effect), never from a search. The rules are the
skill's own ``sweep_rules``, in ``rules_merge_sweep``. A fact the state does not hold is named under
``unread`` and never read as a zero.
"""

from __future__ import annotations

from typing import Any

from omnimarket.handlers import rules_merge_sweep as rules
from omnimarket.models.merge_sweep import (
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

        merges = [
            {
                "repo": m.repo,
                "number": m.number,
                "merged_at": m.merged_at,
                "files": m.files,
            }
            for m in request.merges
        ]
        product = rules.product_merges(
            merges, request.floors, t, window_min=request.window_min
        )
        if product["excluded"]["files_unread"]:
            unread.append(
                ModelMergeSweepUnread(
                    field="merge-files",
                    why=(
                        f"{product['excluded']['files_unread']} merges in the window "
                        "carry no changed-file list; none counts as a product merge"
                    ),
                )
            )

        reds: list[dict[str, Any]] = []
        open_prs: list[dict[str, Any]] = []
        red_now: dict[str, bool] = {}
        detail_budget = request.max_reds
        for repo in sorted(rules.FLEET_REPOS):
            for pr in (p for p in request.open_prs if p.repo == repo):
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
                red_now[key] = _rollup_red(pr)
                if pr.draft:
                    continue
                if pr.runs is None:
                    unread.append(
                        ModelMergeSweepUnread(
                            field=f"checks:{key}",
                            why=pr.runs_why
                            or "the state holds no check read at this head",
                        )
                    )
                    continue
                if not red_now[key]:
                    continue
                ticket = ticket_of(pr.title)
                entry: dict[str, Any] = {
                    "repo": repo,
                    "number": pr.number,
                    "state": "OPEN",
                    "ticket": ticket,
                    "owner": rules.claim_owner(key, ticket, rows, t),
                }
                if detail_budget <= 0:
                    unread.append(
                        ModelMergeSweepUnread(
                            field=f"checks:{key}",
                            why=f"over max_reds {request.max_reds}; classes unread",
                        )
                    )
                    entry["classes"] = None
                    reds.append(entry)
                    continue
                detail_budget -= 1
                for name in ("ready_at", "files"):
                    if name in pr.facts_unread:
                        unread.append(
                            ModelMergeSweepUnread(
                                field=f"{name}:{key}",
                                why="the PR watcher state does not record it",
                            )
                        )
                runs = [run.model_dump() for run in pr.runs]
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
        owner = rules.claim_owner(key, ticket_of(request.title), rows, request.now)
        verdict, code = rules.claim_check_verdict(state, owner)
        line = (
            f"CLAIM-CHECK {key} state={state} owner={owner.get('state')} "
            f"lane={owner.get('lane') or '-'} verdict={verdict}"
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
