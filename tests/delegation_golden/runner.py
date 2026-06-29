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

Lane / connection config (mirrors tests/integration/e2e_probe/test_delegation_e2e_probe.py):
    ONEX_E2E_LANE                default stability-test
    ONEX_E2E_KAFKA_BOOTSTRAP     default 192.168.86.201:39092 (stability-test Redpanda)
    ONEX_E2E_POSTGRES_HOST       default 192.168.86.201
    ONEX_E2E_POSTGRES_PORT       default 15436 (stability-test)
    ONEX_E2E_POSTGRES_DB         default omnidash_analytics
    ONEX_E2E_POSTGRES_USER       default postgres
    ONEX_E2E_POSTGRES_PASSWORD   required (or POSTGRES_PASSWORD)

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

_DEFAULT_KAFKA = "192.168.86.201:39092"  # onex-allow-internal-ip OMN-13540 reason="stability-test lab Redpanda default; overridden by ONEX_E2E_KAFKA_BOOTSTRAP at runtime"
_DEFAULT_PG_HOST = "192.168.86.201"  # onex-allow-internal-ip OMN-13540 reason="stability-test lab Postgres host; overridden by ONEX_E2E_POSTGRES_HOST at runtime"
_DEFAULT_PG_PORT_STABILITY = 15436
_DEFAULT_PG_PORT_DEV = 5436

KAFKA_BOOTSTRAP = _env_or("ONEX_E2E_KAFKA_BOOTSTRAP", _DEFAULT_KAFKA)
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
POLL_TIMEOUT_S = float(os.environ.get("ONEX_E2E_POLL_TIMEOUT_S", "330"))


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


async def publish_case(topic: str, case: ModelCorpusCase, correlation_id: str) -> None:
    """Thin-publish one delegate-skill command to the live bus."""
    from aiokafka import AIOKafkaProducer

    envelope = ModelEventEnvelope[dict[str, Any]](
        payload=_command_payload(case, correlation_id),
        correlation_id=uuid.UUID(correlation_id),
        source_tool="omnimarket.delegation-regression.omn-13540",
        event_type="omnimarket.delegate-skill",
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )
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


async def wait_for_row(
    conn: Any, correlation_id: str, *, timeout: float = POLL_TIMEOUT_S
) -> dict[str, Any]:
    """Poll delegation_events for the terminal row of this correlation_id."""
    deadline = time.monotonic() + timeout
    while True:
        row = await conn.fetchrow(
            "SELECT * FROM delegation_events WHERE correlation_id = $1",
            correlation_id,
        )
        if row is not None:
            return dict(row)
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"no delegation_events row for correlation_id={correlation_id!r} "
                f"within {timeout}s (lane={_LANE} kafka={KAFKA_BOOTSTRAP})"
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

    terminal = str(row.get("terminal_state") or row.get("status") or "")
    # delegation_events does not carry an explicit terminal column on every lane;
    # quality_gate_passed + presence of the row implies a completed projection.
    if not terminal:
        terminal = "completed"

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

    failures = evaluate_row(case, row)
    return CaseResult(
        case_id=case.id,
        task_type=case.task_type,
        correlation_id=correlation_id,
        passed=not failures,
        xfail_ticket=xfail_ticket,
        terminal=str(row.get("terminal_state") or row.get("status") or "completed"),
        model_name=row.get("model_name"),
        delegated_to=row.get("delegated_to"),
        tokens_input=int(row.get("tokens_input") or 0),
        tokens_output=int(row.get("tokens_output") or 0),
        cost_usd=float(row.get("cost_usd") or 0.0),
        quality_gate_passed=row.get("quality_gate_passed"),
        failures=failures,
    )


async def run_corpus(corpus: ModelCorpus | None = None) -> Scoreboard:
    """Run every integration case against the live lane and build a scoreboard."""
    import asyncpg

    corpus = corpus or load_corpus()
    topic = _command_topic()
    started = datetime.now(UTC).isoformat()

    if not PG_PASSWORD:
        raise RuntimeError(
            "ONEX_E2E_POSTGRES_PASSWORD / POSTGRES_PASSWORD not set — cannot connect "
            f"to {_LANE} Postgres at {PG_HOST}:{PG_PORT}"
        )

    conn = await asyncpg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, database=PG_DB
    )
    results: list[CaseResult] = []
    try:
        for case in corpus.integration_cases():
            results.append(await run_case(conn, topic, case))
    finally:
        await conn.close()

    return Scoreboard(
        lane=_LANE,
        corpus_version=corpus.corpus_version,
        started_at=started,
        finished_at=datetime.now(UTC).isoformat(),
        results=results,
    )


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

    scoreboard = asyncio.run(run_corpus())
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
