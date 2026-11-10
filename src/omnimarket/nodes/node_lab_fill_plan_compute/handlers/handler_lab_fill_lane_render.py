# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render the exact authorized task; the caller alone launches its structured spec."""

from omnimarket.models.lab_fill import (
    ALREADY_BUILT_LINE,
    PUSH_AND_REPORT_LINE,
    ModelLabFillApprovedRow,
    ModelLabFillLaunch,
)

from ..models.model_lab_fill_lane_render import (
    ModelLabFillFallbackItem,
    ModelLabFillLaneRenderResult,
)
from ..models.model_lab_fill_lane_render import (
    ModelLabFillLaneRenderRequest as ModelLabFillLaneRenderRequest,
)
from .handler_lab_fill_dispatch_plan import APPROVED_KINDS
from .helpers_outcomes import pick_host

# The task of a lane that lands a pull request: a red one, or a stalled one the landing controller escalated.
LAND_TASK = "Land {pr} ({ticket}) end to end with /omni:pr-land: {why}. Operator RULING 2026-10-10T04:08:23Z (OMN-20864) makes a per-PR pr-land lane the default for a PR the landing controller parked or escalated, or one that is red with no owner, whenever lab slots are idle. Read its required checks once on its current head (omni:ci-watch, one read), classify each red, fix the real cause on the PR's own branch in a worktree at {where}, push (a rebase onto its base keeps the base's records byte-identical; push it with --force-with-lease), and land it on the exact head through /omni:pr-land, whose gates decide. If a cause no lane can fix on this PR stops it (the base branch is red, the same red is on other open PRs, a HOLD names it, or it needs a human approval), write your TERMINAL with outcome=blocked naming that cause, and stop."
TASKS = {
    "pr-red": LAND_TASK,
    "pr-stalled": "Take over {pr} ({ticket}), stalled since {stalled} with no live owner. Bring it current with its base, answer its open review threads, and make its required checks green on the PR's own branch in a worktree at {where}. Push a rebase with --force-with-lease.",
    "defect": "Fix the {pillar} defect {ticket}: {title}. Read the ticket's acceptance criteria live, build the smallest change that meets them with tests, in a worktree at {where}, and open one PR whose title contains {ticket}.",
    "ticket": "Work {ticket}: {title}. Read the ticket's acceptance criteria live, build the smallest change that meets them with tests, in a worktree at {where}, and open one PR whose title contains {ticket}.",
    # OMN-20780: the tier-1 source kinds are tickets chosen by their source, so they carry the ticket brief.
    "plan-stalled": "Work {ticket}: {title}. It is the next action of a stalled priority plan ({evidence}) and no live lane owns it. Read the ticket's acceptance criteria live and integrate existing work before building, build the smallest change that meets them with tests, in a worktree at {where}, and open one PR whose title contains {ticket}.",
    "ruling-pending": "Work {ticket}: {title}. It is the pending mechanism ticket of an operator process ruling that has no built and wired mechanism ({evidence}). Read the ticket's acceptance criteria and the ruling row live, build the mechanism the ticket names with tests, in a worktree at {where}, and open one PR whose title contains {ticket}. A mechanism counts only when it is wired: a content check needs a required context and a pre-commit hook; a timer needs a loaded unit and a recent run record.",
    "lane-resend": "Finish {ticket}: {title}. An earlier lane stopped short ({evidence}) and no live lane owns the ticket now. Read the ticket's acceptance criteria and that lane's TERMINAL row live, integrate its pushed branch or patch before building anything new, fix the blocker it names, finish the work with tests, in a worktree at {where}, and open one PR whose title contains {ticket}. If the blocker is a credential or an operator decision, stop and report it once.",
    # OMN-20864, operator RULING 2026-10-10T04:08:23Z: an idle lab slot lands a PR the landing controller
    # parked, escalated or left red with no owner, as a per-PR landing lane.
    "pr-land": "Land {pr} ({ticket}) with /omni:pr-land. Lab-fill chose it for an idle lab slot ({title}): under operator RULING 2026-10-10T04:08:23Z a per-PR landing lane is the default work for an open PR the landing controller parked, escalated or left red with no owner. Run pr-land end to end on its exact head: claim, live read, hold check, CI triage, a fix on the PR's own branch in a worktree at {where}, then land it. If what is left is a cause this lane cannot fix on the PR, write your TERMINAL with outcome=blocked and cause=<required-approval|base-red|held|external-owner> and stop: lab-fill does not send it again until its head or that cause changes.",
    # OMN-22413, operator RULING 2026-11-02: fixing factory machinery comes before features.
    "machinery": "Fix a factory machinery break ({title}). Priority class {priority_class}; ticket {ticket}. Read the evidence first: the FRICTION rows of this class in the rolling ledger (lane_brief.py grep) and the ticket live. Find the root cause on the real call path, build the smallest change that removes it with a red test first, in a worktree at {where}, and open one PR whose title contains {ticket}. If a ticket you have read already covers this class, cite that one and do not mint another. If a live lane owns the class, write your TERMINAL with outcome=skipped-owned and stop.",
    # OMN-17427: a ticket skipped because its implementation merged is verified and closed, not skipped.
    "dod-verify": "Verify and close {ticket}. Lab-fill chose it for an idle lab slot because its {title}. Read the ticket and its acceptance criteria live, then run /onex:dod_verify against its contract on the merged code. If every criterion has evidence, record the dod_evidence the receipt names on the ticket and move it to Done. If a criterion has none, name it, build the smallest change that meets it with tests in a worktree at {where}, and open one PR whose title contains {ticket}. If the ticket is already Done or the merged work belongs to another ticket, write your TERMINAL with outcome=no-op naming why and stop.",
}
APPROVED_TASK = "Finish approved {kind} {id} ({ticket}) in {repo}. Its goal and acceptance check are row {id} of the approved-work list, read by id when this brief was written:\n{marker}\nRead the ticket live and integrate existing work before building. Close-out scope: process fixes and finishing or wiring partially built work only. Worktree: {where}. Run focused RED tests first, open a PR and hand it off."
# The CLOSE rule every lab-fill brief carries: how a lane's TERMINAL reads (OMN-20864).
CLOSE_RULE = "your TERMINAL states what you did and what backs it. A lane that only reads (no commit, no PR, no ticket edit, no drafted text) closes with the typed not-applicable delegation: the pipe cell `delegation=na:<reason>` (a reason of 2 to 41 lowercase letters, digits and hyphens, such as `read-only-verification`) and no `delegated=` cell. A lane that drafts text or code goes through 3a.9 and writes `delegated=<n> runs=<ids>`. Write outcome=done only when the TERMINAL carries `evidence=<the report file you wrote under .claude_scratch/reports/>` and that report lists each command you ran in this lane with its exit code and output, and a `pr=` cell for every PR you pushed; a result copied from an earlier lane's report is not evidence. A lane that ran no command, or could not read what its brief names, writes outcome=blocked and says which."
LINES = [
    "# Lane {lane} (parent {parent_lane}, ticket {ticket})",
    "Dispatched by the lab-fill controller, run {run_key}, for the {pillar} pillar, {placement}",
    "AUTHORITY: operator RULING {authority_ruling} lane={authority_lane} ticket={parent_ticket} in docs/tracking/ROLLING_WORK_LEDGER.md (find it with lane_brief.py grep '{authority_ruling} | RULING'): the lab-fill ruling, M4 tickets first and the approved-work list when lanes are free. This dispatch is that ruling's mechanism, and its scope is this one item. {landing}",
    "WORKTREE PRECONDITIONS: Before mkdir, git clone, git -C or git worktree add, resolve the session's OMNI_HOME and the destination {where}; never redefine OMNI_HOME inline or use a home-directory default. Resolve variables from that session and resolve cd and every git -C from the command's actual working directory before execution. A sibling omni_worktrees beside any other clone is stray. An existing destination must be a clean linked worktree of the owning clone with no live peer owner. For omnibase_internal use the declared OMNIBASE_INTERNAL_HOME (an absolute existing canonical registry directory) or the sanctioned sibling of OMNI_HOME; if the live host guard lists only $OMNI_HOME/omni_worktrees, use $OMNI_HOME/omni_worktrees/{lane}/{repo_or_placeholder} instead. An invalid destination must refuse once with the guard's documented reason; stop and report it without retrying another stray path.",
    "TASK: {what}",
    "REPOSITORY: {repo}, chosen from where the work lives ({repo_source}), not a default. If the work belongs to another repository, name it in your TERMINAL and stop; do not build it here. Write the cell owning-repo=<repository> (that repository's directory name) in that TERMINAL: the next tick sends this ticket there.",
    "OWNERSHIP FIRST: read the ledger (lane_brief.py grep {subject}) for an exact open CLAIM whose lane wrote any row in the last 45 minutes, and unreleased controller cause leases. Ticket-index and per-PR registry disagreement is not ownership. If a live lane owns {subject}, write your TERMINAL with outcome=skipped-owned naming that lane, and stop.",
    "RULES: plain commit, pre-commit, focused tests only; no merge by hand, no arming, no pkill, no stash, no --no-verify, no skip tokens, no hooksPath override, no allowlist or baseline widening, no new scripts; canonical shape only (the standing rules' Canonical shape paragraph): new capability is a node with a contract.yaml, a handler and bus topics, you wire the existing node or handler before building, and you add no standalone module, CLI tool or script; never flip a ticket Done. gh under 40 calls. Ledger CLAIM, STATUS and TERMINAL through /omni:ledger-write with parent={parent_lane}. Hand a green PR to {landing_lane} with one needs=land MSG through /omni:pr-handoff, and stop.",
    "TICKET READ: a lab lane has no Linear tool. Read {ticket}'s description and acceptance criteria with the host's LINEAR_API_KEY (one GraphQL issue query to api.linear.app; never print the key). If the ticket cannot be read, write your TERMINAL with outcome=blocked naming ticket-unreadable and stop: criteria taken from a contract file or an earlier lane's report are not the ticket's.",
]
# The authority line's closing sentence: a lane that lands its pull request cites the landing ruling.
LANDING_SENTENCE = (
    "PR landing goes through the landing controller under its own routing RULING."
)
LAND_SENTENCE = (
    "This lane lands its PR under operator RULING 2026-10-10T04:08:23Z (OMN-20864)."
)
# Kinds that land a pull request rather than open one carry no WIP declaration (the runner's `Opens PR:` read).
LANDING_KINDS = ("pr-red", "pr-stalled", "pr-land")


# A dod-verify lane closes its ticket on a passing DoD receipt, so its Done rule names that receipt.
DOD_VERIFY_RULES = (
    (
        "never flip a ticket Done.",
        "flip the ticket Done only on a passing /onex:dod_verify receipt with its dod_evidence.",
    ),
)
# A lane that lands its pull request itself (pr-red, an escalated pr-stalled) merges only through /omni:pr-land.
LAND_RULES = (
    (
        "no merge by hand, no arming, ",
        "merge and arm only through /omni:pr-land on the exact head, never by hand, ",
    ),
    (
        "Hand a green PR to {landing_lane}",
        "When /omni:pr-land cannot finish but the PR is green, hand it to {landing_lane}",
    ),
)
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
        extra = item.model_dump()
        land = item.kind == "pr-red" or (
            item.kind == "pr-stalled" and extra.get("escalated") is True
        )
        values = {
            **cfg.model_dump(),
            **{key: "" if value is None else value for key, value in extra.items()},
            "where": where,
            "marker": f"APPROVED-ROW {item.id} (unresolved)",
            "stalled": item.updated_at or f"over {cfg.stalled_hours}h ago",
            "subject": item.pr or getattr(item, "machinery_key", None) or item.ticket,
            "repo_or_placeholder": item.repo or "<repo>",
            "placement": f"onto {host}, the host this work must run on."
            if item.pinned is True
            else "placed by the remote-lane runner on the lab host with headroom when it launched.",
            "why": "the landing controller parked or escalated it and no live lane owns it"
            if extra.get("escalated") is True
            else "it is red on its current head and no live lane owns it",
            "landing": LAND_SENTENCE if land else LANDING_SENTENCE,
        }
        task = (
            LAND_TASK
            if land
            else TASKS.get(
                item.kind, APPROVED_TASK if item.kind in APPROVED_KINDS else ""
            )
        ).format_map(values)
        fix_evidence = " ".join(str(extra.get("fix_evidence") or "").split())
        parts = [
            LINES[0].format_map(values),
            item.route,
            f"Fill kind: {item.kind}",
            *([f"Fix evidence: {fix_evidence}"] if fix_evidence else []),
            *(
                [f"Opens PR: {item.repo} kind={item.kind}"]
                if item.repo and item.kind not in LANDING_KINDS
                else []
            ),
            "",
            LINES[1].format_map(values),
            "",
            LINES[2].format_map(values),
            "",
            LINES[3].format_map(values),
            "",
            "TASK: " + task,
            "",
            PUSH_AND_REPORT_LINE,
            "",
        ]
        if extra.get("repo_source"):
            parts.extend([LINES[5].format_map(values), ""])
        if not item.kind.startswith("pr-"):
            parts.extend([LINES[8].format_map(values), "", ALREADY_BUILT_LINE, ""])
        rules = LINES[7]
        for old, new in (
            LAND_RULES
            if land
            else {"pr-land": PR_LAND_RULES, "dod-verify": DOD_VERIFY_RULES}.get(
                item.kind, ()
            )
        ):
            rules = rules.replace(old, new)
        parts.extend(
            [
                LINES[6].format_map(values),
                "",
                rules.format_map(values),
                "",
                f"CLOSE: {CLOSE_RULE}",
                "",
                "RETURN: per PR the quoted cause and what you did, with its checks at your last read.",
            ]
        )
        brief = "\n".join(parts)
        if isinstance(item, ModelLabFillFallbackItem):
            if item.retry == "capacity":
                note = f"RETRY: lane {item.from_lane} was refused for capacity (no host took it) and no work was done. You are its one retry, on {item.hosts[0] if item.hosts else 'another host'}; there is no second. If a peer has taken {item.pr or item.ticket} since, write your TERMINAL with outcome=skipped-owned and stop."
            else:
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
            **(
                {"priority_class": klass}
                if (klass := getattr(item, "priority_class", None))
                else {}
            ),
        )
        return ModelLabFillLaneRenderResult(brief=brief, launch=launch)
