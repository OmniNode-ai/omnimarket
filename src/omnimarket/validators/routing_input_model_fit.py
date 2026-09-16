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
For one handler class inside one contract, a single ``input_model`` may not be declared
under more than one ``message_category``. That is decidable from the contract text alone,
needs no import and no runtime, and is idiom-independent.

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

import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ModelRoutingInputModelFinding",
    "findings_for_contract_tree",
    "main",
]

_CONTRACT_FILENAME = "contract.yaml"


class ModelRoutingInputModelFinding(BaseModel):
    """One handler declaring a single input model under more than one category."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: str = Field(
        ..., description="Contract ``name`` that declares the entries."
    )
    contract_path: str = Field(..., description="Path of the offending contract file.")
    handler: str = Field(..., description="Handler class both entries route to.")
    input_model: str = Field(..., description="The one model declared for both.")
    categories: tuple[str, ...] = Field(
        ..., description="The declared message categories, sorted."
    )
    topics: tuple[str, ...] = Field(
        ..., description="The topics carrying that model, sorted."
    )

    def render(self) -> str:
        """One human-readable line naming the contract, handler, model and topics."""
        return (
            f"{self.contract_path}: handler {self.handler!r} declares input_model "
            f"{self.input_model!r} for message categories "
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


def _findings_for_contract(
    contract_path: Path,
) -> list[ModelRoutingInputModelFinding]:
    """Every offending (handler, input_model) pair declared by one contract file."""
    document = yaml.safe_load(contract_path.read_text())
    if not isinstance(document, dict):
        return []
    routing = document.get("handler_routing")
    if not isinstance(routing, dict):
        return []
    entries = routing.get("handlers")
    if not isinstance(entries, list):
        return []

    # (handler class, handler module, input model) -> category -> topics
    seen: dict[tuple[str, str | None, str], dict[str, set[str]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        handler_name = _named(entry.get("handler"))
        input_model = _named(entry.get("input_model"))
        category = entry.get("message_category")
        topic = entry.get("topic")
        if not (
            handler_name
            and input_model
            and isinstance(category, str)
            and isinstance(topic, str)
        ):
            continue
        key = (handler_name, _module_of(entry.get("handler")), input_model)
        seen.setdefault(key, {}).setdefault(category.lower(), set()).add(topic)

    contract_name = document.get("name")
    findings: list[ModelRoutingInputModelFinding] = []
    for (handler_name, _handler_module, input_model), by_category in seen.items():
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
                input_model=input_model,
                categories=tuple(sorted(by_category)),
                topics=tuple(topics),
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

    findings, contract_count = findings_for_contract_tree(scan_root)
    if contract_count == 0:
        sys.stderr.write(
            f"[routing-input-model-fit] FAIL: no {_CONTRACT_FILENAME} found under "
            f"{scan_root}; a zero-contract scan is a broken scan, not a clean tree.\n"
        )
        return 2

    if findings:
        sys.stderr.write(
            f"[routing-input-model-fit] FAIL: {len(findings)} handler(s) declare one "
            f"input model under more than one message category "
            f"({contract_count} contracts scanned):\n"
        )
        for finding in findings:
            sys.stderr.write(f"  - {finding.render()}\n")
        return 1

    sys.stderr.write(
        f"[routing-input-model-fit] OK: {contract_count} contracts scanned, "
        "0 input-model/category conflicts.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
