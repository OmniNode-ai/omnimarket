# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure graders of the delegation checks C13, C14 and C17 over what a customer machine observed (OMN-26043).

An unproven zero is a failure: "no provider call was recorded" means nothing unless the instrument that would have
recorded one was running and saw the process that did the work, so a clause over an absence also grades its controls.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    ModelCheckClause,
    ModelDelegateObservation,
    ModelRoutingCollection,
    ModelSurfaceObservation,
)

NO_SUCH_COMMAND = "No such command"


def _hostport(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url if "//" in url else f"//{url}")
    return f"{parts.hostname}:{parts.port}" if parts.hostname and parts.port else None


def _zero_provider(obs: ModelDelegateObservation) -> ModelCheckClause:
    if not obs.egress_recorded:
        return ModelCheckClause(
            name="zero_provider",
            ok=False,
            detail="the egress instrument recorded nothing, so a zero would be unproven",
        )
    if obs.model_connects < 1 or not obs.token_counter_moved:
        return ModelCheckClause(
            name="zero_provider",
            ok=False,
            detail="the instrument saw no connect to the responder, or its token counter did not move: blind to the process that did the work",
        )
    if not obs.control_external_classified:
        return ModelCheckClause(
            name="zero_provider",
            ok=False,
            detail="a deliberate outbound connect was not classified external: the parser cannot see what it grades for",
        )
    if obs.egress_external:
        return ModelCheckClause(
            name="zero_provider",
            ok=False,
            detail=f"the session connected outside the responder: {', '.join(sorted(set(obs.egress_external)))}",
        )
    return ModelCheckClause(
        name="zero_provider",
        ok=True,
        detail="no connect beyond loopback and the chosen responder; both controls held",
    )


def grade_c13(
    obs: ModelDelegateObservation, *, responder_base: str, served_model: str
) -> tuple[ModelCheckClause, ...]:
    """C13: customer-local delegation on a no-checkout machine, through the responder the chain chose."""
    local = (
        obs.receipt_tier == "local"
        and obs.receipt_model == served_model
        and _hostport(obs.receipt_endpoint) == _hostport(responder_base)
        and bool(obs.attempt_tiers)
        and all(t == "local" for t in obs.attempt_tiers)
    )
    return (
        ModelCheckClause(
            name="no_checkout",
            ok=obs.no_checkout,
            detail="every distribution came from the package index"
            if obs.no_checkout
            else "an OmniNode distribution did not come from the package index",
        ),
        ModelCheckClause(
            name="three_files",
            ok=obs.rc == 0 and obs.three_files_agree,
            detail="result.txt, receipt.json and run.json exist and agree"
            if obs.rc == 0 and obs.three_files_agree
            else f"delegate rc {obs.rc}, files agree {obs.three_files_agree}",
        ),
        ModelCheckClause(
            name="local_model",
            ok=local,
            detail=f"the receipt names the chosen responder's model {served_model} on tier local"
            if local
            else f"receipt tier {obs.receipt_tier}, model {obs.receipt_model}, endpoint {obs.receipt_endpoint}, ladder {list(obs.attempt_tiers)} against responder {responder_base} serving {served_model}",
        ),
        _zero_provider(obs),
    )


def grade_c14(
    obs: ModelDelegateObservation, routing: ModelRoutingCollection
) -> tuple[ModelCheckClause, ...]:
    """C14: routing proven on the customer-local path (rows 1, 2 and 4)."""
    row1 = (
        bool(obs.attempt_tiers)
        and obs.attempt_tiers[0] == "local"
        and obs.receipt_tier == "local"
        and obs.accepted_last
        and not obs.escalated
        and obs.cost_usd == 0
    )
    row2 = (
        not routing.unbindable
        and routing.undeclared_control_classified_deleted
        and routing.closed_port_control_classified_unreachable
    )
    row4_instrument = obs.egress_recorded and obs.model_connects >= 1
    row4 = (
        routing.judge_endpoint_private
        and not routing.judge_needs_credential
        and routing.judge_serves_model
        and row4_instrument
        and not obs.egress_external
    )
    return (
        ModelCheckClause(
            name="no_checkout",
            ok=obs.no_checkout,
            detail="every distribution came from the package index"
            if obs.no_checkout
            else "an OmniNode distribution did not come from the package index",
        ),
        ModelCheckClause(
            name="row1_local_answer_ends_chain",
            ok=row1,
            detail="first and accepted rung local, accepted last, nothing escalated, no cost"
            if row1
            else f"ladder {list(obs.attempt_tiers)}, accepted last {obs.accepted_last}, escalated {obs.escalated}, cost {obs.cost_usd}",
        ),
        ModelCheckClause(
            name="row2_no_unbindable_tier",
            ok=row2,
            detail="every customer-visible task class binds on every tier; both controls held"
            if row2
            else f"unbindable {list(routing.unbindable)}; controls held {routing.undeclared_control_classified_deleted and routing.closed_port_control_classified_unreachable}",
        ),
        ModelCheckClause(
            name="row4_reviewer_unmetered",
            ok=row4,
            detail="the judge is a private endpoint with no credential and the recorded egress is empty"
            if row4
            else f"judge private {routing.judge_endpoint_private}, needs credential {routing.judge_needs_credential}, serves {routing.judge_serves_model}, egress instrument {row4_instrument}, external {list(obs.egress_external)}",
        ),
    )


def grade_c17(obs: ModelSurfaceObservation) -> tuple[ModelCheckClause, ...]:
    """C17: the customer surface (guides, clean install, parameter audit, broken command)."""
    install_ok = (
        obs.install_rc == 0 and obs.install_interpreter_matches and obs.help_answers
    )
    audit_ok = not obs.parameter_findings and obs.parameter_controls_caught
    return (
        ModelCheckClause(
            name="guides_lead_with_local_path",
            ok=obs.guides_lead_with_local,
            detail="the first delegating command of each guide's primary path is the local onex delegate"
            if obs.guides_lead_with_local
            else "a guide's primary path leads with a cloud command",
        ),
        ModelCheckClause(
            name="clean_install_named_interpreter",
            ok=install_ok,
            detail="the guide's own install line ran clean on the interpreter it names and onex delegate --help answers"
            if install_ok
            else f"install rc {obs.install_rc}, interpreter matches {obs.install_interpreter_matches}, help answers {obs.help_answers}",
        ),
        ModelCheckClause(
            name="parameter_audit",
            ok=audit_ok,
            detail="no internal-only parameter on a customer surface; every control caught"
            if audit_ok
            else f"{len(obs.parameter_findings)} finding(s), controls caught {obs.parameter_controls_caught}",
        ),
        grade_broken_command(obs),
    )


def grade_broken_command(obs: ModelSurfaceObservation) -> ModelCheckClause:
    """A broken ``onex.cli`` entry point answers with an error naming it, and removing it restores onex.

    The customer surface hides every group its declaration does not mark customer, so a planted entry point answers
    "No such command" however it is broken. The run plants it with the installed core's own operator switch on, and a case
    answered "No such command" is a run that did not take the switch, never a product that vanished a broken command.
    """
    if not obs.broken_cases:
        return ModelCheckClause(
            name="broken_command_named_error",
            ok=False,
            detail="no broken-command run was recorded",
        )
    hidden = [c.entry_point for c in obs.broken_cases if NO_SUCH_COMMAND in c.output]
    if hidden and not obs.broken_ran_with_switch:
        return ModelCheckClause(
            name="broken_command_named_error",
            ok=False,
            detail=f"{', '.join(hidden)} answered {NO_SUCH_COMMAND}: the customer surface hides an undeclared group, and the run did not set {obs.operator_switch_env}",
        )
    unnamed = [
        c.entry_point
        for c in obs.broken_cases
        if c.rc == 0 or c.entry_point not in c.output or NO_SUCH_COMMAND in c.output
    ]
    restored = obs.broken_before_rc == 0 and obs.broken_after_rc == 0
    ok = not unnamed and restored
    return ModelCheckClause(
        name="broken_command_named_error",
        ok=ok,
        detail="a broken entry point returns an error naming it, and removing it restores onex"
        if ok
        else f"unnamed or vanished: {unnamed}; onex before rc {obs.broken_before_rc}, after rc {obs.broken_after_rc}",
    )
