# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input-model fit: one declared model may not serve two message categories (OMN-17888).

WHAT THIS CATCHES, AND WHY THE EXISTING GATES CANNOT
----------------------------------------------------
``subscriber_dispatcher_resolution`` (OMN-16939) asks whether a declared
``subscribe_topic`` reaches a REGISTERED DISPATCHER for its own ``(category, message
type)``. ``contract_topic_category`` (OMN-14605) asks whether one ``handler_routing``
entry's topics span two categories. Both are route-shaped questions, and a contract can
answer both correctly while still being false about the only thing that decides whether a
message survives: the MODEL the handler on the other end is handed.

``node_redeploy_deploy_effect`` answered both correctly. Its completion-event entry
declared ``message_category: event`` and resolved cleanly, and no entry spanned two
categories. It also declared ``input_model: ModelDeployPublishCommand`` — the model of the
COMMAND the node consumes on its other topic — for the deploy agent's completion EVENT. A
command and an event are different wire shapes, so that declaration cannot be true of any
handler. Live consequence on the .201 dev lane, read off
``onex.dlq.omnibase-infra.events.v1`` on 2026-09-16T12:35Z: **72 dead-lettered completion
events** between 2026-09-11T13:12:41Z and 2026-09-16T11:28:20Z, each carrying
``ValidationError: 12 validation errors for ModelDeployPublishCommand``, while every
routing gate reported the topic resolved and the consumer group read healthy.

THE RULE
--------
For one handler class inside one contract, a single EFFECTIVE model may not serve more
than one ``message_category``.

WHICH MODEL IS "EFFECTIVE" — THE OMN-18568 CORRECTION
-----------------------------------------------------
The rule above was right from the start. What was wrong was the answer to *which model
this entry actually hands the handler*, and that answer belongs to the dispatcher, not to
this file. Until OMN-18568 this gate read ``entry["input_model"]`` and skipped every entry
that did not declare one. Measured over ``src/omnimarket`` (403 contracts) on
2026-09-17: **19** entries declare entry-level ``input_model``, **163** declare
``event_model``, and **373** declare neither. The gate was blind to 163 of the 182
declarations it exists to check, and resolved no fallback at all — so it printed
``OK: 403 contracts scanned, 0 input-model/category conflicts`` while
``node_swarm_fanout_orchestrator`` dead-lettered 120 delegation-escalation events in 21
minutes on the ``.201`` stability lane, each
``ValidationError: 14 validation errors for ModelSwarmFanoutRequest``.

The dispatcher resolves an entry's model in exactly two steps
(``omnibase_infra/runtime/auto_wiring/handler_wiring.py``):

1. a contract-declared ``event_model`` becomes the ``PayloadTypeMatcher`` target;
2. with no ``event_model``, ``_make_dispatch_callback`` takes its ``event_model is None``
   branch (``:919``) and resolves the coercion target from the HANDLER'S OWN ``handle()``
   ANNOTATION via ``_resolve_def_b_input_model_type`` (``:970``, resolver at ``:1391``) —
   preferring ``handle_async`` when the class declares it, and coercing NOTHING when the
   handler is envelope-annotated (``:963``), union-annotated, ``dict``-annotated or
   var-positional.

The contract-level ``input_model`` is **not** consulted on that path. Resolving to it
instead is not a harmless approximation: over the same 403 contracts it reports **six**
offenders where the dispatcher-faithful resolution reports **three**, and five of those
six are correct contracts — including ``node_redeploy_orchestrator``, which sits on the
prod-promotion path and whose handler takes a ``ModelEventEnvelope`` and branches on
``event_type``. A gate that names five healthy contracts to find one sick one is a broken
probe rather than a finding, which is the same trap this module's "WHAT THIS DELIBERATELY
DOES NOT DO" note below was written to avoid.

So this gate now imports the handler class and reads its signature. That costs it the
no-import property it used to have. The trade is deliberate: an import-free gate can only
guess at the fallback, and the guess is wrong five times out of six. The mirror is pinned
against the runtime's own two helpers in
``tests/validators/test_routing_input_model_fit_effective_model.py`` so the gate and the
dispatcher cannot drift apart silently.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not try to prove, repo-wide, that a handler BRANCHES on each event it declares.
Measured over ``src/omnimarket`` on 2026-09-16: 403 contracts, 16 of which route more than
one message category to a single handler class, and only two express the choice as an
``event_name ==`` comparison — the rest use topic matching, payload-type dispatch, or
separate typed def-B signatures. A repo-wide sweep for any one of those idioms reports the
other fourteen as offenders, which is a broken probe rather than a finding. The branch half
is asserted per handler, beside the handler, in
``tests/test_deploy_publish_monitor_rebuild_completed_dispatch.py`` and
``tests/test_redeploy_orchestrator_dispatch_resolution.py``.

Run it directly::

    uv run python -m omnimarket.validators.routing_input_model_fit src/omnimarket
"""

from __future__ import annotations

import functools
import importlib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import yaml
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "PEER_FENCED_CONTRACTS",
    "ModelRoutingInputModelFinding",
    "effective_entry_model",
    "findings_for_contract_tree",
    "main",
    "peer_fence_key_for",
]

_CONTRACT_FILENAME = "contract.yaml"

# A contract carrying this defect whose fix is owned by a DIFFERENT ticket, pinned in the
# shape ``no_baseline_refreeze.PEER_FENCED_BASELINES`` uses: named, reasoned, and bound to
# the ticket that DELETES the row. This is not an exemption list and it cannot grow into
# one quietly -- ``main`` fails when a pinned contract comes back CLEAN, so a row that has
# been fixed must be removed here in the same change, and any contract not listed here is
# a hard failure.
#
# node_content_ingestion_effect routes both ``onex.cmd.omnimarket.content-ingestion-start.v1``
# and ``onex.evt.omnimarket.content-discovered.v1`` to ``HandlerContentIngestion``, whose
# ``handle(self, payload: ModelIngestionRequest)`` can serve only the first. Neither entry
# is spurious: the command topic is the contract's own declared
# ``runtime_dispatch.command_topic`` (the CLI/skill dispatcher is its producer, OMN-18013),
# and the event topic is the node's declared primary input -- its
# ``subscribe_topic_metadata`` names the crawler's ``ModelContentDiscoveredEvent``, and the
# node description says it consumes exactly that. Removing either one would be a false
# statement in the other direction, and declaring the real ``event_model`` without a handler
# branch would trade a validation error for an unhandled one. The handler is what has to
# change, and that rearchitecture is OMN-14613.
PEER_FENCED_CONTRACTS: dict[str, str] = {
    "src/omnimarket/nodes/node_content_ingestion_effect/contract.yaml": (
        "OMN-14613 — HandlerContentIngestion takes ModelIngestionRequest only, while the "
        "contract legitimately declares both a CLI command topic (runtime_dispatch."
        "command_topic) and the crawler's content-discovered event as its primary input. "
        "Fixing it means giving the handler an event path, not editing the contract."
    ),
}


class ModelRoutingInputModelFinding(BaseModel):
    """One handler whose effective model serves more than one message category."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: str = Field(
        ..., description="Contract ``name`` that declares the entries."
    )
    contract_path: str = Field(..., description="Path of the offending contract file.")
    handler: str = Field(..., description="Handler class both entries route to.")
    effective_model: str = Field(
        ...,
        description="The one model the dispatcher resolves for every listed topic.",
    )
    resolved_from: str = Field(
        ...,
        description="How the model was resolved: 'event_model', 'input_model' or "
        "'handle_signature'.",
    )
    categories: tuple[str, ...] = Field(
        ..., description="The declared message categories, sorted."
    )
    topics: tuple[str, ...] = Field(
        ..., description="The topics carrying that model, sorted."
    )
    peer_fenced_key: str | None = Field(
        default=None,
        description="The PEER_FENCED_CONTRACTS key this finding is pinned to, if any.",
    )

    def render(self) -> str:
        """One human-readable line naming the contract, handler, model and topics."""
        return (
            f"{self.contract_path}: handler {self.handler!r} resolves to model "
            f"{self.effective_model!r} (via {self.resolved_from}) for message categories "
            f"{', '.join(self.categories)} on topics {', '.join(self.topics)}. "
            "One wire model cannot be both; give each category the model its own "
            "producer really publishes, and branch on it in the handler."
        )


def _named(value: object) -> str | None:
    """The ``name`` of a contract sub-block written either as a mapping or a string."""
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else None
    if isinstance(value, str):
        return value
    return None


def _module_of(value: object) -> str | None:
    """The ``module`` of a contract sub-block, when it is written as a mapping."""
    if isinstance(value, dict):
        module = value.get("module")
        return module if isinstance(module, str) else None
    return None


@functools.cache
def _runtime_resolvers() -> tuple[
    Callable[[object], bool], Callable[[object], type[BaseModel] | None]
]:
    """The dispatcher's own two resolution helpers, imported once.

    Imported lazily and by name so a failure to reach them is a LOUD refusal rather than
    a gate that quietly answers a different question than the runtime does.
    """
    try:
        from omnibase_infra.runtime.auto_wiring.handler_wiring import (
            _handler_declares_typed_event_envelope,
            _resolve_def_b_input_model_type,
        )
    except ImportError as exc:  # pragma: no cover - environment failure
        raise ValueError(
            "routing_input_model_fit cannot import the runtime's model-resolution "
            "helpers from omnibase_infra.runtime.auto_wiring.handler_wiring, so it "
            f"cannot state how the dispatcher would resolve any entry: {exc}"
        ) from exc
    declares_envelope = cast(
        "Callable[[object], bool]", _handler_declares_typed_event_envelope
    )
    resolve_def_b = cast(
        "Callable[[object], type[BaseModel] | None]", _resolve_def_b_input_model_type
    )
    return declares_envelope, resolve_def_b


def _handle_method(handler_class: type) -> object | None:
    """The method the dispatcher would actually invoke, ``handle_async`` preferred."""
    for attribute in ("handle_async", "handle"):
        method: object | None = getattr(handler_class, attribute, None)
        if callable(method):
            return method
    return None


def _signature_model_name(handler_name: str, handler_module: str) -> str | None:
    """The model the dispatcher would coerce into for a handler declaring no event_model.

    ``None`` when the dispatcher coerces nothing — an envelope-annotated handler (which
    receives a materialized ``ModelEventEnvelope``), a union or ``dict`` annotation, a
    var-positional signature, or a class exposing no ``handle``. Such an entry cannot
    carry a model/category conflict because it carries no model.
    """
    declares_envelope, resolve_def_b = _runtime_resolvers()
    try:
        handler_class = getattr(importlib.import_module(handler_module), handler_name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(
            f"handler {handler_module}.{handler_name} cannot be imported, so this gate "
            f"cannot state which model the dispatcher would hand it: {exc}"
        ) from exc

    method = _handle_method(handler_class)
    if method is None or declares_envelope(method):
        return None
    target = resolve_def_b(method)
    return None if target is None else target.__name__


def effective_entry_model(entry: dict[str, object]) -> str | None:
    """The model the dispatcher resolves for one ``handler_routing`` entry.

    Mirrors ``_make_dispatch_callback``: a declared ``event_model`` wins; otherwise the
    handler's own ``handle()`` annotation decides, and an entry the dispatcher would not
    coerce at all resolves to ``None``. ``input_model`` is honoured between the two
    because 19 entries in this repo still spell the per-entry model that way and the
    OMN-17888 rule was written against them.
    """
    return _effective_entry_model_with_source(entry)[0]


def _effective_entry_model_with_source(
    entry: dict[str, object],
) -> tuple[str | None, str]:
    event_model = _named(entry.get("event_model"))
    if event_model:
        return event_model, "event_model"
    input_model = _named(entry.get("input_model"))
    if input_model:
        return input_model, "input_model"
    handler_name = _named(entry.get("handler"))
    handler_module = _module_of(entry.get("handler"))
    if not (handler_name and handler_module):
        return None, "handle_signature"
    return _signature_model_name(handler_name, handler_module), "handle_signature"


def peer_fence_key_for(contract_path: Path | str) -> str | None:
    """The ``PEER_FENCED_CONTRACTS`` key this contract is pinned under, if any."""
    posix = Path(contract_path).as_posix()
    for key in PEER_FENCED_CONTRACTS:
        if posix == key or posix.endswith(f"/{key}"):
            return key
    return None


def _findings_for_contract(
    contract_path: Path,
) -> list[ModelRoutingInputModelFinding]:
    """Every offending (handler, effective model) pair declared by one contract file."""
    document = yaml.safe_load(contract_path.read_text())
    if not isinstance(document, dict):
        return []
    routing = document.get("handler_routing")
    if not isinstance(routing, dict):
        return []
    entries = routing.get("handlers")
    if not isinstance(entries, list):
        return []

    # (handler class, handler module, effective model) -> category -> topics
    seen: dict[tuple[str, str | None, str], dict[str, set[str]]] = {}
    sources: dict[tuple[str, str | None, str], set[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        handler_name = _named(entry.get("handler"))
        category = entry.get("message_category")
        topic = entry.get("topic")
        if not (handler_name and isinstance(category, str) and isinstance(topic, str)):
            continue
        effective_model, source = _effective_entry_model_with_source(entry)
        if not effective_model:
            # The dispatcher coerces nothing for this entry, so it carries no model to
            # conflict. Not a silent skip: this is the envelope / untyped-handler shape.
            continue
        key = (handler_name, _module_of(entry.get("handler")), effective_model)
        seen.setdefault(key, {}).setdefault(category.lower(), set()).add(topic)
        sources.setdefault(key, set()).add(source)

    contract_name = document.get("name")
    fence_key = peer_fence_key_for(contract_path)
    findings: list[ModelRoutingInputModelFinding] = []
    for (handler_name, _handler_module, effective_model), by_category in seen.items():
        if len(by_category) < 2:
            continue
        topics = sorted({topic for topics in by_category.values() for topic in topics})
        findings.append(
            ModelRoutingInputModelFinding(
                contract=contract_name
                if isinstance(contract_name, str)
                else "<unnamed>",
                contract_path=str(contract_path),
                handler=handler_name,
                effective_model=effective_model,
                resolved_from="+".join(
                    sorted(sources[(handler_name, _handler_module, effective_model)])
                ),
                categories=tuple(sorted(by_category)),
                topics=tuple(topics),
                peer_fenced_key=fence_key,
            )
        )
    return findings


def findings_for_contract_tree(
    scan_root: Path,
) -> tuple[tuple[ModelRoutingInputModelFinding, ...], int]:
    """Scan every ``contract.yaml`` under ``scan_root``.

    Returns the findings and the number of contracts read. The count is returned rather
    than logged because an empty finding list and a collapsed discovery are the same
    output otherwise, and a gate that cannot tell them apart reports a broken scan as a
    clean tree (rule 16).
    """
    findings: list[ModelRoutingInputModelFinding] = []
    contract_count = 0
    for contract_path in sorted(scan_root.rglob(_CONTRACT_FILENAME)):
        try:
            contract_findings = _findings_for_contract(contract_path)
        except yaml.YAMLError as exc:
            raise ValueError(
                f"{contract_path}: contract is not parseable YAML, so this gate cannot "
                f"state whether it is clean: {exc}"
            ) from exc
        except ValueError as exc:
            raise ValueError(f"{contract_path}: {exc}") from exc
        contract_count += 1
        findings.extend(contract_findings)
    return tuple(findings), contract_count


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: exit 1 on any finding, 2 when the scan itself is unusable."""
    args = list(sys.argv[1:] if argv is None else argv)
    scan_root = Path(args[0]) if args else Path("src/omnimarket")
    if not scan_root.is_dir():
        sys.stderr.write(
            f"[routing-input-model-fit] FAIL: scan root {scan_root} is not a "
            "directory; failing closed rather than reporting a clean tree.\n"
        )
        return 2

    try:
        findings, contract_count = findings_for_contract_tree(scan_root)
    except ValueError as exc:
        sys.stderr.write(f"[routing-input-model-fit] FAIL: {exc}\n")
        return 2

    if contract_count == 0:
        sys.stderr.write(
            f"[routing-input-model-fit] FAIL: no {_CONTRACT_FILENAME} found under "
            f"{scan_root}; a zero-contract scan is a broken scan, not a clean tree.\n"
        )
        return 2

    unpinned = [f for f in findings if f.peer_fenced_key is None]
    pinned_hit = {f.peer_fenced_key for f in findings if f.peer_fenced_key}

    # A pin whose contract is clean is an exemption with nothing left to exempt, and is a
    # one-line edit away from re-authorizing the class. Scoped to the tree actually
    # scanned, so a single-node scan does not demand rows it never looked at.
    scanned_pins = {
        key
        for key in PEER_FENCED_CONTRACTS
        if any(
            peer_fence_key_for(path) == key
            for path in scan_root.rglob(_CONTRACT_FILENAME)
        )
    }
    stale = sorted(scanned_pins - pinned_hit)

    for finding in findings:
        if finding.peer_fenced_key is not None:
            sys.stderr.write(
                f"[routing-input-model-fit] PINNED ({PEER_FENCED_CONTRACTS[finding.peer_fenced_key]}):\n"
                f"  - {finding.render()}\n"
            )

    if stale:
        sys.stderr.write(
            "[routing-input-model-fit] FAIL: these contracts are pinned in "
            "PEER_FENCED_CONTRACTS but are now CLEAN. Delete the pin in the same change "
            "that fixed them:\n"
        )
        for key in stale:
            sys.stderr.write(f"  - {key}\n")
        return 1

    if unpinned:
        sys.stderr.write(
            f"[routing-input-model-fit] FAIL: {len(unpinned)} handler(s) resolve one "
            f"model under more than one message category "
            f"({contract_count} contracts scanned):\n"
        )
        for finding in unpinned:
            sys.stderr.write(f"  - {finding.render()}\n")
        return 1

    sys.stderr.write(
        f"[routing-input-model-fit] OK: {contract_count} contracts scanned, "
        f"0 unpinned model/category conflicts ({len(pinned_hit)} pinned).\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
