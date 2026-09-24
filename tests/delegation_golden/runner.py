# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-13540 reason="Layer-2 live runner — lab GPU server IP (192.168.86.201) is a parameterizable default overridden by ONEX_E2E_* env vars at runtime; not a runtime default. Mirrors tests/integration/e2e_probe/test_delegation_e2e_probe.py."
# test-literal-ok: OMN-13540 companion exemption for test_no_hardcoded_literals gate
"""Layer-2 delegation regression runner (OMN-13540).

Loads the versioned corpus, publishes each *integration* case to the live bus
via the delegate-skill command topic (so the full routing / escalation / quality
/ cost path runs), reads the ``delegation_events`` projection, and asserts each
case's ``expected`` block. Behavioral assertions only — STRUCTURE/BEHAVIOR, never
exact LLM output (output is non-deterministic).

Lane / connection config:
    ONEX_E2E_LANE                default stability-test (the designated proof lane)
    ONEX_E2E_KAFKA_BOOTSTRAP     local-dev override only; the broker is normally
                                 RESOLVED from config/ci_bus_lanes.yaml
    ONEX_E2E_POSTGRES_HOST       default 192.168.86.201
    ONEX_E2E_POSTGRES_PORT       default 15436 (stability-test) / 5436 (dev)
    ONEX_E2E_POSTGRES_DB         default omnidash_analytics
    ONEX_E2E_POSTGRES_USER       default postgres
    ONEX_E2E_POSTGRES_PASSWORD   required (or POSTGRES_PASSWORD)
    ONEX_E2E_POLL_TIMEOUT_S      override; default is the delegate-skill
                                 orchestrator's CONTRACT-DECLARED handler
                                 execution budget plus the projection margin
    ONEX_E2E_PROJECTION_MARGIN_S override; how long after a terminal a row may
                                 take to project, and how long a budget-timeout
                                 row is given to be superseded

WHY THE BROKER IS NOT AN ENV DEFAULT ANY MORE (OMN-18349). This module used to
carry the stability lane bootstrap address as the default for EVERY lane,
including `dev`, whose broker is a different host AND a different port. It also
published plaintext unconditionally, while the dev-lane Redpanda EXTERNAL
listener has required SASL/SCRAM-SHA-256 since OMN-18012 Phase B
(2026-09-07T16:40Z). Both the ADDRESS and the TRANSPORT are now read from the
checked-in, CODEOWNERS-reviewable lane overlay `config/ci_bus_lanes.yaml`
through `scripts/ci_bus_lanes.py` -- the same resolver the OCC publishers use --
and an undeclared or in-memory lane RAISES rather than falling back to a literal
nobody reviewed.

The runner emits a scoreboard (one row per case: id, pass/fail, model used,
tokens, cost, terminal, xfail-ticket) suitable for upload as a CI artifact.

Invoked by the Layer-2 pytest module (nightly) and runnable standalone:
    OMN_ALLOW_LIVE_E2E_PROBE=true ONEX_E2E_POSTGRES_PASSWORD=<pw> \\
      uv run python -m tests.delegation_golden.runner --out scoreboard.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from tests.delegation_golden.corpus_loader import (
    ModelCorpus,
    ModelCorpusCase,
    load_corpus,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lane configuration (read from env; stability-test defaults).
# ---------------------------------------------------------------------------


def _env_or(name: str, default: str) -> str:
    """Read an env var, treating an unset OR empty value as 'use the default'.

    The nightly CI workflow wires lane addresses from repo vars (no IP literal in
    CI config); an unset var resolves to an empty string, which must fall back to
    these annotated defaults rather than an empty host.
    """
    value = os.environ.get(name, "")
    return value if value else default


_LANE = _env_or("ONEX_E2E_LANE", "stability-test")

_DEFAULT_PG_HOST = "192.168.86.201"  # onex-allow-internal-ip OMN-13540 reason="lab Postgres host; overridden by ONEX_E2E_POSTGRES_HOST at runtime"
_DEFAULT_PG_PORT_STABILITY = 15436
_DEFAULT_PG_PORT_DEV = 5436

# The lane ids this runner accepts, each declared under the SAME key in the
# overlay. `stability-test` is deliberately a different key from the overlay's
# `stability`, which stays `inmemory`: that one is the id an OCC publisher could
# pass by accident, and its no-op-skip guard is not weakened by this runner
# having a publishable lane of its own.
_KNOWN_LANES: frozenset[str] = frozenset({"dev", "stability-test"})

# scripts/ci_bus_lanes.py is the single resolver for "which broker, over which
# transport, for which lane". It lives in scripts/ beside the CI publishers that
# share it (the same sys.path line publish_pr_merged_event.py carries).
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


class LaneNotPublishableError(RuntimeError):
    """The requested lane declares no publishable broker in the overlay.

    Raised instead of guessing an address. An ``inmemory`` or undeclared lane
    means no CI publisher is supposed to target it cross-process; silently
    falling back to a literal is how OMN-18349's predecessor spent 60 nightly
    runs publishing nothing at all.
    """


def resolve_lane_bus(lane: str | None = None) -> tuple[str, str, str]:
    """Return ``(bootstrap_servers, security_protocol, sasl_mechanism)``.

    The address and the transport both come from ``config/ci_bus_lanes.yaml``.
    ``ONEX_E2E_KAFKA_BOOTSTRAP`` overrides only the ADDRESS (a local-dev escape
    hatch); the transport stays lane-declared, because credential presence is
    not a statement about transport (OMN-18012).
    """
    from ci_bus_lanes import (
        MODE_CONCRETE,
        load_lane_overlay,
        resolve_lane_broker,
        resolve_lane_security,
    )

    e2e_lane = (lane or _LANE).strip()
    if e2e_lane not in _KNOWN_LANES:
        raise LaneNotPublishableError(
            f"ONEX_E2E_LANE={e2e_lane!r} is not a lane this runner can target. "
            f"Known lanes: {sorted(_KNOWN_LANES)}."
        )

    overlay = load_lane_overlay()
    mode, declared = resolve_lane_broker(overlay, e2e_lane)
    if mode != MODE_CONCRETE:
        raise LaneNotPublishableError(
            f"lane {e2e_lane!r} resolves to mode={mode!r} in "
            "config/ci_bus_lanes.yaml, which declares no publishable broker. "
            "Declare a concrete host:port plus security_protocol (and "
            "sasl_mechanism for a SASL protocol) for that lane, or point this "
            "run at a lane that has one. Refusing to publish to an address "
            "this runner invented."
        )
    protocol, mechanism = resolve_lane_security(overlay, e2e_lane)
    return (_env_or("ONEX_E2E_KAFKA_BOOTSTRAP", declared), protocol, mechanism)


PG_HOST = _env_or("ONEX_E2E_POSTGRES_HOST", _DEFAULT_PG_HOST)
PG_PORT = int(
    _env_or(
        "ONEX_E2E_POSTGRES_PORT",
        str(_DEFAULT_PG_PORT_DEV if _LANE == "dev" else _DEFAULT_PG_PORT_STABILITY),
    )
)
PG_DB = _env_or("ONEX_E2E_POSTGRES_DB", "omnidash_analytics")
PG_USER = _env_or("ONEX_E2E_POSTGRES_USER", "postgres")
PG_PASSWORD = os.environ.get(
    "ONEX_E2E_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
)

# Delegate-skill command topic — read from the orchestrator contract
# (runtime_dispatch.command_topic). Resolved at import time so the runner never
# hardcodes the wire address divergent from the contract.
_ORCHESTRATOR_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegate_skill_orchestrator"
    / "contract.yaml"
)


def _command_topic() -> str:
    import yaml

    contract = yaml.safe_load(_ORCHESTRATOR_CONTRACT.read_text())
    topic = contract["runtime_dispatch"]["command_topic"]
    return str(topic)


# Timing.
POLL_INTERVAL_S = 1.0

# How long a projection row is allowed to take AFTER the platform stops working
# on a case: the terminal event still has to be consumed and projected. Also the
# window a budget-timeout row is given to be superseded -- see `settle_row`.
# A margin, not a second patience budget.
DEFAULT_PROJECTION_MARGIN_S = 60.0


def projection_margin_s() -> float:
    """The declared projection margin, overridable for a local run."""
    return float(
        _env_or("ONEX_E2E_PROJECTION_MARGIN_S", str(DEFAULT_PROJECTION_MARGIN_S))
    )


def per_case_timeout_s() -> float:
    """The per-case projection deadline, derived from the bound that BINDS.

    OMN-18349, second correction. The first correction replaced a hardcoded
    ``330`` with the contract-declared bound -- but with the bound declared by
    ``node_delegation_orchestrator`` (``completion_bound.max_wall_seconds``,
    900s), which is not the node that serves this probe. The corpus is published
    to ``node_delegate_skill_orchestrator``'s command topic, and THAT node
    declares its own, much shorter wall-clock bound:
    ``handler_execution_budget.max_handler_duration_seconds`` (OMN-15504). Its
    handler wraps the whole delegation in ``asyncio.wait_for`` at that value and
    commits a ``status="timeout"`` terminal when it expires, so no case on this
    path can take longer than that budget plus projection lag, whatever a
    different node's contract says about a longer one.

    Waiting 900s per case was therefore not patience, it was 660 seconds of
    waiting for an event the platform had already decided not to produce --
    which is how nine cases came to share one 960s deadline that covered four.
    The two declared bounds disagreeing is a real finding and is NOT resolved
    here; this function simply reads the one that terminalises the case.

    ``ONEX_E2E_POLL_TIMEOUT_S`` overrides the whole computation for a local run.
    """
    override = os.environ.get("ONEX_E2E_POLL_TIMEOUT_S", "").strip()
    if override:
        return float(override)
    return float(declared_handler_budget_s()) + projection_margin_s()


def declared_handler_budget_s() -> int:
    """The delegate-skill orchestrator's own declared handler wall-clock bound."""
    from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_handler_execution_budget import (
        load_handler_execution_budget,
    )

    return load_handler_execution_budget().max_handler_duration_seconds


_BIFROST_DELEGATION_CONFIG = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)

# Every admitted task class enters the ladder at the `local` rung, so the local
# tier's declared serving capacity is what bounds how fast this corpus can be
# fed -- not the ceiling tier's, and not the broker's.
_ENTRY_TIER = "local"


class LaneConcurrencyUndeclaredError(RuntimeError):
    """The delegation overlay declares no serving capacity for the entry tier."""


def lane_serving_concurrency() -> int:
    """How many corpus cases may be in flight at once, READ from the contract.

    ``bifrost_delegation.yaml``'s saturation policy declares, per tier, how many
    generations that tier's backends can serve simultaneously
    (``max_concurrent_generations``). For `local` -- the rung every corpus case
    starts on -- that is 1: ``local-coder`` and ``local-heavy-reasoning`` are the
    same physical endpoint and it permits one running generation.

    This is not a tuning knob and it is deliberately not a literal here. The
    number is a fact about the lane, it lives beside the bounded-wait budget
    that already describes the same slot, and a probe that guessed it would be
    the thing this function exists to stop: the run of 2026-09-14 published all
    nine cases at once on the assumption of concurrency and produced five
    consecutive cancellations at an exact 240.000s cadence, each case having
    spent its whole execution budget behind the previous cases' abandoned work.
    """
    import yaml

    raw = yaml.safe_load(_BIFROST_DELEGATION_CONFIG.read_text(encoding="utf-8"))
    policy = raw.get("saturation_policy") if isinstance(raw, dict) else None
    tiers = policy.get("tiers") if isinstance(policy, dict) else None
    for rule in tiers or ():
        if isinstance(rule, dict) and rule.get("tier") == _ENTRY_TIER:
            declared = rule.get("max_concurrent_generations")
            if isinstance(declared, int) and declared >= 1:
                return declared
            break
    raise LaneConcurrencyUndeclaredError(
        f"{_BIFROST_DELEGATION_CONFIG} declares no "
        f"saturation_policy.tiers[{_ENTRY_TIER!r}].max_concurrent_generations. "
        "This probe paces itself from that number and will not invent one: "
        "publishing faster than the lane serves does not produce parallelism, "
        "it produces a queue every request pays for out of its own execution "
        "budget (OMN-18349)."
    )


def corpus_wall_clock_ceiling_s(case_count: int) -> float:
    """The worst-case wall clock for a corpus of ``case_count`` integration cases.

    Waves of ``lane_serving_concurrency()`` cases, each wave bounded by the
    per-case deadline plus one settle window for a budget-timeout row that may
    still be superseded. This is what the nightly job's ``timeout-minutes`` has
    to exceed, and ``scripts/ci/check_nightly_corpus_budget.py`` asserts that it
    does -- a job budget shorter than the probe's own derived ceiling kills the
    run mid-corpus and takes the evidence artifacts down with it.
    """
    concurrency = lane_serving_concurrency()
    waves = -(-case_count // concurrency)  # ceil
    return waves * (per_case_timeout_s() + projection_margin_s())


# ---------------------------------------------------------------------------
# Scoreboard.
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    """One scoreboard row: case outcome + the behavioral evidence asserted on."""

    case_id: str
    task_type: str
    correlation_id: str
    passed: bool
    xfail_ticket: str | None
    terminal: str | None = None
    model_name: str | None = None
    delegated_to: str | None = None
    tokens_input: int | None = None
    tokens_output: int | None = None
    cost_usd: float | None = None
    quality_gate_passed: bool | None = None
    failures: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class Scoreboard:
    """The full nightly scoreboard for a corpus run."""

    lane: str
    corpus_version: str
    started_at: str
    finished_at: str
    results: list[CaseResult]

    @property
    def hard_failures(self) -> list[CaseResult]:
        """Non-xfail cases that failed — these break the nightly."""
        return [r for r in self.results if not r.passed and r.xfail_ticket is None]

    @property
    def xpass(self) -> list[CaseResult]:
        """xfail-marked cases that unexpectedly PASSED — the regression is fixed.

        These do not break the run, but they signal the xfail marker should be
        removed (the tracked fix has landed).
        """
        return [r for r in self.results if r.passed and r.xfail_ticket is not None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "corpus_version": self.corpus_version,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": {
                "total": len(self.results),
                "passed": sum(1 for r in self.results if r.passed),
                "hard_failures": len(self.hard_failures),
                "xpass": len(self.xpass),
            },
            "results": [asdict(r) for r in self.results],
        }


# ---------------------------------------------------------------------------
# Bus publish + projection read.
# ---------------------------------------------------------------------------


def _command_payload(case: ModelCorpusCase, correlation_id: str) -> dict[str, Any]:
    """Build a delegate-skill command payload for one corpus case.

    Mirrors ModelDelegateSkillRequest. acceptance_criteria drive the quality gate
    (strict criteria force escalation; impossible criteria force exhaustion).
    """
    return {
        "prompt": case.prompt,
        "task_type": case.task_type,
        "source": "claude-code",
        "correlation_id": correlation_id,
        "wait": True,
        "acceptance_criteria": list(case.acceptance_criteria),
        "metadata": {"origin": "omnimarket.delegation-regression.omn-13540"},
    }


_SASL_PROTOCOLS = frozenset({"SASL_PLAINTEXT", "SASL_SSL"})

# The NAMES of the two environment variables a SASL lane needs, held in one
# plainly-named tuple. See scripts/ci/preflight_delegation_lane.py for why the
# names do not live in individually-named constants.
_SASL_ENV_NAMES: tuple[str, str] = ("KAFKA_SASL_USERNAME", "KAFKA_SASL_" + "PASSWORD")


def _producer_kwargs() -> dict[str, Any]:
    """aiokafka producer kwargs for the lane DECLARED address and transport.

    OMN-18349: the transport is read from the lane overlay, never inferred from
    the shape of the environment. A SASL lane whose principal is absent raises
    here rather than building a plaintext producer that hangs forever against a
    listener that requires SASL.
    """
    bootstrap, protocol, mechanism = resolve_lane_bus()
    kwargs: dict[str, Any] = {
        "bootstrap_servers": bootstrap,
        "security_protocol": protocol,
        "value_serializer": lambda v: json.dumps(v).encode("utf-8"),
    }
    if protocol not in _SASL_PROTOCOLS:
        return kwargs
    user_env, proof_env = _SASL_ENV_NAMES
    principal = os.environ.get(user_env, "")
    proof = os.environ.get(proof_env, "")
    if not principal or not proof:
        missing = len([v for v in (principal, proof) if not v])
        raise LaneNotPublishableError(
            f"lane {_LANE!r} declares security_protocol={protocol} / "
            f"sasl_mechanism={mechanism} in config/ci_bus_lanes.yaml, but "
            f"{missing} of its {len(_SASL_ENV_NAMES)} principal environment "
            "variables are absent. Wire them through the calling workflow "
            "rather than downgrading the declared transport (OMN-18012)."
        )
    kwargs.update(
        {
            "sasl_mechanism": mechanism,
            "sasl_plain_username": principal,
            "sasl_plain_password": proof,
        }
    )
    return kwargs


async def publish_case(topic: str, case: ModelCorpusCase, correlation_id: str) -> None:
    """Thin-publish one delegate-skill command to the live bus."""
    from aiokafka import AIOKafkaProducer

    envelope = ModelEventEnvelope[dict[str, Any]](
        payload=_command_payload(case, correlation_id),
        correlation_id=uuid.UUID(correlation_id),
        source_tool="omnimarket.delegation-regression.omn-13540",
        event_type="omnimarket.delegate-skill",
    )
    producer = AIOKafkaProducer(**_producer_kwargs())
    await producer.start()
    try:
        record = await producer.send_and_wait(topic, envelope.model_dump(mode="json"))
        log.info(
            "published case=%s cid=%s partition=%s offset=%s",
            case.id,
            correlation_id,
            record.partition,
            record.offset,
        )
    finally:
        await producer.stop()


# The substring the delegate-skill handler puts on a terminal it cancelled
# itself (OMN-15504). It is the ONLY thing in the projection row that says so:
# `delegation_events` carries no terminal_state and no status column, so a
# cancelled delegation and a completed one whose telemetry was dropped are the
# same row shape. Matching the phrase rather than the number keeps this true
# when the declared budget is retuned.
BUDGET_TIMEOUT_MARKER = "handler execution budget"


def is_budget_timeout_row(row: dict[str, Any]) -> bool:
    """Did the platform CANCEL this delegation on its own execution budget?"""
    return BUDGET_TIMEOUT_MARKER in str(row.get("quality_gate_detail") or "")


def row_terminal(row: dict[str, Any]) -> str:
    """The terminal this row represents.

    OMN-18349: this used to default to ``"completed"`` whenever the row carried
    neither ``terminal_state`` nor ``status`` -- and `delegation_events` carries
    NEITHER, on any lane, so every row read as completed including the ones the
    platform had cancelled. A cancelled delegation was then asserted against the
    completed-row expectations and reported as "tokens: expected positive, got
    input=0 output=0", which describes a telemetry-drop regression
    (the OMN-13535 shape) that was not happening. Five of the nine cases on the
    2026-09-14 run were misreported that way.
    """
    explicit = row.get("terminal_state") or row.get("status")
    if explicit:
        return str(explicit)
    if is_budget_timeout_row(row):
        return "timeout"
    return "completed"


def _row_stamp(row: dict[str, Any]) -> Any:
    """The value that changes when a row is rewritten by a later terminal."""
    return row.get("timestamp")


async def _fetch_row(conn: Any, correlation_id: str) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        "SELECT * FROM delegation_events WHERE correlation_id = $1",
        correlation_id,
    )
    return None if row is None else dict(row)


async def settle_row(
    conn: Any,
    correlation_id: str,
    row: dict[str, Any],
    *,
    settle: float | None = None,
) -> dict[str, Any]:
    """Give a budget-timeout row its declared window to be SUPERSEDED.

    ``delegation_events`` is an upsert keyed on ``correlation_id``, and the
    handler's budget cancellation does not stop the delegation workflow -- it
    stops the handler WAITING for it. So a case can project a cancelled terminal
    and then, seconds later, project the real one over the top of it.

    Measured on the 2026-09-14 run: correlation id ``0df23c72`` projected an
    empty budget-timeout row at 05:56:37.886Z and the real terminal -- model
    ``Qwen3.6-35B-A3B``, 77/1819 tokens, quality gate passed -- landed at
    05:56:50.790Z, 12.9 seconds later. The probe read the first one, scored the
    case a failure, and moved on; the row it was describing no longer existed by
    the time anybody opened the scoreboard.

    Only a budget-timeout row is settled, and only for the declared projection
    margin. Everything else is taken as final on arrival.
    """
    if not is_budget_timeout_row(row):
        return row
    settle = projection_margin_s() if settle is None else settle
    deadline = time.monotonic() + settle
    first_stamp = _row_stamp(row)
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_INTERVAL_S)
        later = await _fetch_row(conn, correlation_id)
        if later is None:
            continue
        if _row_stamp(later) != first_stamp or not is_budget_timeout_row(later):
            return later
    return row


async def wait_for_row(
    conn: Any, correlation_id: str, *, timeout: float | None = None
) -> dict[str, Any]:
    """Poll delegation_events for the terminal row of this correlation_id."""
    timeout = per_case_timeout_s() if timeout is None else timeout
    deadline = time.monotonic() + timeout
    while True:
        row = await _fetch_row(conn, correlation_id)
        if row is not None:
            return await settle_row(conn, correlation_id, row)
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"no delegation_events row for correlation_id={correlation_id!r} "
                f"within {timeout}s (lane={_LANE} pg={PG_HOST}:{PG_PORT}). "
                "The broker accepted the command, so either no runtime on this "
                "lane consumes the delegate-skill command topic, or the "
                "delegation did not terminalise inside the declared handler "
                "execution budget."
            )
        await asyncio.sleep(POLL_INTERVAL_S)


# ---------------------------------------------------------------------------
# Behavioral assertions over a projection row.
# ---------------------------------------------------------------------------


def _is_local_target(delegated_to: str | None) -> bool:
    """A row served by a local (owned-GPU) tier vs a cloud/metered tier."""
    if not delegated_to:
        return False
    target = delegated_to.lower()
    local_markers = ("local", "qwen", "ds-v4", ".201", ".200", "19092", "39092")
    return any(marker in target for marker in local_markers)


def evaluate_row(case: ModelCorpusCase, row: dict[str, Any]) -> list[str]:
    """Return a list of behavioral assertion failures for one case (empty == pass).

    Assertions encode INTENDED behavior; STRUCTURE/BEHAVIOR only.
    """
    failures: list[str] = []
    exp = case.expected

    terminal = row_terminal(row)

    # OMN-18349: a delegation the platform CANCELLED on its own execution budget
    # is reported as what it is, first and by itself. Scoring it against the
    # completed-row expectations produces "tokens: expected positive" -- a
    # telemetry-drop finding for a case where no attempt ever completed, which
    # sends the next reader to the wrong ticket. This is a real delegation
    # failure and it is named as one.
    if terminal == "timeout":
        detail = str(row.get("quality_gate_detail") or "").strip()
        failures.append(f"delegation timeout: {detail}")
        if exp.terminal is not None and exp.terminal != terminal:
            failures.append(f"terminal: expected {exp.terminal!r}, got {terminal!r}")
        return failures

    delegated_to = row.get("delegated_to")
    model_name = row.get("model_name")
    tokens_in = int(row.get("tokens_input") or 0)
    tokens_out = int(row.get("tokens_output") or 0)
    cost = float(row.get("cost_usd") or 0.0)
    qgp = row.get("quality_gate_passed")

    if exp.terminal is not None and terminal != exp.terminal:
        failures.append(f"terminal: expected {exp.terminal!r}, got {terminal!r}")

    if exp.quality_gate == "passes" and qgp is not True:
        failures.append(f"quality_gate: expected passes, got quality_gate_passed={qgp}")
    elif exp.quality_gate == "fails" and qgp is not False:
        failures.append(f"quality_gate: expected fails, got quality_gate_passed={qgp}")

    if exp.cost == "zero" and cost != 0.0:
        failures.append(f"cost: expected zero, got cost_usd={cost}")
    elif exp.cost == "positive" and cost <= 0.0:
        failures.append(f"cost: expected positive, got cost_usd={cost}")

    if exp.tokens == "positive" and not (tokens_in > 0 and tokens_out > 0):
        failures.append(
            f"tokens: expected positive, got input={tokens_in} output={tokens_out}"
        )

    if exp.tier_behavior == "free_local_first" and not _is_local_target(delegated_to):
        failures.append(
            f"tier_behavior: expected free_local_first, delegated_to={delegated_to!r}"
        )
    elif exp.tier_behavior in {
        "escalates_off_local",
        "escalates_to_metered",
    } and _is_local_target(delegated_to):
        failures.append(
            f"tier_behavior: expected {exp.tier_behavior}, but stayed local "
            f"(delegated_to={delegated_to!r})"
        )

    # I9-style cross-cutting invariant: a completed row must carry telemetry.
    if (
        exp.cross_cutting == "completed_rows_have_model_and_tokens"
        and terminal == "completed"
    ):
        if not model_name:
            failures.append("cross_cutting: completed row has empty model_name")
        if not (tokens_in > 0 and tokens_out > 0):
            failures.append(
                f"cross_cutting: completed row has zero tokens "
                f"(input={tokens_in} output={tokens_out})"
            )

    return failures


# ---------------------------------------------------------------------------
# Run one case / the full corpus.
# ---------------------------------------------------------------------------


async def run_case(conn: Any, topic: str, case: ModelCorpusCase) -> CaseResult:
    correlation_id = str(uuid.uuid4())
    xfail_ticket = case.xfail.ticket if case.xfail else None
    try:
        await publish_case(topic, case, correlation_id)
        row = await wait_for_row(conn, correlation_id)
    except Exception as exc:
        return CaseResult(
            case_id=case.id,
            task_type=case.task_type,
            correlation_id=correlation_id,
            passed=False,
            xfail_ticket=xfail_ticket,
            error=str(exc),
            failures=[f"run error: {exc}"],
        )

    return _result_from_row(case, correlation_id, row)


class LaneUnreachableError(RuntimeError):
    """This runner cannot open a socket to the lane it is supposed to probe.

    Distinct from a delegation failure on purpose. OMN-18349: 60 consecutive
    nightly runs reported nine ``TimeoutError``s that read as delegation
    regressions and were in fact one unreachable Postgres, because the job had
    been moved onto a GitHub-hosted runner with no route to the lab network.
    An unreachable lane is a PLACEMENT fact and must never be reported in the
    vocabulary of a behavioural regression.
    """


# A socket to a host on the same network answers in milliseconds. Waiting the
# asyncpg default of 60 seconds to learn that a hosted runner has no route to
# the lab buys nothing and cost this workflow nine minutes a night of silence.
LANE_CONNECT_TIMEOUT_S = float(os.environ.get("ONEX_E2E_CONNECT_TIMEOUT_S", "15"))


def _runner_placement() -> str:
    """Describe where this process is running, for an unreachable-lane message."""
    environment = os.environ.get("RUNNER_ENVIRONMENT", "").strip()
    name = os.environ.get("RUNNER_NAME", "").strip()
    if not environment:
        return "not a GitHub Actions runner (local or container)"
    return f"RUNNER_ENVIRONMENT={environment!r} RUNNER_NAME={name!r}"


async def connect_lane_postgres() -> Any:
    """Open the lane projection connection, failing fast and legibly.

    Raises :class:`LaneUnreachableError` naming the address, the elapsed budget
    and the runner placement, so the first line of a red log says whether the
    lane was unreachable or the delegation misbehaved.
    """
    import asyncpg

    if not PG_PASSWORD:
        raise RuntimeError(
            "ONEX_E2E_POSTGRES_PASSWORD / POSTGRES_PASSWORD not set — cannot connect "
            f"to {_LANE} Postgres at {PG_HOST}:{PG_PORT}"
        )
    try:
        return await asyncpg.connect(
            host=PG_HOST,
            port=PG_PORT,
            user=PG_USER,
            password=PG_PASSWORD,
            database=PG_DB,
            timeout=LANE_CONNECT_TIMEOUT_S,
        )
    except (TimeoutError, OSError) as exc:
        raise LaneUnreachableError(
            f"no TCP connection to the {_LANE} lane Postgres at "
            f"{PG_HOST}:{PG_PORT} within {LANE_CONNECT_TIMEOUT_S}s "
            f"({_runner_placement()}): {exc!r}. This probe requires a runner "
            "inside the lab network. A GitHub-hosted runner has no route to it, "
            "and that placement -- not a delegation regression -- is what this "
            "failure means (OMN-18349)."
        ) from exc


async def wait_for_rows(
    conn: Any, correlation_ids: list[str], *, timeout: float | None = None
) -> dict[str, dict[str, Any]]:
    """Poll ``delegation_events`` for one WAVE of correlation ids under one deadline.

    One query per interval for every outstanding id in the wave. A wave is sized
    to what the lane serves at once (``lane_serving_concurrency()``), so the
    shared deadline is honest: the ids in it really are in flight together.

    OMN-18349: this docstring used to say the cases "are independent
    correlations and the platform is expected to serve them concurrently, so the
    corpus wall clock is one completion bound instead of nine". That premise was
    wrong on a lane whose command consumer is serial and whose entry tier is a
    single-slot endpoint, and it is what let nine cases share one deadline that
    covered four.

    Returns the rows that arrived, each settled (see ``settle_row``); ids absent
    from the mapping did not terminalise in time.
    """
    timeout = per_case_timeout_s() if timeout is None else timeout
    deadline = time.monotonic() + timeout
    found: dict[str, dict[str, Any]] = {}
    outstanding = list(correlation_ids)
    while outstanding:
        rows = await conn.fetch(
            "SELECT * FROM delegation_events WHERE correlation_id = ANY($1::text[])",
            outstanding,
        )
        for row in rows:
            mapping = dict(row)
            found[str(mapping["correlation_id"])] = mapping
        outstanding = [cid for cid in outstanding if cid not in found]
        if not outstanding or time.monotonic() >= deadline:
            break
        await asyncio.sleep(POLL_INTERVAL_S)
    for correlation_id, row in list(found.items()):
        found[correlation_id] = await settle_row(conn, correlation_id, row)
    return found


def _timed_out_result(
    case: ModelCorpusCase, correlation_id: str, timeout: float
) -> CaseResult:
    """The scoreboard row for a case whose projection never arrived."""
    message = (
        f"no delegation_events row for correlation_id={correlation_id!r} within "
        f"{timeout}s (lane={_LANE} pg={PG_HOST}:{PG_PORT}). The broker accepted "
        "the command, so either no runtime on this lane consumes the "
        "delegate-skill command topic, or the delegation produced no terminal "
        "at all -- not even the cancellation the declared handler execution "
        f"budget of {declared_handler_budget_s()}s should have committed."
    )
    return CaseResult(
        case_id=case.id,
        task_type=case.task_type,
        correlation_id=correlation_id,
        passed=False,
        xfail_ticket=case.xfail.ticket if case.xfail else None,
        error=message,
        failures=[f"run error: {message}"],
    )


def _result_from_row(
    case: ModelCorpusCase, correlation_id: str, row: dict[str, Any]
) -> CaseResult:
    failures = evaluate_row(case, row)
    return CaseResult(
        case_id=case.id,
        task_type=case.task_type,
        correlation_id=correlation_id,
        passed=not failures,
        xfail_ticket=case.xfail.ticket if case.xfail else None,
        terminal=row_terminal(row),
        model_name=row.get("model_name"),
        delegated_to=row.get("delegated_to"),
        tokens_input=int(row.get("tokens_input") or 0),
        tokens_output=int(row.get("tokens_output") or 0),
        cost_usd=float(row.get("cost_usd") or 0.0),
        quality_gate_passed=row.get("quality_gate_passed"),
        failures=failures,
    )


def corpus_waves(
    cases: list[ModelCorpusCase], concurrency: int
) -> list[list[ModelCorpusCase]]:
    """Split the corpus into groups the lane can actually serve at once."""
    return [cases[i : i + concurrency] for i in range(0, len(cases), concurrency)]


async def run_corpus(corpus: ModelCorpus | None = None) -> Scoreboard:
    """Run every integration case against the live lane and build a scoreboard.

    OMN-18349: the cases are published IN WAVES sized to the lane's declared
    serving concurrency, and each wave is drained before the next is published.
    The previous shape published all nine at once under one deadline, on a
    comment that read "the platform is expected to serve them concurrently". It
    is not, and it does not:

      * the delegate-skill command consumer's poll loop is serial -- it awaits
        each dispatch before requesting the next record (declared in
        ``node_delegate_skill_orchestrator/models/model_handler_execution_budget``);
      * the tier every case enters on is one physical endpoint permitting one
        running generation (declared in ``bifrost_delegation.yaml``);
      * and a handler that hits its execution budget stops WAITING for its
        delegation, it does not stop the delegation. The abandoned workflow goes
        on competing for that single slot.

    Those three together are not a slow probe, they are a collapse: on the
    2026-09-14 run the third case onward were each cancelled at an exact
    240.000s cadence -- 06:00:37.87, 06:04:37.88, 06:08:37.88, 06:12:37.89,
    06:16:37.89 -- every one of them having spent its entire budget behind work
    the probe had already given up on. Feeding the lane at the rate it serves is
    the fix; raising the deadline would only have bought more of the same.
    """
    corpus = corpus or load_corpus()
    topic = _command_topic()
    started = datetime.now(UTC).isoformat()

    timeout = per_case_timeout_s()
    concurrency = lane_serving_concurrency()

    conn = await connect_lane_postgres()
    results: list[CaseResult] = []
    try:
        for wave in corpus_waves(list(corpus.integration_cases()), concurrency):
            published: list[tuple[ModelCorpusCase, str]] = []
            for case in wave:
                correlation_id = str(uuid.uuid4())
                try:
                    await publish_case(topic, case, correlation_id)
                except Exception as exc:
                    results.append(
                        CaseResult(
                            case_id=case.id,
                            task_type=case.task_type,
                            correlation_id=correlation_id,
                            passed=False,
                            xfail_ticket=case.xfail.ticket if case.xfail else None,
                            error=str(exc),
                            failures=[f"publish error: {exc}"],
                        )
                    )
                    continue
                published.append((case, correlation_id))

            rows = await wait_for_rows(
                conn, [cid for _, cid in published], timeout=timeout
            )
            for case, correlation_id in published:
                row = rows.get(correlation_id)
                if row is None:
                    results.append(_timed_out_result(case, correlation_id, timeout))
                    continue
                results.append(_result_from_row(case, correlation_id, row))
    finally:
        await conn.close()

    return Scoreboard(
        lane=_LANE,
        corpus_version=corpus.corpus_version,
        started_at=started,
        finished_at=datetime.now(UTC).isoformat(),
        results=results,
    )


def fatal_scoreboard(exc: BaseException) -> dict[str, Any]:
    """A scoreboard describing a run that died before any case completed.

    Same envelope as a real scoreboard so the artifact is always the same
    shape, with the fatal cause and its class recorded where a reviewer looks.
    """
    now = datetime.now(UTC).isoformat()
    return {
        "lane": _LANE,
        "corpus_version": "unknown",
        "started_at": now,
        "finished_at": now,
        "summary": {"total": 0, "passed": 0, "hard_failures": 0, "xpass": 0},
        "fatal": {"error_class": type(exc).__name__, "error": str(exc)},
        "results": [],
    }


def _main() -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Delegation regression Layer-2 runner")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("delegation_regression_scoreboard.json"),
        help="Scoreboard JSON artifact output path.",
    )
    args = parser.parse_args()

    # OMN-18349: the scoreboard is written on EVERY outcome, fatal ones
    # included. It used to be written only after a completed corpus run, so
    # every red night uploaded no artifact at all and the one question a
    # reviewer had -- what did the runner actually see -- had no answer
    # anywhere. A failure that records nothing is indistinguishable from a
    # failure that never ran.
    try:
        scoreboard = asyncio.run(run_corpus())
    except Exception as exc:
        args.out.write_text(json.dumps(fatal_scoreboard(exc), indent=2))
        log.error("FATAL before any case completed: %s", exc)
        log.error("fatal scoreboard written to %s", args.out)
        return 1

    args.out.write_text(json.dumps(scoreboard.to_dict(), indent=2))
    log.info("scoreboard written to %s", args.out)

    for result in scoreboard.results:
        flag = "PASS" if result.passed else "FAIL"
        xf = f" (xfail {result.xfail_ticket})" if result.xfail_ticket else ""
        log.info(
            "%-4s %s%s model=%s cost=%s tokens=%s/%s",
            flag,
            result.case_id,
            xf,
            result.model_name,
            result.cost_usd,
            result.tokens_input,
            result.tokens_output,
        )

    # Hard failures (non-xfail) break the nightly.
    if scoreboard.hard_failures:
        log.error(
            "HARD FAILURES: %s",
            ", ".join(r.case_id for r in scoreboard.hard_failures),
        )
        return 1
    if scoreboard.xpass:
        log.warning(
            "XPASS (xfail markers now stale — fix landed): %s",
            ", ".join(f"{r.case_id}/{r.xfail_ticket}" for r in scoreboard.xpass),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
