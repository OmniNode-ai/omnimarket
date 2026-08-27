# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The git-automation demotion ratchet (OMN-16536 AC#2). Pure — no I/O.

Asserts that no Linear ``GitAutomationState``, on any team, can resolve a
``completed``-type ticket to a non-``completed`` state.

The ratchet
-----------
Because a ``GitAutomationState`` carries no source-state predicate and fires
unconditionally, the assertion reduces to a property of each automation's target
alone:

    per team:  len(gitAutomationStates) == 0
               OR every automation targets a completed-type state

Anything else fails.

Guarding the guard
------------------
The passing state of this ratchet is, in practice, "we found nothing" — AC#1
deleted all ten mappings on 2026-08-27, so the steady green is three teams with
zero automations each. A check whose green state is an empty result is only
worth anything if it can distinguish *nothing is configured* from *the probe did
not work*. Four failure modes produce an identical empty set: a revoked or
downscoped token, a renamed GraphQL field, a complexity-capped query, and a
silently truncated page.

So emptiness is never trusted on its own. It is accepted only behind a positive
control that must independently prove the probe ran:

1. Team enumeration succeeded (no transport or GraphQL error).
2. It returned at least one team.
3. Every enumerated team was actually probed — no team silently skipped.
4. No team's probe errored.
5. No team's automation page came back exactly full, which cannot prove it was
   the last page.

Any of these failing produces ``passed=False`` with the reason named, regardless
of how clean the findings look. An API error FAILS the check; it never passes
silently.

This is the deliberate inverse of the OMN-15373 guard's rule, which fails on an
empty automation set outright. That rule was correct while a populated set was
expected, and became wrong the moment AC#1 made empty the target state. The
resolution is not to drop the scepticism but to relocate it: prove the probe,
then believe the emptiness.
"""

from __future__ import annotations

from datetime import datetime

from omnimarket.nodes.node_linear_triage.models.model_git_automation_demotion_guard import (
    COMPLETED_STATE_TYPE,
    EnumDemotionVerdict,
    ModelDemotionAuditReport,
    ModelDemotionFinding,
    ModelTeamAutomationProbe,
)
from omnimarket.nodes.node_linear_triage.models.model_git_automation_guard import (
    ModelGitAutomationState,
)

__all__ = [
    "audit_demotion_risk",
    "evaluate_demotion_risk",
    "render_demotion_report",
]


def _is_all_branches(automation: ModelGitAutomationState) -> bool:
    """True when the automation is scoped to no branch pattern (i.e. all)."""
    return not (automation.target_branch or "").strip()


def evaluate_demotion_risk(
    automation: ModelGitAutomationState,
) -> ModelDemotionFinding:
    """Return the fail-closed verdict for one git automation. Pure — no I/O."""
    all_branches = _is_all_branches(automation)

    if not automation.state_readable:
        return ModelDemotionFinding(
            team_key=automation.team_key,
            automation_id=automation.automation_id,
            event=automation.event,
            state_name=automation.state_name,
            state_type=automation.state_type,
            verdict=EnumDemotionVerdict.UNREADABLE,
            reason=(
                f"the target workflow state for the {automation.event!r} automation on "
                f"team {automation.team_key!r} could not be read, so it cannot be shown "
                "to be a completed-type state. An unreadable check is not a passing "
                "check — failing closed (OMN-16536 AC#2)."
            ),
            all_branches=all_branches,
        )

    if automation.state_type.strip().lower() == COMPLETED_STATE_TYPE:
        return ModelDemotionFinding(
            team_key=automation.team_key,
            automation_id=automation.automation_id,
            event=automation.event,
            state_name=automation.state_name,
            state_type=automation.state_type,
            verdict=EnumDemotionVerdict.CLEAN,
            reason=(
                f"{automation.event!r} resolves to {automation.state_name or '<none>'!r} "
                f"(type {COMPLETED_STATE_TYPE!r}), so firing it cannot demote a "
                "completed ticket. NOTE: a completed-type target is simultaneously "
                "DRIFT for the OMN-15373 guard, which asserts the mirror property — "
                "both assertions must hold."
            ),
            all_branches=all_branches,
        )

    scope = (
        "EVERY branch of EVERY repo"
        if all_branches
        else f"branch pattern {automation.target_branch!r}"
    )
    return ModelDemotionFinding(
        team_key=automation.team_key,
        automation_id=automation.automation_id,
        event=automation.event,
        state_name=automation.state_name,
        state_type=automation.state_type,
        verdict=EnumDemotionVerdict.DEMOTION_RISK,
        reason=(
            f"team {automation.team_key!r} git automation {automation.event!r} resolves "
            f"to {automation.state_name or '<none>'!r}, whose type is "
            f"{automation.state_type or '<none>'!r} — not {COMPLETED_STATE_TYPE!r} — on "
            f"{scope}. A GitAutomationState has no source-state predicate: it fires "
            "unconditionally, so this mapping demotes a verified-Done ticket to a "
            "non-completed state whenever any PR cites it, including a bare "
            "non-closing 'Refs:' mention in an unrelated PR (OMN-16536; measured "
            "+2.3-3.2s after the driving PR event). Delete this mapping, or retarget "
            "it to a completed-type state."
        ),
        all_branches=all_branches,
    )


def _positive_control_failure(
    team_probes: list[ModelTeamAutomationProbe],
    *,
    teams_enumerated: int,
    enumeration_ok: bool,
    enumeration_error: str,
) -> str:
    """Return the reason the probe cannot be trusted, or "" when it can.

    Checked BEFORE any finding is considered: a run that cannot prove it read
    the workspace has proven nothing, however clean its findings look.
    """
    if not enumeration_ok:
        return (
            "Linear team enumeration FAILED, so no assertion could be made about any "
            "team's git automations. An API error fails the check — it never passes "
            f"silently (OMN-16536 AC#2). Probe error: {enumeration_error or 'unknown'}"
        )

    if teams_enumerated <= 0:
        return (
            "Linear team enumeration returned ZERO teams. That is indistinguishable "
            "from a revoked token, a downscoped key, or a renamed field, so it is "
            "never read as 'no demotion risk found'. Failing closed (OMN-16536 AC#2)."
        )

    if len(team_probes) != teams_enumerated:
        probed = ", ".join(sorted(p.team_key for p in team_probes)) or "<none>"
        return (
            f"{teams_enumerated} team(s) were enumerated but only {len(team_probes)} "
            f"were probed ({probed}). A silently skipped team shrinks the audit "
            "population without anyone noticing — the same failure as not checking at "
            "all. Failing closed (OMN-16536 AC#2)."
        )

    broken = [p for p in team_probes if not p.probe_ok]
    if broken:
        detail = "; ".join(
            f"{p.team_key}: {p.probe_error or 'unknown error'}" for p in broken
        )
        return (
            f"{len(broken)} team probe(s) FAILED, so those teams' automations are "
            "unknown. The other teams being clean proves nothing about the ones that "
            f"could not be read. Failing closed (OMN-16536 AC#2). {detail}"
        )

    truncated = [p for p in team_probes if p.possibly_truncated]
    if truncated:
        detail = "; ".join(
            f"{p.team_key}: {len(p.automations)} returned at page size {p.page_size}"
            for p in truncated
        )
        return (
            f"{len(truncated)} team(s) returned an automation page that was exactly "
            "full, which cannot prove it was the last page — an automation beyond the "
            "page boundary would be invisible to this audit. Failing closed on "
            f"possible truncation (OMN-16536 AC#2). {detail}"
        )

    return ""


def audit_demotion_risk(
    team_probes: list[ModelTeamAutomationProbe],
    *,
    teams_enumerated: int,
    enumeration_ok: bool,
    enumeration_error: str = "",
    now: datetime,
) -> ModelDemotionAuditReport:
    """Audit every team's automations and return the fail-closed report.

    ``now`` is accepted for signature parity with the OMN-15373 guard and for
    forward compatibility with a dated exception registry. This ratchet has no
    exception registry by design: an accepted demoting automation would mean
    accepting that verified-Done work gets silently reverted, which is the whole
    defect. If one is ever needed it must be dated and owned, per the sibling.

    Pure function — no I/O.
    """
    del now  # no time-dependent rule in this ratchet; see docstring.

    control_failure = _positive_control_failure(
        team_probes,
        teams_enumerated=teams_enumerated,
        enumeration_ok=enumeration_ok,
        enumeration_error=enumeration_error,
    )
    if control_failure:
        return ModelDemotionAuditReport(
            passed=False,
            positive_control_ok=False,
            teams_enumerated=max(teams_enumerated, 0),
            teams_probed=len(team_probes),
            failure_reason=control_failure,
        )

    findings = [
        evaluate_demotion_risk(a) for probe in team_probes for a in probe.automations
    ]

    risk = sum(1 for f in findings if f.verdict is EnumDemotionVerdict.DEMOTION_RISK)
    unreadable = sum(1 for f in findings if f.verdict is EnumDemotionVerdict.UNREADABLE)
    clean = sum(1 for f in findings if f.verdict is EnumDemotionVerdict.CLEAN)

    passed = risk == 0 and unreadable == 0
    failure_reason = ""
    if not passed:
        failure_reason = (
            f"{risk} git automation(s) resolve to a non-completed state and "
            f"{unreadable} could not be read. Each one silently reverts verified-Done "
            "tickets on a bare, non-closing PR reference."
        )

    return ModelDemotionAuditReport(
        passed=passed,
        positive_control_ok=True,
        teams_enumerated=teams_enumerated,
        teams_probed=len(team_probes),
        findings=findings,
        demotion_risk_count=risk,
        unreadable_count=unreadable,
        clean_count=clean,
        failure_reason=failure_reason,
    )


def render_demotion_report(report: ModelDemotionAuditReport) -> str:
    """Render a human-readable summary for the scheduled runner's log."""
    lines: list[str] = [
        "=" * 72,
        "Linear git-automation DEMOTION ratchet (OMN-16536 AC#2)",
        "no GitAutomationState may resolve a completed ticket to a non-completed state",
        "=" * 72,
    ]

    # The control is printed before the findings, and on every run: a green
    # result whose control is unproven must never look like a clean bill.
    control = "PROVEN" if report.positive_control_ok else "NOT PROVEN"
    lines.append(
        f"positive control: {control} "
        f"(teams enumerated={report.teams_enumerated}, probed={report.teams_probed})"
    )
    lines.append("-" * 72)

    for f in sorted(report.findings, key=lambda x: (x.team_key, x.event)):
        marker = {
            EnumDemotionVerdict.CLEAN: "ok  ",
            EnumDemotionVerdict.DEMOTION_RISK: "FAIL",
            EnumDemotionVerdict.UNREADABLE: "FAIL",
        }[f.verdict]
        scope = "all-branches" if f.all_branches else "branch-scoped"
        lines.append(
            f"[{marker}] {f.team_key:<4} {f.event:<7} -> "
            f"{f.state_name or '<none>'} ({f.state_type or '<none>'}) [{scope}]"
        )
        if f.verdict is not EnumDemotionVerdict.CLEAN:
            lines.append(f"         {f.reason}")

    if not report.findings and report.positive_control_ok:
        lines.append(
            "no git automations configured on any enumerated team — the "
            "OMN-16536 AC#1 post-deletion state, confirmed against a proven probe."
        )

    lines.append("-" * 72)
    lines.append(
        f"clean={report.clean_count} demotion_risk={report.demotion_risk_count} "
        f"unreadable={report.unreadable_count}"
    )
    lines.append(f"RESULT: {'PASS' if report.passed else 'FAIL'}")
    if report.failure_reason:
        lines.append(f"REASON: {report.failure_reason}")
    lines.append("=" * 72)
    return "\n".join(lines)
