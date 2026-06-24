"""NodeGoldenChainSweep — field-presence validation over pre-collected projection rows.

Defines chains (head topic -> tail table) and runs field-level assertions against
``projected_rows`` the **caller** supplies, producing per-chain pass/fail results.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls, **zero live I/O**.

Chain set resolution (OMN-13553): when a caller dispatches with no ``chains``
(the canonical ``onex skill golden_chain_sweep`` path supplies only the
runtime-injected ``correlation_id``), the request defaults to the packaged
``golden_chains.yaml`` registry. Reading the node's own packaged config is
deterministic resolution, NOT live runtime I/O. An empty validated chain set is
fail-closed: ``overall_status`` is ``fail``, NEVER ``pass`` — a sweep over zero
chains is vacuous truth, not health.

Evidence scope (OMN-8724, OMN-13126): this node does NOT perform any live I/O —
no Kafka publish, no DB poll, no ``count(*)``, no row-count delta. A ``pass`` only
asserts that the caller-supplied rows contain the expected field keys; it does NOT
prove an event flowed end-to-end or that a row materialized in a live tail table.
Do NOT cite a pass here as live row / end-to-end data-flow evidence. A real
live-Postgres fetch + row-count-delta assertion is tracked under OMN-8724 and is
not implemented here yet.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EnumChainStatus(StrEnum):
    """Validation status for a single chain."""

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    TIMEOUT = "timeout"
    GATED = "gated"  # consumer healthy but idle; non-blocking


class EnumSweepStatus(StrEnum):
    """Overall sweep status."""

    PASS = "pass"
    PARTIAL = "partial"
    FAIL = "fail"
    GATED = "gated"  # all non-passing chains are idle-gated; non-blocking


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ModelChainDefinition(BaseModel):
    """Definition of a golden chain to validate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    head_topic: str
    tail_table: str
    expected_fields: list[str] = Field(default_factory=list)
    correlation_id: str | None = None
    proof_classification: str = Field(
        default="diagnostic",
        description=(
            "proof-ready only when the exact live code, bus, and projection lane "
            "are traversed; deterministic unit/fixture chains stay diagnostic."
        ),
    )
    replay_status: str = Field(default="replay-not-applicable")
    stages: list[dict[str, object]] = Field(default_factory=list)


class ModelChainResult(BaseModel):
    """Validation result for a single chain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    status: EnumChainStatus
    head_topic: str
    tail_table: str
    publish_ms: float = 0.0
    projection_ms: float = 0.0
    missing_fields: list[str] = Field(default_factory=list)
    message: str = ""


def _default_chains_from_registry() -> list[ModelChainDefinition]:
    """Resolve the default chain set from the packaged ``golden_chains.yaml``.

    Used when a caller dispatches with no ``chains`` (the canonical
    ``onex skill golden_chain_sweep`` path supplies only ``correlation_id``).
    Reading the node's own packaged registry is deterministic config
    resolution, NOT live runtime I/O — no Kafka publish, no DB poll. The
    handler stays a pure validator; this only ensures the validated chain set
    is non-empty so the sweep cannot report a vacuous ``pass`` over zero chains
    (OMN-13553). The lazy import avoids a module-level cycle with
    ``registry`` (which imports ``ModelChainDefinition`` from this module).
    """
    from omnimarket.nodes.node_golden_chain_sweep.registry import load_registry

    return load_registry()


class GoldenChainSweepRequest(BaseModel):
    """Input for the golden chain sweep handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Injected by the runtime into every dispatched command payload (RuntimeLocal
    # publishes {"correlation_id": ...} as the minimal initial payload when no
    # input file is supplied). Declared so the request validates cleanly on the
    # bus dispatch path — without it the runtime-injected field is rejected by
    # extra="forbid" before the handler ever runs.
    correlation_id: str = ""
    chains: list[ModelChainDefinition] = Field(
        default_factory=lambda: _default_chains_from_registry()
    )
    timeout_ms: int = 15000
    projected_rows: dict[str, dict[str, object]] = Field(default_factory=dict)
    idle_gate: bool = (
        False  # when True, missing rows → GATED (non-blocking) not TIMEOUT
    )


class GoldenChainSweepResult(BaseModel):
    """Output of the golden chain sweep handler."""

    model_config = ConfigDict(extra="forbid")

    chain_results: list[ModelChainResult] = Field(default_factory=list)
    chains_total: int = 0
    chains_passed: int = 0
    chains_failed: int = 0
    chains_gated: int = 0
    overall_status: EnumSweepStatus = EnumSweepStatus.PASS
    status: str = "pass"

    @property
    def by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for cr in self.chain_results:
            counts[cr.status] = counts.get(cr.status, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeGoldenChainSweep:
    """Validate chain field-presence against pre-collected projection rows.

    Pure compute handler — validates the caller-supplied ``projected_rows``
    against chain definitions. Performs **zero live I/O** (no Kafka, no DB). A
    ``pass`` proves field presence in caller-supplied rows only — NOT live row
    materialization or end-to-end data flow. Do not cite as live evidence
    (OMN-8724).
    """

    def handle(self, request: GoldenChainSweepRequest) -> GoldenChainSweepResult:
        """Execute the golden chain sweep."""
        results: list[ModelChainResult] = []
        passed = 0
        failed = 0
        gated = 0

        for chain in request.chains:
            result = self._validate_chain(
                chain, request.projected_rows, idle_gate=request.idle_gate
            )
            results.append(result)
            if result.status == EnumChainStatus.PASS:
                passed += 1
            elif result.status == EnumChainStatus.GATED:
                gated += 1
            else:
                failed += 1

        if not request.chains:
            # Fail-closed: zero validated chains must NEVER report pass. A sweep
            # over an empty chain set is vacuous truth, not health — an operator
            # or CI gate reading `overall_status: pass` here would see "all
            # golden chains healthy" when nothing was checked (OMN-13553).
            overall = EnumSweepStatus.FAIL
        elif failed == 0 and gated == 0:
            overall = EnumSweepStatus.PASS
        elif failed == 0 and gated > 0:
            overall = EnumSweepStatus.GATED
        elif passed > 0 or gated > 0:
            overall = EnumSweepStatus.PARTIAL
        else:
            overall = EnumSweepStatus.FAIL

        return GoldenChainSweepResult(
            chain_results=results,
            chains_total=len(request.chains),
            chains_passed=passed,
            chains_failed=failed,
            chains_gated=gated,
            overall_status=overall,
            status=overall.value,
        )

    def _validate_chain(
        self,
        chain: ModelChainDefinition,
        projected_rows: dict[str, dict[str, object]],
        *,
        idle_gate: bool = False,
    ) -> ModelChainResult:
        """Validate a single chain against projected data."""
        row = projected_rows.get(chain.name)

        if row is None:
            if idle_gate:
                return ModelChainResult(
                    name=chain.name,
                    status=EnumChainStatus.GATED,
                    head_topic=chain.head_topic,
                    tail_table=chain.tail_table,
                    message=f"No projected row for {chain.name} — consumer idle (non-blocking)",
                )
            return ModelChainResult(
                name=chain.name,
                status=EnumChainStatus.TIMEOUT,
                head_topic=chain.head_topic,
                tail_table=chain.tail_table,
                message=f"No projected row found for chain {chain.name}",
            )

        missing = [f for f in chain.expected_fields if f not in row]

        if missing:
            return ModelChainResult(
                name=chain.name,
                status=EnumChainStatus.FAIL,
                head_topic=chain.head_topic,
                tail_table=chain.tail_table,
                missing_fields=missing,
                message=f"Missing fields: {', '.join(missing)}",
            )

        return ModelChainResult(
            name=chain.name,
            status=EnumChainStatus.PASS,
            head_topic=chain.head_topic,
            tail_table=chain.tail_table,
            message="All expected fields present",
        )
