"""NodeGoldenChainSweep — field-presence + recency validation over pre-collected projection rows.

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

Per-chain freshness (OMN-13639): field-presence on the latest tail row does NOT
prove the row is *recent*. A chain whose only matching row is a weeks-old fixture
would otherwise read green even when the producer is idle. A chain may declare a
``max_row_age_seconds`` threshold plus a ``timestamp_field``; when the latest tail
row's timestamp exceeds the threshold (or recency cannot be proven), the chain is
downgraded to a distinct ``STALE``/``WARN`` tri-state — NOT ``PASS`` — and the row
age is reported. The reference clock is injected via ``now_iso`` so the compute
stays pure and deterministic (no system-clock read inside the handler).

Evidence scope (OMN-8724, OMN-13126): this node does NOT perform any live I/O —
no Kafka publish, no DB poll, no ``count(*)``, no row-count delta. A ``pass`` only
asserts that the caller-supplied rows contain the expected field keys (and, when a
freshness threshold is configured, that the supplied row timestamp is recent); it
does NOT prove an event flowed end-to-end or that a row materialized in a live
tail table. Do NOT cite a pass here as live row / end-to-end data-flow evidence.
A real live-Postgres fetch + row-count-delta assertion is tracked under OMN-8724
and is not implemented here yet.
"""

from __future__ import annotations

from datetime import datetime
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
    STALE = "stale"  # fields present but latest row exceeds freshness threshold


class EnumSweepStatus(StrEnum):
    """Overall sweep status."""

    PASS = "pass"
    PARTIAL = "partial"
    FAIL = "fail"
    GATED = "gated"  # all non-passing chains are idle-gated; non-blocking
    WARN = "warn"  # all non-passing chains are stale; non-blocking warning


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
    timestamp_field: str = Field(
        default="created_at",
        description=(
            "Column on the tail-table row holding the row's event/ingest time. "
            "Read only when max_row_age_seconds is set (OMN-13639)."
        ),
    )
    max_row_age_seconds: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Per-chain freshness threshold (OMN-13639). When set, the latest "
            "tail row's timestamp_field value must be no older than this many "
            "seconds relative to the injected now_iso; otherwise the chain is "
            "downgraded to STALE (a distinct non-PASS tri-state) rather than "
            "reading green on a weeks-old fixture row. None disables the check."
        ),
    )


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
    row_age_seconds: float | None = Field(
        default=None,
        description=(
            "Age of the latest tail row in seconds relative to now_iso, when a "
            "freshness threshold is configured and the timestamp parsed (OMN-13639)."
        ),
    )
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
    # Reference clock for the per-chain freshness check (OMN-13639). Injected by
    # the caller (effect/orchestrator boundary) so the compute stays pure and
    # deterministic — the handler never reads the system clock. ISO-8601 with an
    # explicit offset. None disables freshness evaluation; a freshness-gated
    # chain dispatched without it is surfaced as ERROR (fail-fast), never a
    # silent PASS.
    now_iso: str | None = None


class GoldenChainSweepResult(BaseModel):
    """Output of the golden chain sweep handler."""

    model_config = ConfigDict(extra="forbid")

    chain_results: list[ModelChainResult] = Field(default_factory=list)
    chains_total: int = 0
    chains_passed: int = 0
    chains_failed: int = 0
    chains_gated: int = 0
    chains_stale: int = 0
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


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z``.

    Returns ``None`` when the value is not a parseable ISO-8601 timestamp.
    """
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


class NodeGoldenChainSweep:
    """Validate chain field-presence (and optional recency) against pre-collected rows.

    Pure compute handler — validates the caller-supplied ``projected_rows``
    against chain definitions. Performs **zero live I/O** (no Kafka, no DB). A
    ``pass`` proves field presence in caller-supplied rows only — NOT live row
    materialization or end-to-end data flow. When a chain declares
    ``max_row_age_seconds``, a row older than the threshold (or whose recency
    cannot be proven) is downgraded to ``STALE`` (OMN-13639). Do not cite a pass
    as live evidence (OMN-8724).
    """

    def handle(self, request: GoldenChainSweepRequest) -> GoldenChainSweepResult:
        """Execute the golden chain sweep."""
        results: list[ModelChainResult] = []
        passed = 0
        failed = 0
        gated = 0
        stale = 0

        for chain in request.chains:
            result = self._validate_chain(
                chain,
                request.projected_rows,
                idle_gate=request.idle_gate,
                now_iso=request.now_iso,
            )
            results.append(result)
            if result.status == EnumChainStatus.PASS:
                passed += 1
            elif result.status == EnumChainStatus.GATED:
                gated += 1
            elif result.status == EnumChainStatus.STALE:
                stale += 1
            else:
                failed += 1

        overall = self._aggregate(
            chains_present=bool(request.chains),
            passed=passed,
            failed=failed,
            gated=gated,
            stale=stale,
        )

        return GoldenChainSweepResult(
            chain_results=results,
            chains_total=len(request.chains),
            chains_passed=passed,
            chains_failed=failed,
            chains_gated=gated,
            chains_stale=stale,
            overall_status=overall,
            status=overall.value,
        )

    @staticmethod
    def _aggregate(
        *,
        chains_present: bool,
        passed: int,
        failed: int,
        gated: int,
        stale: int,
    ) -> EnumSweepStatus:
        """Roll per-chain counts up to a single overall status.

        Precedence: a vacuous (zero-chain) sweep is fail-closed. A blocking
        FAIL always degrades to PARTIAL/FAIL. GATED (idle) and STALE (recency)
        are non-blocking — when they are the *only* non-PASS chains, the sweep
        reports GATED or WARN respectively rather than green PASS, so an
        operator sees the distinction.
        """
        if not chains_present:
            # Fail-closed: zero validated chains must NEVER report pass. A sweep
            # over an empty chain set is vacuous truth, not health (OMN-13553).
            return EnumSweepStatus.FAIL
        if failed > 0:
            # Any blocking failure → PARTIAL if anything else passed/gated/stale,
            # else a hard FAIL.
            if passed > 0 or gated > 0 or stale > 0:
                return EnumSweepStatus.PARTIAL
            return EnumSweepStatus.FAIL
        # No blocking failures from here down.
        if gated > 0:
            # Idle-gated chains are non-blocking; GATED takes precedence over a
            # stale-only warning when both are present (consumer idle is the more
            # fundamental "no flow" signal).
            return EnumSweepStatus.GATED
        if stale > 0:
            return EnumSweepStatus.WARN
        return EnumSweepStatus.PASS

    def _validate_chain(
        self,
        chain: ModelChainDefinition,
        projected_rows: dict[str, dict[str, object]],
        *,
        idle_gate: bool = False,
        now_iso: str | None = None,
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

        # Field-presence satisfied. Apply the optional per-chain recency check.
        if chain.max_row_age_seconds is not None:
            return self._evaluate_freshness(chain, row, now_iso=now_iso)

        return ModelChainResult(
            name=chain.name,
            status=EnumChainStatus.PASS,
            head_topic=chain.head_topic,
            tail_table=chain.tail_table,
            message="All expected fields present",
        )

    def _evaluate_freshness(
        self,
        chain: ModelChainDefinition,
        row: dict[str, object],
        *,
        now_iso: str | None,
    ) -> ModelChainResult:
        """Downgrade a field-complete chain to STALE when its latest row is too old.

        Caller-supplied ``now_iso`` is the reference clock — the compute never
        reads the system clock. A freshness-gated chain dispatched with no
        ``now_iso`` is a wiring bug (ERROR, fail-fast). When recency cannot be
        proven (timestamp column absent or unparseable) the chain is STALE, not
        PASS — green must require provable recent flow (OMN-13639).
        """
        threshold = chain.max_row_age_seconds
        assert threshold is not None  # guarded by caller

        if now_iso is None:
            return ModelChainResult(
                name=chain.name,
                status=EnumChainStatus.ERROR,
                head_topic=chain.head_topic,
                tail_table=chain.tail_table,
                message=(
                    f"Chain {chain.name} declares max_row_age_seconds={threshold} "
                    "but no now_iso reference clock was injected — cannot evaluate "
                    "recency (fail-fast, not a silent pass)"
                ),
            )

        now_dt = _parse_iso(now_iso)
        if now_dt is None:
            return ModelChainResult(
                name=chain.name,
                status=EnumChainStatus.ERROR,
                head_topic=chain.head_topic,
                tail_table=chain.tail_table,
                message=f"now_iso is not a parseable ISO-8601 timestamp: {now_iso!r}",
            )

        raw_ts = row.get(chain.timestamp_field)
        if raw_ts is None:
            return ModelChainResult(
                name=chain.name,
                status=EnumChainStatus.STALE,
                head_topic=chain.head_topic,
                tail_table=chain.tail_table,
                message=(
                    f"STALE: timestamp field '{chain.timestamp_field}' absent from "
                    f"{chain.tail_table} row — recency cannot be proven for "
                    f"{chain.name} (threshold {threshold}s)"
                ),
            )

        row_dt = _parse_iso(str(raw_ts))
        if row_dt is None:
            return ModelChainResult(
                name=chain.name,
                status=EnumChainStatus.STALE,
                head_topic=chain.head_topic,
                tail_table=chain.tail_table,
                message=(
                    f"STALE: timestamp field '{chain.timestamp_field}'={raw_ts!r} is "
                    f"not a parseable ISO-8601 timestamp — recency cannot be proven "
                    f"for {chain.name} (threshold {threshold}s)"
                ),
            )

        age_seconds = (now_dt - row_dt).total_seconds()

        if age_seconds > threshold:
            return ModelChainResult(
                name=chain.name,
                status=EnumChainStatus.STALE,
                head_topic=chain.head_topic,
                tail_table=chain.tail_table,
                row_age_seconds=age_seconds,
                message=(
                    f"STALE: latest {chain.tail_table} row is "
                    f"{int(age_seconds)}s old (> threshold {threshold}s) — "
                    f"fields present but no recent flow for {chain.name}"
                ),
            )

        return ModelChainResult(
            name=chain.name,
            status=EnumChainStatus.PASS,
            head_topic=chain.head_topic,
            tail_table=chain.tail_table,
            row_age_seconds=age_seconds,
            message=(
                f"All expected fields present; latest row "
                f"{int(age_seconds)}s old (<= threshold {threshold}s)"
            ),
        )
