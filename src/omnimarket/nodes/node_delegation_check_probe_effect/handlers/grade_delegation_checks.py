# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure graders of the delegation checks C13, C14 and C17 over what a customer machine observed (OMN-26043).

An unproven zero is a failure: "no provider call was recorded" means nothing unless the instrument that would have
recorded one was running and saw the process that did the work, so a clause over an absence also grades its controls.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final
from urllib.parse import urlsplit

from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    CHAIN_TIER_ORDER,
    ModelDelegationCheckResponder,
)
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
            name="install_entry",
            ok=obs.entry_is_install_own,
            detail=f"the delegate ran through the install's own packaged onex ({obs.entry_path})"
            if obs.entry_is_install_own
            else f"the delegate did not run through the install's own onex: the customer machine resolved {obs.entry_path}",
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


def grade_chain_tier_order(
    responders: Sequence[ModelDelegationCheckResponder],
) -> ModelCheckClause:
    """C15: the responder chain is declared in tier order, local then OpenRouter then the Claude API."""
    declared = [r.tier for r in responders]
    rank = [CHAIN_TIER_ORDER.index(t) for t in declared]
    ok = bool(declared) and rank == sorted(rank)
    return ModelCheckClause(
        name="responder_chain_tier_order",
        ok=ok,
        detail=f"the chain is declared {' then '.join(declared)}"
        if ok
        else f"the chain is declared {' then '.join(declared) or 'empty'}, not {' then '.join(CHAIN_TIER_ORDER)}",
    )


#: Request fields that would pin a delegation to one model, backend or provider instead of leaving the choice to the chain.
PINNING_FIELDS: tuple[str, ...] = (
    "model",
    "model_id",
    "backend_id",
    "provider",
    "endpoint",
    "base_url",
)


def grade_no_model_named(
    payloads: Sequence[Mapping[str, object]],
) -> ModelCheckClause:
    """A delegation check hands the task to the chain: no request it sent names a model, backend or provider."""
    pinned = sorted({f for payload in payloads for f in PINNING_FIELDS if f in payload})
    ok = bool(payloads) and not pinned
    return ModelCheckClause(
        name="no_model_named_in_request",
        ok=ok,
        detail=f"{len(payloads)} delegation request(s) carry no model, backend or provider field"
        if ok
        else f"the delegation requests sent {len(payloads)} payload(s) and name {pinned or 'no payload at all'}",
    )


#: The provider words a receipt carries for each chain tier, read on the dev lane from delegation receipts (terminal payload
#: provider): the local tier stamps "local", the OpenRouter tier "openrouter", and the routing tier named "claude" is backed by a Gemini backend
#: and stamps "gemini". No dev receipt names "claude_api"; that is the chain tier's name, not a provider.
CHAIN_TIER_PROVIDER_WORDS: Final[Mapping[str, tuple[str, ...]]] = {
    "local": ("local",),
    "openrouter": ("openrouter",),
    "claude_api": ("gemini",),
}


def grade_serving_tier(
    receipt_provider: str | None, chain_tier: str | None
) -> ModelCheckClause:
    """The tier that served the run is the one the chain picked: the first available tier in the declared order."""
    ok = (
        receipt_provider is not None
        and chain_tier is not None
        and receipt_provider in CHAIN_TIER_PROVIDER_WORDS.get(chain_tier, ())
    )
    return ModelCheckClause(
        name="serving_tier_is_chain_pick",
        ok=ok,
        detail=f"the receipt names provider {receipt_provider}, the one the chain's {chain_tier} tier stamps"
        if ok
        else f"the receipt names provider {receipt_provider!r}; the chain picked {chain_tier!r} out of {list(CHAIN_TIER_ORDER)} ({CHAIN_TIER_PROVIDER_WORDS.get(chain_tier or '')} expected)",
    )


def grade_own_key_provider(
    receipt_provider: str | None, chosen_provider: str | None
) -> ModelCheckClause:
    """An own-key run is answered by the provider the tenant chose, never by a house or fallback provider."""
    ok = chosen_provider is not None and receipt_provider == chosen_provider
    return ModelCheckClause(
        name="only_chosen_provider_answered",
        ok=ok,
        detail=f"the receipt names the tenant's chosen provider {chosen_provider}"
        if ok
        else f"the receipt names provider {receipt_provider!r}; the tenant chose {chosen_provider!r}",
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
