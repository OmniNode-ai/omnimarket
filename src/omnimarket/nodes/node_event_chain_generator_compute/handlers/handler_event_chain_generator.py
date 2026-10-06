# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Walker paths -> driven chains -> expectations, and the gate over them."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from omnimarket.nodes.node_event_chain_generator_compute.models.model_chain_generation import (
    ChainKind,
    ModelChainExpectationSet,
    ModelChainGateRequest,
    ModelChainGateResult,
    ModelChainMismatch,
    ModelChainObligation,
    ModelDrivenChain,
    ModelGeneratedChain,
    TerminalValue,
)

# Terminal payload fields a chain pins: what the workflow decided, never the
# content it carried. The error names match the chain assertion helper's.
TERMINAL_FIELDS = frozenset(
    {
        "quality_passed",
        "content_verdict",
        "terminal_outcome",
        "routing_disposition",
        "operational_outcome",
        "error_code",
        "failure_reason",
        "failure_class",
        "failure_code",
        "terminal_failure_reason",
        "unrouted_reason",
    }
)
_COMPARED = ("kind", "expected_event_types", "expected_states", "terminal_fields")


class UndrivablePathError(LookupError):
    """The driver has no fixture for a trigger on this path."""


class ProtocolChainDriver(Protocol):
    """Drives one walker path through the real workflow handlers."""

    workflow_owner: str

    async def drive(self, obligation: ModelChainObligation) -> ModelDrivenChain: ...


def _seq(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"expected a sequence, got {type(value).__name__}")
    return value


def _map(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"expected a mapping, got {type(value).__name__}")
    return cast("Mapping[str, object]", value)


def _owner_state(value: object) -> str:
    return str(_seq(value)[0])


class HandlerEventChainGenerator:
    def handle(self, request: ModelChainGateRequest) -> ModelChainGateResult:
        """Dispatch entrypoint: the gate operation."""
        return self.gate(request)

    def obligations(
        self, walker_report: Mapping[str, object], workflow_owner: str
    ) -> tuple[ModelChainObligation, ...]:
        """Project the workflow's paths onto owner state changes and deduplicate."""
        matches = [
            workflow
            for item in _seq(walker_report.get("workflows"))
            if (workflow := _map(item)).get("workflow_owner") == workflow_owner
        ]
        if len(matches) != 1:
            raise ValueError(
                f"expected one workflow owned by {workflow_owner!r}; found {len(matches)}"
            )
        seen: Counter[tuple[str, tuple[tuple[str, str, str], ...]]] = Counter()
        for raw_path in _seq(matches[0].get("paths")):
            path = _map(raw_path)
            steps: list[tuple[str, str, str]] = []
            for raw_step in _seq(path.get("steps")):
                step = _map(raw_step)
                frm = _owner_state(step.get("from_state"))
                to = _owner_state(step.get("to_state"))
                if frm != to:
                    steps.append((frm, str(step.get("trigger")), to))
            seen[(str(path.get("kind")), tuple(steps))] += 1
        obligations = [
            ModelChainObligation(
                path_id=f"{kind}:{'>'.join(t for _, t, _ in steps)}",
                kind=cast(ChainKind, kind),
                steps=steps,
            )
            for kind, steps in seen
        ]
        return tuple(sorted(obligations, key=lambda o: o.path_id))

    async def generate(
        self,
        obligations: tuple[ModelChainObligation, ...],
        driver: ProtocolChainDriver,
    ) -> ModelChainExpectationSet:
        """Drive every path once and record its chain; name the undrivable ones."""
        chains: list[ModelGeneratedChain] = []
        undriven: dict[str, str] = {}
        for obligation in obligations:
            try:
                driven = await driver.drive(obligation)
            except UndrivablePathError as exc:
                undriven[obligation.path_id] = str(exc)
                continue
            terminal: dict[str, TerminalValue] = {
                name: cast(TerminalValue, value)
                for name, value in sorted(driven.terminal_payload.items())
                if name in TERMINAL_FIELDS and value is not None
            }
            chains.append(
                ModelGeneratedChain(
                    path_id=obligation.path_id,
                    kind=obligation.kind,
                    expected_event_types=driven.event_types,
                    expected_states=driven.states,
                    terminal_fields=terminal,
                )
            )
        return ModelChainExpectationSet(
            workflow_owner=driver.workflow_owner,
            chains=tuple(chains),
            undriven=undriven,
        )

    def gate(self, request: ModelChainGateRequest) -> ModelChainGateResult:
        """Fail on a path with no chain, a chain with no path, or a changed chain."""
        path_ids = {o.path_id for o in request.obligations}
        committed = {
            c.path_id: c
            for c in (request.committed.chains if request.committed else ())
        }
        generated = {c.path_id: c for c in request.generated.chains}
        missing = tuple(sorted(path_ids - committed.keys()))
        stale = tuple(sorted(committed.keys() - path_ids))
        undriven = tuple(
            sorted(
                (path_ids - generated.keys())
                | (path_ids & request.generated.undriven.keys())
            )
        )
        mismatches: list[ModelChainMismatch] = []
        for path_id in sorted(path_ids & committed.keys()):
            fresh = generated.get(path_id)
            if fresh is None:
                continue
            old = committed[path_id]
            for field in _COMPARED:
                if getattr(old, field) != getattr(fresh, field):
                    mismatches.append(
                        ModelChainMismatch(
                            path_id=path_id,
                            field=field,
                            committed=getattr(old, field),
                            generated=getattr(fresh, field),
                        )
                    )
        return ModelChainGateResult(
            workflow_owner=request.workflow_owner,
            passed=not (missing or stale or undriven or mismatches),
            missing_chain_path_ids=missing,
            stale_chain_path_ids=stale,
            undriven_path_ids=undriven,
            mismatches=tuple(mismatches),
        )
