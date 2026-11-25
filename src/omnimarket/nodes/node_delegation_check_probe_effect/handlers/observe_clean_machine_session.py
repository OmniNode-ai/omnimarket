# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the in-machine driver's document means for C13 and C14, as the observations the pure graders read (OMN-26046).

The driver (``clean_machine_routing_driver``) measures and never grades. These functions only read its document: they hold no
host, model or store value of their own. The responder base is the settings row's, handed in by the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    ModelDelegateObservation,
    ModelRoutingCollection,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    CollectorAbsentError,
)

PRODUCT_DISTS = ("omnibase-core", "omnibase-infra", "omnimarket")
PRODUCT_MODULES = ("omnimarket", "omnibase_infra", "omnibase_core")
LOCAL_TIER = "local"
BINDABLE = "bindable"
DELETED_BACKEND = "deleted_backend"
UNREACHABLE = "unreachable"


def _hostport(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url if "//" in url else f"//{url}")
    return f"{parts.hostname}:{parts.port}" if parts.hostname and parts.port else None


def _entries(raw: Any) -> list[Mapping[str, Any]]:
    return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


def _no_checkout(document: Mapping[str, Any]) -> bool:
    """Every OmniNode distribution came from the package index: no direct URL, in site-packages, no git tree above it."""
    facts = document.get("distributions") or {}
    for name in PRODUCT_DISTS:
        entry = facts.get(name) or {}
        if not entry.get("installed") or entry.get("direct_url"):
            return False
    for name in PRODUCT_MODULES:
        entry = facts.get(name) or {}
        if not entry.get("in_site_packages") or entry.get("git_ancestor"):
            return False
    return True


def delegate_observation_from_document(
    document: Mapping[str, Any], *, responder_base: str
) -> ModelDelegateObservation:
    """What the one real ``onex delegate`` run on the clean machine showed. Pure."""
    run = document.get("run") or {}
    receipt = run.get("receipt") or {}
    result = run.get("receipt_result") or {}
    attempts = [a for a in result.get("attempts") or [] if isinstance(a, dict)]
    last = attempts[-1] if attempts else {}
    metrics = result.get("metrics") or {}
    egress = _entries(run.get("egress"))
    responder = _hostport(responder_base)
    entry = document.get("entry") or {}
    resolved = entry.get("resolved")
    exit_code = run.get("exit_code")
    return ModelDelegateObservation(
        rc=exit_code if isinstance(exit_code, int) else -1,
        no_checkout=_no_checkout(document),
        three_files_agree=bool(run.get("three_files_agree")),
        receipt_tier=last.get("tier"),
        receipt_model=result.get("model_name") or last.get("model_id"),
        receipt_endpoint=receipt.get("endpoint") or result.get("provider"),
        attempt_tiers=tuple(str(a.get("tier")) for a in attempts),
        accepted_last=bool(attempts) and last.get("acceptance_decision") == "accept",
        escalated=int(result.get("escalation_count") or 0) > 0,
        cost_usd=float(metrics.get("cost_usd") or 0.0),
        egress_recorded=bool(egress),
        egress_external=tuple(
            str(e.get("target")) for e in egress if not e.get("private")
        ),
        model_connects=sum(
            1
            for e in egress
            if e.get("method") == "POST"
            and _hostport(str(e.get("target"))) == responder
        ),
        token_counter_moved=bool(metrics.get("input_tokens"))
        and bool(metrics.get("output_tokens")),
        control_external_classified=any(
            not e.get("private") for e in _entries(run.get("control_egress"))
        ),
        entry_path=resolved,
        entry_is_install_own=bool(resolved) and resolved == entry.get("install_own"),
    )


def _unbindable(routing: Mapping[str, Any]) -> tuple[str, ...]:
    """Every reason a customer-visible task class has a tier no backend can be bound for. An empty walk is a finding."""
    findings: list[str] = []
    authority = routing.get("public_task_classes")
    contract_public = routing.get("public_task_classes_from_contract_yaml")
    walk = routing.get("walk")
    if not isinstance(authority, list) or not authority:
        return ("the task-class authority yielded no customer-visible classes",)
    if not isinstance(contract_public, list) or sorted(contract_public) != sorted(
        authority
    ):
        findings.append(
            "the authority's public classes disagree with the shipped contract"
        )
    if not isinstance(walk, dict):
        return (*findings, "the routing walk is absent")
    missing = sorted(set(authority) - set(walk))
    if missing:
        findings.append(f"classes absent from the walk: {missing}")
    backends = routing.get("backends") or {}
    for task_class in sorted(walk):
        tiers = walk[task_class]
        if not isinstance(tiers, list) or not tiers:
            findings.append(f"{task_class}: resolved an empty tier order")
            continue
        if tiers[0].get("tier") != LOCAL_TIER:
            findings.append(f"{task_class}: no local first rung")
        for tier in tiers:
            name = tier.get("tier")
            if not tier.get("declared", True):
                findings.append(f"{task_class}/{name}: tier undeclared")
                continue
            candidates = tier.get("candidates") or []
            if not candidates:
                findings.append(
                    f"{task_class}/{name}: no model in the tier serves the class"
                )
                continue
            for backend_id in candidates:
                state = (backends.get(backend_id) or {}).get(
                    "classification", DELETED_BACKEND
                )
                if state != BINDABLE:
                    findings.append(
                        f"{task_class}/{name}: backend {backend_id} is {state}"
                    )
    return tuple(findings)


def routing_from_document(document: Mapping[str, Any]) -> ModelRoutingCollection:
    """What the routing walk of C14 collected on the installed product. Raises when the walk itself did not run."""
    if document.get("install_failed"):
        # The install did not complete, so no walk could run: every row reads unproven, never an absent instrument.
        return ModelRoutingCollection(unbindable=(str(document["install_failed"]),))
    routing = document.get("routing") or {}
    if not routing or routing.get("collect_error"):
        raise CollectorAbsentError(
            "routing_walk",
            f"the in-machine routing walk did not run: {routing.get('collect_error', 'no routing in the driver document')}",
        )
    controls = {
        c.get("name"): c.get("classification")
        for c in _entries(routing.get("controls"))
    }
    judge = routing.get("judge") or {}
    judged = not judge.get("error")
    return ModelRoutingCollection(
        unbindable=_unbindable(routing),
        undeclared_control_classified_deleted=controls.get("deleted_backend_control")
        == DELETED_BACKEND,
        closed_port_control_classified_unreachable=controls.get(
            "unreachable_endpoint_control"
        )
        == UNREACHABLE,
        judge_endpoint_private=judged and bool(judge.get("endpoint_private")),
        judge_needs_credential=(not judged) or bool(judge.get("requires_credential")),
        judge_serves_model=judged and judge.get("classification") == BINDABLE,
    )
