# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render the exact authorized task; the caller alone launches its structured spec."""

from omnimarket.models.lab_fill import ModelLabFillApprovedRow, ModelLabFillLaunch

from ..models.model_lab_fill_lane_render import (
    ModelLabFillFallbackItem,
    ModelLabFillLaneRenderRequest,
    ModelLabFillLaneRenderResult,
)
from .handler_lab_fill_dispatch_plan import APPROVED_KINDS
from .helpers_outcomes import pick_host

TASKS = {
    "pr-red": "Make {pr} ({ticket}) green. It is red on its current head and no live lane owns it. Read its required checks once on that head (omni:ci-watch, one read), classify each red, fix the real cause on the PR's own branch in a worktree at {where}, and push. A rebase onto its base keeps the base's records byte-identical; push it with --force-with-lease.",
    "pr-stalled": "Take over {pr} ({ticket}), stalled since {stalled} with no live owner. Bring it current with its base, answer its open review threads, and make its required checks green on the PR's own branch in a worktree at {where}. Push a rebase with --force-with-lease.",
    "defect": "Fix the {pillar} defect {ticket}: {title}. Read the ticket's acceptance criteria live, build the smallest change that meets them with tests, in a worktree at {where}, and open one PR whose title contains {ticket}.",
    "ticket": "Work {ticket}: {title}. Read the ticket's acceptance criteria live, build the smallest change that meets them with tests, in a worktree at {where}, and open one PR whose title contains {ticket}.",
    # OMN-20864, operator RULING 2026-10-10T04:08:23Z: an idle lab slot lands a PR the landing controller
    # parked, escalated or left red with no owner, as a per-PR landing lane.
    "pr-land": "Land {pr} ({ticket}) with /omni:pr-land. Lab-fill chose it for an idle lab slot ({title}): under operator RULING 2026-10-10T04:08:23Z a per-PR landing lane is the default work for an open PR the landing controller parked, escalated or left red with no owner. Run pr-land end to end on its exact head: claim, live read, hold check, CI triage, a fix on the PR's own branch in a worktree at {where}, then land it. If what is left is a cause this lane cannot fix on the PR, write your TERMINAL with outcome=blocked and cause=<required-approval|base-red|held|external-owner> and stop: lab-fill does not send it again until its head or that cause changes.",
}
APPROVED_TASK = "Finish approved {kind} {id} ({ticket}) in {repo}. Its goal and acceptance check are row {id} of the approved-work list, read by id when this brief was written:\n{marker}\nRead the ticket live and integrate existing work before building. Close-out scope: process fixes and finishing or wiring partially built work only. Worktree: {where}. Run focused RED tests first, open a PR and hand it off."
LINES = [
    "# Lane {lane} (parent {parent_lane}, ticket {ticket})",
    "Dispatched by the lab-fill controller, run {run_key}, for the {pillar} pillar, {placement}",
    "AUTHORITY: operator RULING {authority_ruling} lane={authority_lane} ticket={parent_ticket} in docs/tracking/ROLLING_WORK_LEDGER.md (find it with lane_brief.py grep '{authority_ruling} | RULING'): the lab-fill ruling, M4 tickets first and the approved-work list when lanes are free. This dispatch is that ruling's mechanism, and its scope is this one item. PR landing goes through the landing controller under its own routing RULING.",
    "WORKTREE PRECONDITIONS: Before mkdir, git clone, git -C or git worktree add, resolve the session's OMNI_HOME and the destination {where}; never redefine OMNI_HOME inline or use a home-directory default. Resolve variables from that session and resolve cd and every git -C from the command's actual working directory before execution. A sibling omni_worktrees beside any other clone is stray. An existing destination must be a clean linked worktree of the owning clone with no live peer owner. For omnibase_internal use the declared OMNIBASE_INTERNAL_HOME (an absolute existing canonical registry directory) or the sanctioned sibling of OMNI_HOME; if the live host guard lists only $OMNI_HOME/omni_worktrees, use $OMNI_HOME/omni_worktrees/{lane}/{repo_or_placeholder} instead. An invalid destination must refuse once with the guard's documented reason; stop and report it without retrying another stray path.",
    "TASK: {what}",
    "REPOSITORY: {repo}, chosen from where the work lives ({repo_source}), not a default. If the work belongs to another repository, name it in your TERMINAL and stop; do not build it here.",
    "OWNERSHIP FIRST: run pr_claim_registry_cli.py list and read the ledger (lane_brief.py grep {subject}, onex-claim-index) for a live peer. If a live lane owns {subject}, write your TERMINAL with outcome=skipped-owned naming that lane, and stop.",
    "RULES: plain commit, pre-commit, focused tests only; no merge by hand, no arming, no pkill, no stash, no --no-verify, no skip tokens, no hooksPath override, no allowlist or baseline widening, no new scripts; canonical shape only (OMN-20295): new capability is a node with a contract.yaml, a handler and bus topics, you wire the existing node or handler before building, and you add no standalone module, CLI tool or script; never flip a ticket Done. gh under 40 calls. Ledger CLAIM, STATUS and TERMINAL through /omni:ledger-write with parent={parent_lane}. Hand a green PR to {landing_lane} with one needs=land MSG through /omni:pr-handoff, and stop.",
]


# A pr-land lane lands through /omni:pr-land itself, so its rules replace the hand-off ones.
PR_LAND_RULES = (
    (
        "no merge by hand, no arming, ",
        "merge and arm only through /omni:pr-land on the PR's exact head, ",
    ),
    (
        "Hand a green PR to {landing_lane} with one needs=land MSG through /omni:pr-handoff, and stop.",
        "If pr-land cannot land it, hand it to {landing_lane} with one needs=land MSG through /omni:pr-handoff, and stop.",
    ),
)


class HandlerLabFillLaneRender:
    """Keep authority and route text identical while selecting flags without shell quoting."""

    def handle(
        self, request: ModelLabFillLaneRenderRequest
    ) -> ModelLabFillLaneRenderResult:
        item, cfg = request.item, request.config
        fallback = isinstance(item, ModelLabFillFallbackItem)
        host = (
            (item.host or (item.hosts[0] if item.hosts else ""))
            if isinstance(item, ModelLabFillFallbackItem)
            else item.host
        )
        root = (
            "${OMNIBASE_INTERNAL_HOME:-$OMNI_HOME/../omnibase_internal}/omni_worktrees"
            if item.repo == "omnibase_internal"
            else "$OMNI_HOME/omni_worktrees"
        )
        where = f"{root}/{item.lane}/{item.repo or '<repo>'}"
        values = {
            **cfg.model_dump(),
            **item.model_dump(),
            "where": where,
            "marker": f"APPROVED-ROW {item.id} (unresolved)",
            "stalled": item.updated_at or f"over {cfg.stalled_hours}h ago",
            "subject": item.pr or item.ticket,
            "repo_or_placeholder": item.repo or "<repo>",
            "placement": f"onto {host}, the host this work must run on."
            if item.pinned is True
            else "placed by the remote-lane runner on the lab host with headroom when it launched.",
        }
        task = TASKS.get(
            item.kind, APPROVED_TASK if item.kind in APPROVED_KINDS else ""
        ).format_map(values)
        parts = [
            LINES[0].format_map(values),
            item.route,
            "",
            LINES[1].format_map(values),
            "",
            LINES[2].format_map(values),
            "",
            LINES[3].format_map(values),
            "",
            "TASK: " + task,
            "",
        ]
        if not fallback and getattr(item, "repo_source", None):
            parts.extend([LINES[5].format_map(values), ""])
        rules = LINES[7]
        if item.kind == "pr-land":
            for old, new in PR_LAND_RULES:
                rules = rules.replace(old, new)
        parts.extend(
            [
                LINES[6].format_map(values),
                "",
                rules.format_map(values),
                "",
                "RETURN: per PR the quoted cause and what you did, with its checks at your last read.",
            ]
        )
        brief = "\n".join(parts)
        if isinstance(item, ModelLabFillFallbackItem):
            note = f"FALLBACK: Codex lane {item.from_lane} ended {item.reason} on {item.from_host or 'its host'} before Codex ran, so no work was done. You are its one fallback on Claude; there is no second. If a peer has taken {item.pr or item.ticket} since, write your TERMINAL with outcome=skipped-owned and stop."
            brief = brief.replace(
                "\n\nOWNERSHIP FIRST", "\n\n" + note + "\n\nOWNERSHIP FIRST"
            )
            host = pick_host(item.hosts, request.limited)
        approved = (
            ModelLabFillApprovedRow(
                id=item.id or "",
                kind=item.kind,
                ticket=item.ticket,
                marker=f"APPROVED-ROW {item.id} (unresolved)",
            )
            if item.kind in APPROVED_KINDS
            else None
        )
        launch = ModelLabFillLaunch(
            lane=item.lane,
            ticket=item.ticket,
            engine="sonnet" if fallback else item.engine,
            effort="high",
            host=host if item.pinned is True else None,
            parent=cfg.parent_lane,
            timeout_min=cfg.lane_timeout_min,
            repo=item.repo or None,
            ref=item.ref if item.repo and item.ref else None,
            pr=item.pr.split("/")[-1] if item.pr else None,
            approved_row=approved,
        )
        return ModelLabFillLaneRenderResult(brief=brief, launch=launch)
