"""NodeRuntimeSweep — Runtime registration and wiring verification.

Checks node descriptions, handler wiring, topic symmetry (producer/consumer
pairs), and classifies findings by type and severity.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
"""

from __future__ import annotations

import re
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.watchdog import EnumWatchdogEventType

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
    dry_run: bool = False


class RuntimeSweepResult(BaseModel):
    """Output of the runtime sweep handler."""

    model_config = ConfigDict(extra="forbid")

    findings: list[ModelRuntimeFinding] = Field(default_factory=list)
    contracts_checked: int = 0
    topics_checked: int = 0
    workflows_checked: int = 0
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

    Pure compute handler — operates on pre-collected contract metadata.
    """

    def handle(self, request: RuntimeSweepRequest) -> RuntimeSweepResult:
        """Execute the runtime sweep."""
        findings: list[ModelRuntimeFinding] = []

        # Phase 1: Node description audit
        for contract in request.contracts:
            findings.extend(self._check_description(contract))

        # Phase 2: Handler wiring audit
        for contract in request.contracts:
            findings.extend(self._check_wiring(contract))

        # Phase 3: Topic symmetry audit
        all_producers = set(request.topic_producers)
        all_consumers = set(request.topic_consumers)
        for contract in request.contracts:
            all_producers.update(contract.publish_topics)
            all_consumers.update(contract.subscribe_topics)

        all_topics = all_producers | all_consumers
        findings.extend(self._check_symmetry(all_topics, all_producers, all_consumers))

        # Phase 4: Contract-store durability audit (OMN-12962)
        # Flag any live-registered contract that is not reconstructable from a
        # durable cold-start source — these depend on retained registration
        # events and silently vanish on a cold runtime restart.
        findings.extend(
            self._check_census_durability(request.contracts, request.durable_node_names)
        )

        # Phase 5: Stranded-workflow audit (FSM terminal-state invariant, OMN-12959).
        findings.extend(
            self._check_stranded_workflows(
                request.workflow_observations, request.archetype_sla_ms
            )
        )

        status = "clean" if not findings else "findings"

        return RuntimeSweepResult(
            findings=findings,
            contracts_checked=len(request.contracts),
            topics_checked=len(all_topics),
            workflows_checked=len(request.workflow_observations),
            status=status,
            dry_run=request.dry_run,
        )

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
