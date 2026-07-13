"""NodeRuntimeSweep — Runtime registration and wiring verification.

Checks node descriptions, handler wiring, topic symmetry (producer/consumer
pairs), and classifies findings by type and severity.

ONEX node type: COMPUTE — deterministic, no LLM calls. Check phases are pure;
when a request carries no entities the handler resolves the default input set
from ``$OMNI_HOME`` (OMN-13919), mirroring NodeComplianceSweep's default-scan
resolution.
"""

from __future__ import annotations

import os
import re
from enum import StrEnum
from uuid import UUID

# OMN-14528: reuse the runtime's canonical consumer-group naming so the
# per-contract liveness check matches node_name against live group ids using
# the EXACT normalization the runtime applies when it creates the group. These
# are pure, deterministic helpers (no I/O) — safe to import into a COMPUTE node.
from omnibase_infra.enums import EnumConsumerGroupPurpose
from omnibase_infra.utils.util_consumer_group import normalize_kafka_identifier
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.watchdog import EnumWatchdogEventType

# The canonical consumer-group id embeds the node name in the segment
# ``.{normalized_node_name}.{consume}.`` (see
# ``omnibase_infra.utils.util_consumer_group.compute_consumer_group_id`` —
# ``{env}.{service}.{node_name}.{purpose}.{version}``). Precompute the
# normalized CONSUME purpose token so the liveness matcher stays in lock-step
# with the runtime instead of hardcoding the literal "consume".
_CONSUME_PURPOSE_SEGMENT = normalize_kafka_identifier(
    EnumConsumerGroupPurpose.CONSUME.value
)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EnumDescriptionStatus(StrEnum):
    """Node description classification."""

    REAL = "REAL"
    PLACEHOLDER = "PLACEHOLDER"
    MISSING = "MISSING"


class EnumWiringStatus(StrEnum):
    """Handler wiring classification."""

    WIRED = "WIRED"
    UNWIRED = "UNWIRED"
    ORPHAN_TOPIC = (
        "ORPHAN_TOPIC"  # onex-topic-allow: enum value, not a topic assignment
    )


class EnumSymmetryStatus(StrEnum):
    """Topic symmetry classification."""

    SYMMETRIC = "SYMMETRIC"
    PRODUCER_ONLY = "PRODUCER_ONLY"
    CONSUMER_ONLY = "CONSUMER_ONLY"


class EnumFindingType(StrEnum):
    """Runtime finding type."""

    PLACEHOLDER_DESCRIPTION = "PLACEHOLDER_DESCRIPTION"
    MISSING_DESCRIPTION = "MISSING_DESCRIPTION"
    UNWIRED_HANDLER = "UNWIRED_HANDLER"
    ORPHAN_TOPIC = (
        "ORPHAN_TOPIC"  # onex-topic-allow: enum value, not a topic assignment
    )
    PRODUCER_ONLY = "PRODUCER_ONLY"
    CONSUMER_ONLY = "CONSUMER_ONLY"
    NON_DURABLE_CONTRACT = "NON_DURABLE_CONTRACT"
    STRANDED_WORKFLOW = "STRANDED_WORKFLOW"
    # OMN-12957/OMN-14528: a subscribing, runtime-deployed contract whose OWN
    # node-name identity has no live consumer group on the broker — the
    # subscription is not being drained (a silent orphan / "dead corpse": ships
    # in the image, declares subscribe_topics, has zero live consumer group).
    # Per-CONTRACT, not per-profile: a sibling's live group in the same
    # runtime_profile no longer vouches for this contract (OMN-14528 closes the
    # profile-level vouching false-negative that hid all 13 dead consumers).
    CONTRACT_NO_LIVE_CONSUMER = "CONTRACT_NO_LIVE_CONSUMER"
    # OMN-13589: a single-repo onex.nodes entry point that fails the structural
    # or import probe (module/__init__/contract.yaml/handler/model load). The
    # harness collects per-entry-point probes; the pure node turns a failed
    # probe into this finding.
    BROKEN_ENTRY_POINT = "BROKEN_ENTRY_POINT"


class EnumSweepCheck(StrEnum):
    """Selectable runtime-sweep phases.

    A request with ``enabled_checks=None`` runs every phase (default, full
    cross-repo sweep). A request that names a subset runs only those phases —
    this is how the single-repo CI import-probe scopes itself to REGISTRATION
    without tripping the cross-repo symmetry/durability invariants.
    """

    REGISTRATION = "REGISTRATION"
    DESCRIPTION = "DESCRIPTION"
    WIRING = "WIRING"
    SYMMETRY = "SYMMETRY"
    DURABILITY = "DURABILITY"
    STRANDED_WORKFLOW = "STRANDED_WORKFLOW"
    # OMN-14528: per-contract live-consumer-group verification (replaces the
    # per-profile PROFILE_CONSUMER vouching check). Its census is collected by a
    # live broker probe, never a hand-typed flag; when it runs, the census is
    # REQUIRED (absence fails closed, never a silent skip).
    CONSUMER_LIVENESS = "CONSUMER_LIVENESS"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ModelWorkflowObservation(BaseModel):
    """One workflow FSM instance observed from the event stream.

    Pure-compute input: the collector pairs a workflow-start observation with any
    terminal evidence (a domain ``*-completed`` / ``*-failed`` event OR a typed
    watchdog event) for the same correlation id. ``reached_terminal`` is True when
    terminal evidence exists. A started-but-not-terminal workflow whose
    ``elapsed_ms`` exceeds its archetype SLA is stranded and trips the invariant.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    archetype: str
    workflow_state: str = Field(
        description="Last observed non-terminal FSM state for the workflow."
    )
    elapsed_ms: int = Field(
        ge=0,
        description="Milliseconds from workflow-start to observation cutoff.",
    )
    reached_terminal: bool = Field(
        description="True when a domain terminal OR typed watchdog event was observed.",
    )
    has_routable_next: bool = Field(
        default=True,
        description=(
            "False when no eligible handler/tier/backend can advance the workflow "
            "(unroutable). Drives watchdog classification."
        ),
    )
    making_progress: bool = Field(
        default=True,
        description=(
            "False when the workflow is alive but wedged with no forward progress "
            "(stalled). Drives watchdog classification."
        ),
    )


class ModelContractInput(BaseModel):
    """Input representing a single node contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str
    description: str = ""
    handler_module: str = ""
    handler_exists: bool = True
    publish_topics: list[str] = Field(default_factory=list)
    subscribe_topics: list[str] = Field(default_factory=list)
    # OMN-12957: runtime_profiles declared by this contract (lower-cased).
    runtime_profiles: list[str] = Field(default_factory=list)


class ModelEntryPointProbe(BaseModel):
    """Result of probing one single-repo ``onex.nodes`` entry point.

    The harness (the I/O boundary) walks ``pyproject.toml``, runs the structural
    and import checks for each declared node, and emits one probe per node. The
    pure node turns ``ok=False`` probes into ``BROKEN_ENTRY_POINT`` findings.
    ``reason`` is populated only when ``ok=False`` and mirrors the exact strings
    produced by the structural/import checks (e.g. "contract.yaml missing",
    "handler import failed: <exc>").
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str
    module_path: str
    ok: bool
    reason: str = ""


class ModelRuntimeFinding(BaseModel):
    """A single runtime verification finding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_type: EnumFindingType
    subject: str
    message: str
    severity: str  # CRITICAL | WARNING | INFO


class RuntimeSweepRequest(BaseModel):
    """Input for the runtime sweep handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contracts: list[ModelContractInput] = Field(default_factory=list)
    topic_producers: list[str] = Field(default_factory=list)
    topic_consumers: list[str] = Field(default_factory=list)
    durable_node_names: list[str] | None = Field(
        default=None,
        description=(
            "Census of node names reconstructable from a durable source on a "
            "COLD runtime start — i.e. the image-bundled filesystem manifest "
            "(HYBRID-mode bootstrap + PluginLoaderContractSource) and/or a "
            "compacted snapshot topic. Contracts present in the live registered "
            "census (`contracts`) but ABSENT from this set were materialized "
            "ONLY via the post-freeze dynamic-contract listener, which consumes "
            "the SUFFIX_NODE_REGISTRATION topic (cleanup.policy=delete, "
            "7-day retention) with auto_offset_reset=latest and therefore does "
            "NOT replay history on cold start. Such contracts vanish on a cold "
            "restart unless their registrant re-publishes. When None, the "
            "durability check is skipped (caller did not supply a manifest)."
        ),
    )
    workflow_observations: list[ModelWorkflowObservation] = Field(
        default_factory=list,
        description=(
            "Observed workflow FSM instances for the stranded-workflow invariant "
            "(started-but-no-terminal-event after archetype SLA)."
        ),
    )
    archetype_sla_ms: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Per-archetype terminal SLA in ms. Archetypes absent from the map use "
            "DEFAULT_ARCHETYPE_SLA_MS."
        ),
    )
    # OMN-12957/OMN-14528: live consumer-group census — the ACTUAL LIVE
    # (non-Empty) consumer GROUP IDs currently attached on the broker, collected
    # in code by ``broker_probe.collect_live_consumer_groups`` (never an
    # operator-typed flag; the old ``--live-consumer-profiles`` census was DATA
    # nothing automated ever supplied, so the check silently never ran). The
    # CONSUMER_LIVENESS phase checks each subscribing contract's OWN node-name
    # identity against this census (per-contract, not per-profile).
    #
    # ``None`` means the census was not collected. When ``require_live_consumer_census``
    # is set (the real skill/CLI runtime paths always set it), a ``None`` census
    # with the phase enabled is a HARD FAILURE (``handle`` raises) — absence of
    # the census is never a silent skip (OMN-14528). An empty list is distinct
    # and legal: broker reachable, zero LIVE groups exist.
    live_consumer_groups: list[str] | None = None
    # OMN-14528 fail-closed switch. The runtime dispatch surfaces (the skill
    # default-input resolver and the ``__main__`` full sweep) set this True after
    # probing the broker, so the CONSUMER_LIVENESS phase can never report a
    # vacuous pass over an absent/empty census. Synthetic unit requests leave it
    # False and may exercise the phase with an explicit census without tripping
    # the fail-closed guards.
    require_live_consumer_census: bool = False
    # OMN-13589: single-repo entry-point import probes. The harness walks this
    # repo's pyproject [project.entry-points."onex.nodes"] and produces one probe
    # per node (structural + import checks). The REGISTRATION phase turns failed
    # probes into BROKEN_ENTRY_POINT findings. Empty (default) ⇒ REGISTRATION
    # emits nothing, so cross-repo/skill callers see no behavior change.
    entry_point_probes: list[ModelEntryPointProbe] = Field(default_factory=list)
    # OMN-13589: select which sweep phases run. None (default) ⇒ all phases run
    # exactly as before — existing cross-repo/skill behavior is byte-for-byte
    # preserved. A named subset (e.g. [REGISTRATION]) scopes the sweep, which is
    # how the single-repo CI import-probe avoids the cross-repo symmetry and
    # durability invariants.
    enabled_checks: list[EnumSweepCheck] | None = None
    # OMN-13715: scope hint surfaced via `onex skill runtime_sweep --scope`.
    # Accepted values (by convention): "all-repos" (default, full sweep) or
    # "omnidash-only" (limit sweep to the omnidash repo context). OMN-13919:
    # when the request carries no entities, scope selects which repos the
    # default $OMNI_HOME collection walks. None ⇒ full sweep ("all-repos").
    scope: str | None = None
    dry_run: bool = False


class RuntimeSweepResult(BaseModel):
    """Output of the runtime sweep handler."""

    model_config = ConfigDict(extra="forbid")

    findings: list[ModelRuntimeFinding] = Field(default_factory=list)
    contracts_checked: int = 0
    topics_checked: int = 0
    workflows_checked: int = 0
    entry_points_checked: int = 0
    # OMN-14528: number of subscribing, runtime-deployed contracts evaluated by
    # the CONSUMER_LIVENESS phase, and the size of the live-group census they
    # were evaluated against. Both are 0 when the phase did not run. When the
    # phase runs under a required census, the handler asserts BOTH are > 0
    # before it can report a clean verdict — "scanned nothing" and "all healthy"
    # must never be arithmetically identical (the detection-shelf disease).
    consumers_checked: int = 0
    live_consumer_groups_scanned: int = 0
    # OMN-13919: "no_input" is no longer a reportable status — a zero-entity
    # run raises instead of returning (vacuous passes are unrepresentable).
    status: str = "clean"  # clean | findings | error
    dry_run: bool = False

    @property
    def total_findings(self) -> int:
        return len(self.findings)

    @property
    def by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.finding_type] = counts.get(f.finding_type, 0) + 1
        return counts

    @property
    def by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PLACEHOLDER_PATTERNS = [
    re.compile(r"^compute\+[a-f0-9]+$", re.IGNORECASE),
    re.compile(r"Generated by generate_skill_node", re.IGNORECASE),
    re.compile(r"^(TODO|TBD|FIXME|placeholder)$", re.IGNORECASE),
]

# Default terminal SLA for any archetype not present in request.archetype_sla_ms.
# A workflow started-but-not-terminal past this is treated as stranded.
DEFAULT_ARCHETYPE_SLA_MS = 600_000  # 10 minutes


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeRuntimeSweep:
    """Verify runtime registration and wiring integrity.

    The check phases are pure compute over pre-collected contract metadata.
    When the caller supplies NO entities at all (the ``onex skill
    runtime_sweep`` no-args dispatch path), the handler resolves a default
    input set by walking ``$OMNI_HOME`` for contract.yaml files (OMN-13919) —
    the same collection the ``__main__`` CLI harness performs. This mirrors
    ``NodeComplianceSweep``, which resolves default scan dirs from
    ``$OMNI_HOME`` inside ``handle()``.
    """

    @staticmethod
    def _enabled(request: RuntimeSweepRequest, check: EnumSweepCheck) -> bool:
        """True when ``check`` should run for this request.

        ``enabled_checks is None`` ⇒ every phase runs (default, full sweep).
        A named subset runs only the listed phases.
        """
        return request.enabled_checks is None or check in request.enabled_checks

    @staticmethod
    def _has_input(request: RuntimeSweepRequest) -> bool:
        """True when the caller supplied at least one checkable entity."""
        return bool(
            request.contracts
            or request.topic_producers
            or request.topic_consumers
            or request.workflow_observations
            or request.entry_point_probes
        )

    @staticmethod
    def _resolve_default_input(request: RuntimeSweepRequest) -> RuntimeSweepRequest:
        """Collect the default entity set when the request carries none.

        OMN-13919: the ``onex skill runtime_sweep`` dispatch path invokes this
        handler with only ``{scope, dry_run}`` — before this fix every skill
        run reported ``status=no_input`` with zero entities checked (a
        regression of the OMN-13715/OMN-13708 vacuous-pass class). The local
        repo set under ``$OMNI_HOME`` is the default check target: walk it for
        contract.yaml files exactly as the ``__main__`` CLI harness does and
        derive the topic census from the collected contracts.

        OMN-14528: this resolver is ALSO the only place the live
        consumer-group census can be collected on the skill dispatch path
        (the generic ``onex skill`` runner has no per-skill pre-dispatch hook).
        When the CONSUMER_LIVENESS phase is enabled it probes the broker in
        code (``broker_probe.collect_live_consumer_groups`` against
        ``KAFKA_BOOTSTRAP_SERVERS``) and marks the census REQUIRED, so the
        deadness detector actually RUNS on the real skill path instead of
        silently skipping (the exact defect this fix closes). The probe is the
        I/O boundary; the check phases stay pure.

        Raises:
            ValueError: when ``OMNI_HOME`` is unset, or when the
                CONSUMER_LIVENESS phase is enabled but ``KAFKA_BOOTSTRAP_SERVERS``
                is unset (fail fast / fail closed — never a silent empty
                default; feedback rule #8).
        """
        if NodeRuntimeSweep._has_input(request):
            return request

        # Local import: collection imports ModelContractInput from this
        # module, so a top-level import would be circular.
        from omnimarket.nodes.node_runtime_sweep.collection import collect_contracts

        omni_home = os.environ.get("OMNI_HOME")
        if not omni_home:
            raise ValueError(
                "runtime_sweep received no input entities and OMNI_HOME is "
                "not set — cannot resolve the default contract set. Set "
                "OMNI_HOME or pass contracts/topics/probes explicitly."
            )

        contracts = collect_contracts(omni_home, request.scope or "all-repos")
        all_publish: list[str] = []
        all_subscribe: list[str] = []
        for contract in contracts:
            all_publish.extend(contract.publish_topics)
            all_subscribe.extend(contract.subscribe_topics)
        updates: dict[str, object] = {
            "contracts": contracts,
            "topic_producers": all_publish,
            "topic_consumers": all_subscribe,
        }

        # OMN-14528: collect the live consumer-group census in code (broker
        # probe) so the CONSUMER_LIVENESS deadness check actually runs on the
        # skill dispatch path. Fail closed when the phase is enabled but no
        # broker is configured — a runtime sweep that cannot reach the broker
        # cannot verify liveness, and reporting "clean" would be the exact
        # vacuous green this fix exists to prevent.
        #
        # Skip the probe when the default walk found NO contracts: there is
        # nothing to verify liveness for, and the empty-tree case belongs to
        # the zero-entity hard-fail guard in ``handle`` (OMN-13919), which must
        # not be shadowed by the broker requirement.
        if contracts and NodeRuntimeSweep._enabled(
            request, EnumSweepCheck.CONSUMER_LIVENESS
        ):
            from omnimarket.nodes.node_runtime_sweep.broker_probe import (
                collect_live_consumer_groups,
            )

            bootstrap_servers = os.environ.get("KAFKA_BOOTSTRAP_SERVERS")
            if not bootstrap_servers:
                raise ValueError(
                    "runtime_sweep default sweep enables the CONSUMER_LIVENESS "
                    "check but KAFKA_BOOTSTRAP_SERVERS is not set — cannot probe "
                    "the broker for the live consumer-group census. Set "
                    "KAFKA_BOOTSTRAP_SERVERS (fail closed; the census is never a "
                    "silent skip, OMN-14528), or scope enabled_checks to exclude "
                    "CONSUMER_LIVENESS for a static-only sweep."
                )
            updates["live_consumer_groups"] = collect_live_consumer_groups(
                bootstrap_servers
            )
            updates["require_live_consumer_census"] = True

        return request.model_copy(update=updates)

    def handle(self, request: RuntimeSweepRequest) -> RuntimeSweepResult:
        """Execute the runtime sweep.

        Each phase is gated on ``_enabled``. With ``enabled_checks=None`` every
        phase runs exactly as before (REGISTRATION emits nothing when there are
        no entry-point probes), so existing cross-repo/skill callers are
        unaffected. A named subset (e.g. [REGISTRATION]) scopes the sweep.

        A request with no entities resolves the default ``$OMNI_HOME``
        contract set first; a run that still checks zero entities raises
        instead of returning, so a vacuous pass is unrepresentable
        (OMN-13919).
        """
        request = self._resolve_default_input(request)
        findings: list[ModelRuntimeFinding] = []

        # REGISTRATION phase (OMN-13589): single-repo entry-point probes.
        # Emits nothing when no probes were supplied, so the default full sweep
        # is unaffected.
        if self._enabled(request, EnumSweepCheck.REGISTRATION):
            findings.extend(self._check_registration(request.entry_point_probes))

        # Phase 1: Node description audit
        if self._enabled(request, EnumSweepCheck.DESCRIPTION):
            for contract in request.contracts:
                findings.extend(self._check_description(contract))

        # Phase 2: Handler wiring audit
        if self._enabled(request, EnumSweepCheck.WIRING):
            for contract in request.contracts:
                findings.extend(self._check_wiring(contract))

        # Phase 3: Topic symmetry audit
        all_producers = set(request.topic_producers)
        all_consumers = set(request.topic_consumers)
        for contract in request.contracts:
            all_producers.update(contract.publish_topics)
            all_consumers.update(contract.subscribe_topics)

        all_topics = all_producers | all_consumers
        if self._enabled(request, EnumSweepCheck.SYMMETRY):
            findings.extend(
                self._check_symmetry(all_topics, all_producers, all_consumers)
            )

        # Phase 4: Contract-store durability audit (OMN-12962)
        # Flag any live-registered contract that is not reconstructable from a
        # durable cold-start source — these depend on retained registration
        # events and silently vanish on a cold runtime restart.
        if self._enabled(request, EnumSweepCheck.DURABILITY):
            findings.extend(
                self._check_census_durability(
                    request.contracts, request.durable_node_names
                )
            )

        # Phase 5: Stranded-workflow audit (FSM terminal-state invariant, OMN-12959).
        if self._enabled(request, EnumSweepCheck.STRANDED_WORKFLOW):
            findings.extend(
                self._check_stranded_workflows(
                    request.workflow_observations, request.archetype_sla_ms
                )
            )
        # Phase 6 (OMN-12957/OMN-14528): per-contract live-consumer-group check.
        # Each subscribing, runtime-deployed contract must have its OWN node-name
        # identity present in a live consumer group on the broker — a sibling's
        # live group in the same runtime_profile no longer vouches for it. The
        # census is collected by a live broker probe (never a hand-typed flag),
        # and when it is REQUIRED, its absence or emptiness is a HARD FAILURE,
        # never a silent skip: that silent skip is exactly how all 13 dead
        # contract-declared consumers passed this check for months.
        consumers_checked = 0
        live_groups_scanned = 0
        if self._enabled(request, EnumSweepCheck.CONSUMER_LIVENESS):
            census = request.live_consumer_groups
            if request.require_live_consumer_census and census is None:
                raise ValueError(
                    "runtime_sweep CONSUMER_LIVENESS check is required but the "
                    "live consumer-group census is None — the broker was never "
                    "probed. Absence of the census is a FAILURE, never a silent "
                    "skip (OMN-14528). Collect it via "
                    "broker_probe.collect_live_consumer_groups(...) and pass the "
                    "result."
                )
            if census is not None:
                live_groups_scanned = len(census)
                # scanned_count > 0 assertion: a reachable broker serving a
                # runtime lane with deployed consumers MUST report live groups.
                # Zero live groups while the check is required means the probe
                # hit the wrong/empty broker or nothing is attached — refuse to
                # render a liveness verdict over an empty scan (the "green over
                # nothing" disease). A genuinely-static run must scope
                # enabled_checks to exclude CONSUMER_LIVENESS.
                if request.require_live_consumer_census and live_groups_scanned == 0:
                    raise ValueError(
                        "runtime_sweep CONSUMER_LIVENESS check is required but "
                        "the broker probe returned ZERO live consumer groups — "
                        "refusing to report liveness over an empty scan "
                        "(scanned_count must be > 0). The probe target is wrong "
                        "or no consumer is attached (OMN-14528)."
                    )
                liveness_findings, consumers_checked = self._check_consumer_liveness(
                    request.contracts, census
                )
                findings.extend(liveness_findings)

        entities_checked = (
            len(request.contracts)
            + len(all_topics)
            + len(request.workflow_observations)
            + len(request.entry_point_probes)
        )
        # OMN-13708 established that 0 entities checked is NOT a clean pass.
        # OMN-13919 hardens it: a zero-entity run FAILS (raises) instead of
        # returning a reportable ``no_input`` result, because the dispatch
        # layer mapped any returned result to exit_code=0/status=success —
        # exactly the vacuous false-green this class of fix exists to prevent.
        if entities_checked == 0:
            raise ValueError(
                "runtime_sweep checked zero entities (contracts, topics, "
                "workflows, entry points) — refusing to report a vacuous "
                "pass. Default $OMNI_HOME collection found no contract.yaml "
                "files; the environment is broken or the input wiring "
                "regressed (OMN-13919)."
            )
        status = "findings" if findings else "clean"

        return RuntimeSweepResult(
            findings=findings,
            contracts_checked=len(request.contracts),
            topics_checked=len(all_topics),
            workflows_checked=len(request.workflow_observations),
            entry_points_checked=len(request.entry_point_probes),
            consumers_checked=consumers_checked,
            live_consumer_groups_scanned=live_groups_scanned,
            status=status,
            dry_run=request.dry_run,
        )

    def _check_registration(
        self, probes: list[ModelEntryPointProbe]
    ) -> list[ModelRuntimeFinding]:
        """Turn failed entry-point probes into BROKEN_ENTRY_POINT findings.

        Pure: the import/structural work happens in the harness; this method only
        classifies the pre-collected probe results.
        """
        findings: list[ModelRuntimeFinding] = []
        for probe in probes:
            if probe.ok:
                continue
            findings.append(
                ModelRuntimeFinding(
                    finding_type=EnumFindingType.BROKEN_ENTRY_POINT,
                    subject=probe.node_name,
                    message=f"{probe.module_path}: {probe.reason}",
                    severity="CRITICAL",
                )
            )
        return findings

    @staticmethod
    def _classify_watchdog(
        observation: ModelWorkflowObservation,
    ) -> EnumWatchdogEventType:
        """Classify the typed watchdog class for a stranded workflow.

        Precedence: unroutable (no path to advance) > stalled (alive but wedged)
        > timeout (ran past SLA). Unroutable and stalled are structural holes;
        timeout is the residual catch-all when the workflow could in principle
        advance but did not within SLA.
        """
        if not observation.has_routable_next:
            return EnumWatchdogEventType.WORKFLOW_UNROUTABLE
        if not observation.making_progress:
            return EnumWatchdogEventType.WORKFLOW_STALLED
        return EnumWatchdogEventType.WORKFLOW_TIMEOUT

    def _check_stranded_workflows(
        self,
        observations: list[ModelWorkflowObservation],
        archetype_sla_ms: dict[str, int],
    ) -> list[ModelRuntimeFinding]:
        """Flag workflows started-but-not-terminal past their archetype SLA.

        The FSM terminal-state invariant (OMN-12959): every workflow FSM must
        reach a terminal state or trip a watchdog. A workflow that has not reached
        terminal and whose elapsed time exceeds the archetype SLA is stranded —
        silent absence in every projection — and is reported with its typed
        watchdog class so the runtime sweep can auto-ticket by failure class.
        """
        findings: list[ModelRuntimeFinding] = []

        for obs in observations:
            if obs.reached_terminal:
                continue
            sla_ms = archetype_sla_ms.get(obs.archetype, DEFAULT_ARCHETYPE_SLA_MS)
            if obs.elapsed_ms <= sla_ms:
                continue

            watchdog_type = self._classify_watchdog(obs)
            findings.append(
                ModelRuntimeFinding(
                    finding_type=EnumFindingType.STRANDED_WORKFLOW,
                    subject=f"{obs.archetype}:{obs.correlation_id}",
                    message=(
                        f"Workflow {obs.correlation_id} (archetype {obs.archetype}) "
                        f"stranded in {obs.workflow_state} for {obs.elapsed_ms}ms "
                        f"with no terminal event (SLA {sla_ms}ms); "
                        f"watchdog class {watchdog_type.value}"
                    ),
                    severity="CRITICAL",
                )
            )

        return findings

    def _check_description(
        self, contract: ModelContractInput
    ) -> list[ModelRuntimeFinding]:
        """Check if node description is real, placeholder, or missing."""
        findings: list[ModelRuntimeFinding] = []
        desc = contract.description.strip()

        if not desc:
            findings.append(
                ModelRuntimeFinding(
                    finding_type=EnumFindingType.MISSING_DESCRIPTION,
                    subject=contract.node_name,
                    message=f"Node {contract.node_name} has no description",
                    severity="WARNING",
                )
            )
        elif len(desc) < 10 or any(p.match(desc) for p in _PLACEHOLDER_PATTERNS):
            findings.append(
                ModelRuntimeFinding(
                    finding_type=EnumFindingType.PLACEHOLDER_DESCRIPTION,
                    subject=contract.node_name,
                    message=f"Node {contract.node_name} has placeholder description: {desc[:50]}",
                    severity="WARNING",
                )
            )

        return findings

    def _check_wiring(self, contract: ModelContractInput) -> list[ModelRuntimeFinding]:
        """Check if handler is properly wired."""
        findings: list[ModelRuntimeFinding] = []

        if not contract.handler_exists and contract.handler_module:
            findings.append(
                ModelRuntimeFinding(
                    finding_type=EnumFindingType.UNWIRED_HANDLER,
                    subject=contract.node_name,
                    message=f"Handler {contract.handler_module} declared but not found",
                    severity="CRITICAL",
                )
            )

        return findings

    def _check_census_durability(
        self,
        contracts: list[ModelContractInput],
        durable_node_names: list[str] | None,
    ) -> list[ModelRuntimeFinding]:
        """Audit cold-start durability of the registered contract census.

        The cold-start census is reconstructed from the image-bundled
        filesystem manifest (HYBRID-mode bootstrap + PluginLoaderContractSource),
        which is durable and independent of Kafka retention. The post-freeze
        dynamic-contract listener consumes the ``SUFFIX_NODE_REGISTRATION``
        topic (``cleanup.policy=delete``, 7-day retention) with
        ``auto_offset_reset=latest`` — it never replays history on a cold start.

        Therefore any contract present in the live registered census but absent
        from the durable source is NON-durable: it was materialized only via the
        dynamic path and will vanish on a cold runtime restart unless its
        registrant re-publishes. This check surfaces those contracts.

        When ``durable_node_names`` is None the caller supplied no manifest and
        the check is skipped (no finding emitted).
        """
        if durable_node_names is None:
            return []

        findings: list[ModelRuntimeFinding] = []
        durable = set(durable_node_names)

        for contract in contracts:
            if contract.node_name not in durable:
                findings.append(
                    ModelRuntimeFinding(
                        finding_type=EnumFindingType.NON_DURABLE_CONTRACT,
                        subject=contract.node_name,
                        message=(
                            f"Contract {contract.node_name} is registered in the "
                            f"live runtime but absent from the durable cold-start "
                            f"census (filesystem manifest). It was materialized "
                            f"only via the dynamic node-registration listener "
                            f"(cleanup.policy=delete topic, auto_offset_reset="
                            f"latest) and will not be reconstructed on a cold "
                            f"restart unless its registrant re-publishes."
                        ),
                        severity="CRITICAL",
                    )
                )

        return findings

    def _check_symmetry(
        self,
        all_topics: set[str],
        producers: set[str],
        consumers: set[str],
    ) -> list[ModelRuntimeFinding]:
        """Check topic producer/consumer symmetry."""
        findings: list[ModelRuntimeFinding] = []

        for topic in sorted(all_topics):
            has_producer = topic in producers
            has_consumer = topic in consumers

            if has_producer and not has_consumer:
                findings.append(
                    ModelRuntimeFinding(
                        finding_type=EnumFindingType.PRODUCER_ONLY,
                        subject=topic,
                        message=f"Topic {topic} has producer but no consumer",
                        severity="WARNING",
                    )
                )
            elif has_consumer and not has_producer:
                findings.append(
                    ModelRuntimeFinding(
                        finding_type=EnumFindingType.CONSUMER_ONLY,
                        subject=topic,
                        message=f"Topic {topic} has consumer but no producer",
                        severity="WARNING",
                    )
                )

        return findings

    @staticmethod
    def _node_has_live_consumer(node_name: str, padded_live_groups: list[str]) -> bool:
        """True when a live group id carries THIS node's own consumer identity.

        The canonical consume group id is
        ``{env}.{service}.{node_name}.{consume}.{version}[.__i.{instance}][.__t.{topic}]``
        (``compute_consumer_group_id`` + the per-instance/per-topic suffixes the
        runtime appends). So a node's identity is present in a group id iff the
        segment ``.{normalized_node_name}.{consume}.`` appears in it. The
        matcher:

        * normalizes ``node_name`` with the SAME ``normalize_kafka_identifier``
          the runtime uses, so casing/separator differences never cause a false
          miss, and
        * anchors on BOTH the leading ``.`` and the ``.{consume}.`` suffix, so a
          longer sibling name never yields a false match (``node_foo`` must not
          be vouched for by ``node_foobar``'s group) — the exact prefix-collision
          bug a bare ``node_name in group_id`` substring test would introduce.

        ``padded_live_groups`` are the live group ids lower-cased and wrapped as
        ``f".{group}."`` so the leading/trailing boundary anchors always have a
        character to match against.
        """
        try:
            nnc = normalize_kafka_identifier(node_name)
        except ValueError:
            # A node whose name normalizes to empty can form no valid group id,
            # so it can never have a live consumer group — treat as not live.
            return False
        needle = f".{nnc}.{_CONSUME_PURPOSE_SEGMENT}."
        return any(needle in group for group in padded_live_groups)

    def _check_consumer_liveness(
        self,
        contracts: list[ModelContractInput],
        live_consumer_groups: list[str],
    ) -> tuple[list[ModelRuntimeFinding], int]:
        """OMN-12957/OMN-14528: per-CONTRACT live-consumer-group identity check.

        A node may declare a valid, registered runtime_profile yet have no
        process actually consuming for it (the runtime lane is not deployed /
        not attached, or the consumer died). Static profile-subset validation
        passes but the node is still a silent orphan.

        The PRE-fix check (``_check_profile_consumer_census``) compared each
        subscribing node's declared *profiles* against the set of *profile
        names* with a live consumer group (``declared_profiles & live_profiles``)
        — a profile-level vouching bug: ONE live group anywhere in a profile made
        EVERY other contract sharing that profile name look healthy. That is
        precisely how 13 dead contract-declared consumers passed this check for
        months (OMN-14516/OMN-14517), and they still passed even with a perfect
        census.

        The fixed check is per-contract, not per-profile: it requires the
        contract's OWN node-name identity to appear in a LIVE (non-Empty)
        consumer group id (see ``_node_has_live_consumer``). A live group
        belonging to a sibling contract in the same runtime_profile no longer
        vouches for this contract.

        Scope: a contract is evaluated iff it BOTH subscribes to at least one
        topic AND declares a ``runtime_profiles`` (the auto-wired-runtime-
        consumer signal — a library/compute node without a runtime profile is
        not a deployed consumer and is not expected to hold a consumer group).
        This is the same scope the runtime's own profile-ownership filter uses;
        the 13 dead consumers are runtime-deployed and declare a profile, so
        they remain in scope.

        Returns:
            ``(findings, consumers_checked)`` where ``consumers_checked`` is the
            number of in-scope contracts actually evaluated. The caller asserts
            it is > 0 before it can report a clean verdict.
        """
        # Lower-case + boundary-pad the live census once for anchored matching.
        padded_live = [
            f".{group.strip().lower()}."
            for group in live_consumer_groups
            if group.strip()
        ]
        findings: list[ModelRuntimeFinding] = []
        consumers_checked = 0
        for contract in contracts:
            # Only nodes that subscribe can be orphaned at the consumer level.
            if not contract.subscribe_topics:
                continue
            declared = {
                p.strip().lower() for p in contract.runtime_profiles if p.strip()
            }
            if not declared:
                continue
            consumers_checked += 1

            if self._node_has_live_consumer(contract.node_name, padded_live):
                continue
            findings.append(
                ModelRuntimeFinding(
                    finding_type=EnumFindingType.CONTRACT_NO_LIVE_CONSUMER,
                    subject=contract.node_name,
                    message=(
                        f"Node {contract.node_name} subscribes "
                        f"{sorted(contract.subscribe_topics)} under runtime_profiles "
                        f"{sorted(declared)} but NO live consumer group carries its "
                        f"node-name identity ({len(live_consumer_groups)} live "
                        "group(s) scanned). Its subscriptions are not being "
                        "drained — silent orphan. This is a per-contract identity "
                        "check (OMN-14528): a live group belonging to a different "
                        "node in the same runtime_profile does NOT vouch for this "
                        "contract."
                    ),
                    severity="CRITICAL",
                )
            )
        return findings, consumers_checked
