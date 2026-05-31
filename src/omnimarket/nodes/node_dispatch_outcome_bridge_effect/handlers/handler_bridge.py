# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Bridge handler: dispatch_worker-completed -> intelligence eval -> dispatch_eval_results.

omniintelligence is a runtime peer dependency installed in the container alongside omnimarket.
It cannot be declared as a formal pip dep (circular: omniintelligence requires omnimarket>=0.4.0).
All imports from omniintelligence are deferred inside functions so this module is importable
without omniintelligence present (e.g. during uv sync or CI that only checks omnimarket).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path
from typing import Any

from omnibase_core.enums.cost import EnumUsageSource
from omnibase_core.enums.enum_dispatch_verdict import EnumDispatchVerdict
from omnibase_core.models.cost import ModelCostProvenance
from omnibase_core.models.dispatch import ModelDispatchEvalResult

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.projection.envelope import unwrap_envelope

_log = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
SUBSCRIBE_TOPIC = contract_subscribe_topics(_CONTRACT_PATH)[0]
PUBLISH_TOPIC = contract_publish_topics(_CONTRACT_PATH)[0]
CONSUMER_GROUP = "local.omnimarket.node_dispatch_outcome_bridge_effect.consume.v1"

SQL_UPSERT_DISPATCH_EVAL_RESULT = """
INSERT INTO dispatch_eval_results (
    task_id,
    dispatch_id,
    ticket_id,
    verdict,
    quality_score,
    token_cost,
    dollars_cost,
    model_calls,
    evaluated_at,
    eval_latency_ms,
    usage_source,
    estimation_method,
    source_payload_hash
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12, $13)
ON CONFLICT (task_id, dispatch_id) DO UPDATE SET
    ticket_id = EXCLUDED.ticket_id,
    verdict = EXCLUDED.verdict,
    quality_score = EXCLUDED.quality_score,
    token_cost = EXCLUDED.token_cost,
    dollars_cost = EXCLUDED.dollars_cost,
    model_calls = EXCLUDED.model_calls,
    evaluated_at = EXCLUDED.evaluated_at,
    eval_latency_ms = EXCLUDED.eval_latency_ms,
    usage_source = EXCLUDED.usage_source,
    estimation_method = EXCLUDED.estimation_method,
    source_payload_hash = EXCLUDED.source_payload_hash
"""

_VERDICT_MAP: dict[str, EnumDispatchVerdict] = {
    "PASS": EnumDispatchVerdict.PASS,
    "FAIL": EnumDispatchVerdict.FAIL,
    "ERROR": EnumDispatchVerdict.ERROR,
}

_USAGE_SOURCE_MAP: dict[str, EnumUsageSource] = {
    "measured": EnumUsageSource.MEASURED,
    "estimated": EnumUsageSource.ESTIMATED,
    "unknown": EnumUsageSource.UNKNOWN,
}


def _build_model_input(data: dict[str, Any]) -> Any:
    """Build omniintelligence ModelInput from Kafka event payload (lazy import)."""
    from omniintelligence.nodes.node_dispatch_outcome_eval_effect.models.model_input import (  # type: ignore[import-not-found]
        EnumUsageSource as IntelEnumUsageSource,
    )
    from omniintelligence.nodes.node_dispatch_outcome_eval_effect.models.model_input import (
        ModelCallRecord as IntelModelCallRecord,
    )
    from omniintelligence.nodes.node_dispatch_outcome_eval_effect.models.model_input import (
        ModelCostProvenance as IntelModelCostProvenance,
    )
    from omniintelligence.nodes.node_dispatch_outcome_eval_effect.models.model_input import (
        ModelInput,
    )

    cost_provenance_raw = data.get("cost_provenance") or {}
    if not isinstance(cost_provenance_raw, dict):
        cost_provenance_raw = {}

    usage_source_raw = str(cost_provenance_raw.get("usage_source") or "unknown").lower()
    intel_values = {e.value for e in IntelEnumUsageSource}
    intel_usage_source = (
        IntelEnumUsageSource(usage_source_raw)
        if usage_source_raw in intel_values
        else IntelEnumUsageSource.UNKNOWN
    )

    estimation_method: str | None = cost_provenance_raw.get("estimation_method")
    source_payload_hash: str | None = cost_provenance_raw.get("source_payload_hash")

    if (
        intel_usage_source == IntelEnumUsageSource.MEASURED
        and source_payload_hash is None
    ):
        intel_usage_source = IntelEnumUsageSource.UNKNOWN
        estimation_method = None
        source_payload_hash = None
    elif (
        intel_usage_source == IntelEnumUsageSource.ESTIMATED
        and estimation_method is None
    ):
        intel_usage_source = IntelEnumUsageSource.UNKNOWN

    intel_provenance = IntelModelCostProvenance(
        usage_source=intel_usage_source,
        estimation_method=estimation_method
        if intel_usage_source == IntelEnumUsageSource.ESTIMATED
        else None,
        source_payload_hash=source_payload_hash
        if intel_usage_source == IntelEnumUsageSource.MEASURED
        else None,
    )

    raw_calls = data.get("model_calls") or []
    model_calls: list[IntelModelCallRecord] = []
    for call in raw_calls:
        if not isinstance(call, dict):
            continue
        call_prov_raw = call.get("cost_provenance") or {}
        call_usage_raw = str(
            call_prov_raw.get("usage_source")
            if isinstance(call_prov_raw, dict)
            else "unknown"
        ).lower()
        call_usage = (
            IntelEnumUsageSource(call_usage_raw)
            if call_usage_raw in intel_values
            else IntelEnumUsageSource.UNKNOWN
        )
        model_calls.append(
            IntelModelCallRecord(
                provider=str(call.get("provider") or "unknown"),
                model=str(call.get("model") or "unknown"),
                input_tokens=int(call.get("input_tokens") or 0),
                output_tokens=int(call.get("output_tokens") or 0),
                latency_ms=int(call.get("latency_ms") or 0),
                cost_dollars=float(call.get("cost_dollars") or 0.0),
                cost_provenance=IntelModelCostProvenance(usage_source=call_usage),
            )
        )

    task_id_raw = data.get("task_id")
    dispatch_id_raw = data.get("dispatch_id")
    task_id = str(task_id_raw).strip() if task_id_raw is not None else ""
    dispatch_id = str(dispatch_id_raw).strip() if dispatch_id_raw is not None else ""
    if not task_id or not dispatch_id:
        raise ValueError(
            f"task_id and dispatch_id are required; got task_id={task_id_raw!r} dispatch_id={dispatch_id_raw!r}"
        )

    return ModelInput(
        task_id=task_id,
        dispatch_id=dispatch_id,
        ticket_id=data.get("ticket_id") or None,
        status=str(data.get("status") or "error"),
        artifact_path=data.get("artifact_path") or None,
        model_calls=model_calls,
        token_cost=int(data.get("token_cost") or 0),
        dollars_cost=float(data.get("dollars_cost") or 0.0),
        cost_provenance=intel_provenance,
    )


def _to_core_provenance(
    usage_source: str | None, estimation_method: str | None
) -> ModelCostProvenance:
    raw = (usage_source or "unknown").lower()
    resolved = _USAGE_SOURCE_MAP.get(raw, EnumUsageSource.UNKNOWN)
    if resolved == EnumUsageSource.ESTIMATED and estimation_method:
        return ModelCostProvenance(
            usage_source=resolved, estimation_method=estimation_method
        )
    return ModelCostProvenance(usage_source=EnumUsageSource.UNKNOWN)


async def handle_dispatch_outcome(model_input: Any) -> Any:
    """Delegate dispatch-outcome evaluation to the runtime peer package."""
    from omniintelligence.nodes.node_dispatch_outcome_eval_effect.handlers.handler_dispatch_outcome import (  # type: ignore[import-not-found]
        handle_dispatch_outcome as _handle_dispatch_outcome,
    )

    return await _handle_dispatch_outcome(model_input)


async def process_event(data: dict[str, Any], db: Any, producer: Any | None) -> bool:
    """Evaluate one dispatch-worker-completed event and persist the result."""
    try:
        model_input = _build_model_input(data)
    except Exception as exc:
        _log.error(
            "Failed to parse dispatch_worker-completed payload: %s — keys=%s",
            exc,
            list(data.keys()),
        )
        return False

    try:
        model_output = await handle_dispatch_outcome(model_input)
    except Exception as exc:
        _log.error(
            "handle_dispatch_outcome failed task_id=%s dispatch_id=%s: %s",
            data.get("task_id"),
            data.get("dispatch_id"),
            exc,
            exc_info=True,
        )
        return False

    verdict = _VERDICT_MAP.get(model_output.verdict, EnumDispatchVerdict.ERROR)
    core_provenance = _to_core_provenance(
        model_output.usage_source, model_output.estimation_method
    )

    eval_result = ModelDispatchEvalResult(
        task_id=model_input.task_id,
        dispatch_id=model_input.dispatch_id,
        ticket_id=model_input.ticket_id,
        verdict=verdict,
        quality_score=model_output.quality_score,
        token_cost=model_output.token_cost,
        dollars_cost=model_output.dollars_cost,
        cost_provenance=core_provenance,
        model_calls=[],
        evaluated_at=model_output.evaluated_at,
        eval_latency_ms=model_output.eval_latency_ms,
    )

    try:
        await db.execute(
            SQL_UPSERT_DISPATCH_EVAL_RESULT,
            eval_result.task_id,
            eval_result.dispatch_id,
            eval_result.ticket_id,
            eval_result.verdict.name,
            eval_result.quality_score,
            eval_result.token_cost,
            eval_result.dollars_cost,
            json.dumps([], separators=(",", ":")),
            eval_result.evaluated_at,
            eval_result.eval_latency_ms,
            eval_result.cost_provenance.usage_source.name,
            eval_result.cost_provenance.estimation_method,
            model_output.source_payload_hash,
        )
    except Exception as exc:
        _log.error(
            "DB write failed task_id=%s dispatch_id=%s: %s",
            eval_result.task_id,
            eval_result.dispatch_id,
            exc,
            exc_info=True,
        )
        return False

    _log.info(
        "Dispatch outcome evaluated task_id=%s dispatch_id=%s verdict=%s",
        eval_result.task_id,
        eval_result.dispatch_id,
        eval_result.verdict.name,
    )

    if producer is not None:
        try:
            publish_payload = {
                "task_id": eval_result.task_id,
                "dispatch_id": eval_result.dispatch_id,
                "ticket_id": eval_result.ticket_id,
                "verdict": eval_result.verdict.name,
                "quality_score": eval_result.quality_score,
                "token_cost": eval_result.token_cost,
                "dollars_cost": eval_result.dollars_cost,
                "source_payload_hash": model_output.source_payload_hash,
                "evaluated_at": eval_result.evaluated_at.isoformat(),
                "eval_latency_ms": eval_result.eval_latency_ms,
            }
            await producer.send_and_wait(
                PUBLISH_TOPIC,
                value=json.dumps(publish_payload).encode(),
                key=eval_result.task_id.encode(),
            )
        except Exception as exc:
            _log.warning(
                "Kafka publish failed (non-fatal) task_id=%s: %s",
                eval_result.task_id,
                exc,
            )

    return True


class HandlerDispatchOutcomeBridge:
    """RuntimeLocal handler protocol shim."""

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        ok = asyncio.run(process_event(input_data, _NullDb(), None))
        return {"evaluated": ok}


class _NullDb:
    async def execute(self, *args: Any, **kwargs: Any) -> str:
        return "INSERT 0 1"


async def _run_consumer(broker: str, group_id: str, db_dsn: str) -> None:
    try:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
    except ImportError:
        _log.error("aiokafka is not installed; run: uv add aiokafka")
        raise

    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

    db = AsyncpgAdapter(dsn=db_dsn)
    await db.connect()
    _log.info("DB connected")

    producer = AIOKafkaProducer(bootstrap_servers=broker)
    await producer.start()

    consumer = AIOKafkaConsumer(
        SUBSCRIBE_TOPIC,
        bootstrap_servers=broker,
        group_id=group_id,
        auto_offset_reset="earliest",
        enable_auto_commit=True,
    )
    await consumer.start()
    _log.info(
        "dispatch-outcome-bridge consumer started — broker=%s group=%s topic=%s",
        broker,
        group_id,
        SUBSCRIBE_TOPIC,
    )

    stop_event = asyncio.Event()

    def _signal_handler(sig: int, _: Any) -> None:
        _log.info("received signal %s, shutting down", sig)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _signal_handler)

    evaluated = 0
    skipped = 0
    errors = 0

    try:
        async for msg in consumer:
            if stop_event.is_set():
                break
            raw_value = msg.value
            if raw_value is None:
                continue
            try:
                data = unwrap_envelope(raw_value)
            except Exception as exc:
                _log.error("failed to parse message offset=%d: %s", msg.offset, exc)
                errors += 1
                continue
            if data is None:
                skipped += 1
                continue
            ok = await process_event(data, db, producer)
            if ok:
                evaluated += 1
            else:
                errors += 1
    finally:
        await consumer.stop()
        await producer.stop()
        await db.close()
        _log.info(
            "consumer stopped — evaluated=%d skipped=%d errors=%d",
            evaluated,
            skipped,
            errors,
        )


def _build_dsn() -> str:
    dsn = os.environ.get("OMNIINTELLIGENCE_DB_URL")
    if dsn:
        return dsn
    host = os.environ.get("POSTGRES_HOST", "")
    port = os.environ.get("POSTGRES_PORT", "5436")
    user = os.environ.get("POSTGRES_USER", "postgres")
    password = os.environ["POSTGRES_PASSWORD"]
    return f"postgresql://{user}:{password}@{host}:{port}/omniintelligence"


def main() -> None:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Kafka consumer bridging dispatch-worker-completed into intelligence eval"
    )
    parser.add_argument(
        "--bootstrap-servers",
        default=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", ""),
    )
    parser.add_argument(
        "--group-id",
        default=os.environ.get("KAFKA_CONSUMER_GROUP", CONSUMER_GROUP),
    )
    args = parser.parse_args()
    asyncio.run(_run_consumer(args.bootstrap_servers, args.group_id, _build_dsn()))


if __name__ == "__main__":
    main()
