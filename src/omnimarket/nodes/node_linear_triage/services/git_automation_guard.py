# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fail-closed drift assertion over Linear git automations (OMN-15373).

Why this exists
---------------
On 2026-07-29 the Linear team ``Omninode`` carried a ``GitAutomationState`` with
``event=merge``, ``state=Done`` (type ``completed``) and ``targetBranch=null`` —
every branch, every repo. Any PR merge flipped every linked issue to Done in
~2-3 seconds. No ``Closes``/``Fixes``/``Resolves`` keyword was required: plain
branch-name linkage, or a bare body mention, was sufficient. 16 tickets were
falsely completed in one 4.5h window.

The setting was corrected by hand. That correction is **not a mechanism**: it is
a workspace setting a single API call can silently revert, with no alert and no
CI surface (``feedback_a_rule_is_not_a_mechanism``). This module is the
mechanism — a pure, scheduleable assertion that no git automation on any team
resolves to a ``completed``-type workflow state.

Fail-closed rules
-----------------
1. A ``completed``-type target is DRIFT. It mints Done with zero proof, which
   inverts the standing rule that Done is reachable only via ``dod_verify``.
2. An unresolvable target state is UNREADABLE, which FAILS. An unreadable check
   is not a passing check — the "optional check that silently skips == no
   check" class this guard exists to prevent.
3. An EMPTY automation set fails UNLESS the probe is independently proven to
   have read the workspace — i.e. it enumerated at least one team. A probe that
   returns nothing has otherwise proven nothing; it is indistinguishable from a
   broken query or a revoked token, and must never read as "no drift found".

   This rule was originally an unconditional failure. That was correct while a
   populated automation set was the expected shape, and became wrong on
   2026-08-27 when OMN-16536 AC#1 deleted all ten mappings across teams OMN, CON
   and JON — zero automations is now the *target* configuration, so an
   unconditional failure would have pinned this guard permanently red for the
   one reason that is not drift. The scepticism is not dropped, it is relocated:
   prove the probe (teams enumerated), then believe the emptiness. The mirror
   assertion lives in :mod:`git_automation_demotion_guard` (OMN-16536 AC#2),
   whose steady green is exactly this empty set.
4. An accepted exception must name an owner, a reason, and an ABSOLUTE expiry.
   A lapsed exception stops suppressing and the finding re-fails. There is no
   permanent suppression.

Pure logic — no I/O. The live probe lives in the handler.
"""

from __future__ import annotations

from datetime import datetime

from omnimarket.nodes.node_linear_triage.models.model_git_automation_guard import (
    COMPLETED_STATE_TYPE,
    EnumGitAutomationVerdict,
    ModelGitAutomationAuditReport,
    ModelGitAutomationException,
    ModelGitAutomationFinding,
    ModelGitAutomationState,
)


def _is_all_branches(automation: ModelGitAutomationState) -> bool:
    """True when the automation is scoped to no branch pattern (i.e. all of them)."""
    return not (automation.target_branch or "").strip()


def _live_exception(
    automation: ModelGitAutomationState,
    exceptions: dict[str, ModelGitAutomationException],
    now: datetime,
) -> ModelGitAutomationException | None:
    """Return the registered exception for this automation if it has not lapsed."""
    exc = exceptions.get(automation.automation_id)
    if exc is None:
        return None
    if exc.expires_at <= now:
        return None
    return exc


def evaluate_git_automation(
    automation: ModelGitAutomationState,
    *,
    exceptions: dict[str, ModelGitAutomationException] | None = None,
    now: datetime,
) -> ModelGitAutomationFinding:
    """Return the fail-closed verdict for one git automation. Pure — no I/O."""
    exceptions = exceptions or {}
    all_branches = _is_all_branches(automation)

    if not automation.state_readable:
        return ModelGitAutomationFinding(
            team_key=automation.team_key,
            automation_id=automation.automation_id,
            event=automation.event,
            state_name=automation.state_name,
            state_type=automation.state_type,
            verdict=EnumGitAutomationVerdict.UNREADABLE,
            reason=(
                f"the target workflow state for the {automation.event!r} automation "
                "on team "
                f"{automation.team_key!r} could not be read. An unreadable check is "
                "not a passing check — failing closed (OMN-15373)."
            ),
            all_branches=all_branches,
        )

    if automation.state_type.strip().lower() != COMPLETED_STATE_TYPE:
        return ModelGitAutomationFinding(
            team_key=automation.team_key,
            automation_id=automation.automation_id,
            event=automation.event,
            state_name=automation.state_name,
            state_type=automation.state_type,
            verdict=EnumGitAutomationVerdict.CLEAN,
            reason=(
                f"{automation.event!r} resolves to {automation.state_name or '<none>'!r} "
                f"(type {automation.state_type or '<none>'!r}) — not a completed-type "
                "state, so firing it cannot mint a Done."
            ),
            all_branches=all_branches,
        )

    scope = (
        "EVERY branch of EVERY repo"
        if all_branches
        else f"branch pattern {automation.target_branch!r}"
    )
    drift_reason = (
        f"team {automation.team_key!r} git automation {automation.event!r} resolves to "
        f"{automation.state_name!r}, whose type is {COMPLETED_STATE_TYPE!r}, on {scope}. "
        "Firing it writes Done with no closing keyword, no dod_verify receipt and no "
        "evidence gate of any kind — the OMN-15373 defect (16 false-Dones in one 4.5h "
        "window). Done is reachable only via dod_verify with durable evidence; a merge "
        "is code-only/receipt-bound at best. Retarget this automation to a "
        "non-completed state (e.g. In Review)."
    )

    exc = _live_exception(automation, exceptions, now)
    if exc is not None:
        return ModelGitAutomationFinding(
            team_key=automation.team_key,
            automation_id=automation.automation_id,
            event=automation.event,
            state_name=automation.state_name,
            state_type=automation.state_type,
            verdict=EnumGitAutomationVerdict.ACCEPTED_EXCEPTION,
            reason=(
                f"{drift_reason} ACCEPTED until {exc.expires_at.isoformat()} by "
                f"{exc.owner!r}: {exc.reason}"
            ),
            all_branches=all_branches,
        )

    return ModelGitAutomationFinding(
        team_key=automation.team_key,
        automation_id=automation.automation_id,
        event=automation.event,
        state_name=automation.state_name,
        state_type=automation.state_type,
        verdict=EnumGitAutomationVerdict.DRIFT,
        reason=drift_reason,
        all_branches=all_branches,
    )


def audit_git_automations(
    automations: list[ModelGitAutomationState],
    *,
    exceptions: list[ModelGitAutomationException] | None = None,
    now: datetime,
    probe_ok: bool = True,
    probe_error: str = "",
    teams_enumerated: int = 0,
) -> ModelGitAutomationAuditReport:
    """Audit every automation and return the fail-closed aggregate report.

    ``passed`` is True ONLY when the probe succeeded, the probe is proven to
    have read the workspace, and no finding is DRIFT or UNREADABLE.

    ``teams_enumerated`` is that proof. An empty automation set passes only when
    at least one team was enumerated — which is the post-OMN-16536-AC#1 steady
    state (all ten mappings deleted, three teams still enumerable). Zero teams
    enumerated is indistinguishable from a revoked token or a renamed field and
    still FAILS, as does a failed probe. It defaults to ``0`` so that any caller
    that has not been taught to supply the control keeps the old, strictly
    fail-closed behaviour rather than silently gaining a pass.

    Pure function — no I/O.
    """
    if not probe_ok:
        return ModelGitAutomationAuditReport(
            passed=False,
            failure_reason=(
                "the Linear git-automation probe failed, so no assertion could be "
                f"made. Failing closed (OMN-15373). Probe error: {probe_error or 'unknown'}"
            ),
        )

    if not automations and teams_enumerated <= 0:
        return ModelGitAutomationAuditReport(
            passed=False,
            failure_reason=(
                "the Linear git-automation probe returned ZERO automations AND "
                "enumerated ZERO teams, so it cannot be shown to have read the "
                "workspace at all. That is indistinguishable from a broken query or a "
                "revoked token, so it is never read as 'no drift found'. Failing "
                "closed (OMN-15373)."
            ),
        )

    by_id = {e.automation_id: e for e in (exceptions or [])}
    findings = [
        evaluate_git_automation(a, exceptions=by_id, now=now) for a in automations
    ]

    drift = sum(1 for f in findings if f.verdict is EnumGitAutomationVerdict.DRIFT)
    unreadable = sum(
        1 for f in findings if f.verdict is EnumGitAutomationVerdict.UNREADABLE
    )
    accepted = sum(
        1 for f in findings if f.verdict is EnumGitAutomationVerdict.ACCEPTED_EXCEPTION
    )
    clean = sum(1 for f in findings if f.verdict is EnumGitAutomationVerdict.CLEAN)

    passed = drift == 0 and unreadable == 0
    failure_reason = ""
    if not passed:
        failure_reason = (
            f"{drift} git automation(s) resolve to a completed-type state and "
            f"{unreadable} could not be read. Each one mints Done with zero proof."
        )

    return ModelGitAutomationAuditReport(
        passed=passed,
        findings=findings,
        drift_count=drift,
        accepted_count=accepted,
        unreadable_count=unreadable,
        clean_count=clean,
        failure_reason=failure_reason,
    )


def render_report(report: ModelGitAutomationAuditReport) -> str:
    """Render a human-readable summary for the scheduled runner's log output."""
    lines: list[str] = [
        "=" * 72,
        "Linear git-automation drift guard (OMN-15373)",
        "=" * 72,
    ]
    for f in sorted(report.findings, key=lambda x: (x.team_key, x.event)):
        marker = {
            EnumGitAutomationVerdict.CLEAN: "ok  ",
            EnumGitAutomationVerdict.DRIFT: "FAIL",
            EnumGitAutomationVerdict.ACCEPTED_EXCEPTION: "warn",
            EnumGitAutomationVerdict.UNREADABLE: "FAIL",
        }[f.verdict]
        scope = "all-branches" if f.all_branches else "branch-scoped"
        lines.append(
            f"[{marker}] {f.team_key:<4} {f.event:<7} -> "
            f"{f.state_name or '<none>'} ({f.state_type or '<none>'}) [{scope}]"
        )
        if f.verdict is not EnumGitAutomationVerdict.CLEAN:
            lines.append(f"         {f.reason}")
    lines.append("-" * 72)
    lines.append(
        f"clean={report.clean_count} drift={report.drift_count} "
        f"accepted={report.accepted_count} unreadable={report.unreadable_count}"
    )
    lines.append(f"RESULT: {'PASS' if report.passed else 'FAIL'}")
    if report.failure_reason:
        lines.append(f"REASON: {report.failure_reason}")
    lines.append("=" * 72)
    return "\n".join(lines)


__all__ = ["audit_git_automations", "evaluate_git_automation", "render_report"]
