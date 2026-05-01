# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""E2E orchestrator consumer — bridges build-loop completion to merge sweep.

Subscribes to ``onex.evt.omnimarket.build-loop-orchestrator-completed.v1``.
On receipt:
  1. Polls CI via ``gh pr checks`` for all PR refs (timeout: 25 min).
  2. When all CI checks are green, publishes
     ``onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1``.

Also subscribes to ``onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1``
and writes an evidence bundle to
``.onex_state/e2e-runs/{correlation_id}/`` on merge completion.

Environment:
    KAFKA_BOOTSTRAP_SERVERS  Redpanda/Kafka bootstrap (required)
    KAFKA_BROKER             Alias for KAFKA_BOOTSTRAP_SERVERS (fallback)
    E2E_ORCH_GROUP           Consumer group ID
                             (default: local.omnimarket.e2e-orchestrator.consume.v1)
    ONEX_STATE_DIR           State directory (default: ~/.onex_state)
    CI_POLL_TIMEOUT_SECONDS  CI polling timeout in seconds (default: 1500 = 25 min)
    CI_POLL_INTERVAL_SECONDS Polling interval in seconds (default: 30)

Usage:
    python -m omnimarket.nodes.node_e2e_orchestrator.consumer
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
    TOPIC_BUILD_LOOP_COMPLETED as TOPIC_BUILD_COMPLETED,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    TOPIC_PR_LIFECYCLE_COMPLETED,
    TOPIC_PR_LIFECYCLE_START,
)

logger = logging.getLogger(__name__)

_DEFAULT_GROUP = "local.omnimarket.e2e-orchestrator.consume.v1"
_DEFAULT_CI_TIMEOUT = 1500  # 25 minutes
_DEFAULT_CI_INTERVAL = 30  # seconds


def _state_dir() -> Path:
    configured = os.environ.get("ONEX_STATE_DIR", "")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".onex_state"


def _write_evidence(correlation_id: str, filename: str, data: dict[str, Any]) -> None:
    run_dir = _state_dir() / "e2e-runs" / correlation_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / filename).write_text(json.dumps(data, indent=2, default=str))
    logger.info("evidence written: %s/%s", correlation_id, filename)


def _check_pr_ci(pr_ref: str) -> tuple[bool, bool, dict[str, Any]]:
    """Return (all_green, any_failed, checks_summary) for a PR ref.

    pr_ref is expected to be ``OmniNode-ai/repo#N`` format.
    """
    parts = pr_ref.split("#")
    if len(parts) != 2:
        logger.warning("unparseable pr_ref %r, skipping", pr_ref)
        return True, False, {"pr_ref": pr_ref, "status": "unparseable"}

    repo, pr_num = parts[0].strip(), parts[1].strip()
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "checks",
                pr_num,
                "--repo",
                repo,
                "--json",
                "name,state,conclusion",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                "gh pr checks failed for %s: %s", pr_ref, result.stderr[:200]
            )
            return False, False, {"pr_ref": pr_ref, "error": result.stderr[:200]}

        checks = json.loads(result.stdout or "[]")
        failed = any(
            c.get("conclusion", "").upper()
            in {"FAILURE", "ACTION_REQUIRED", "CANCELLED", "TIMED_OUT"}
            for c in checks
        )
        pending = any(
            c.get("state", "").upper()
            in {"PENDING", "IN_PROGRESS", "QUEUED", "WAITING"}
            for c in checks
        )
        all_green = not failed and not pending
        return all_green, failed, {"pr_ref": pr_ref, "checks": checks}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.warning("CI check error for %s: %s", pr_ref, exc)
        return False, False, {"pr_ref": pr_ref, "error": str(exc)}


async def _poll_ci_until_green(
    pr_refs: list[str],
    correlation_id: str,
    *,
    timeout_seconds: int,
    poll_interval: int,
) -> tuple[bool, list[dict[str, Any]]]:
    """Poll CI for all pr_refs until all green or timeout. Returns (success, checks_summary)."""
    if not pr_refs:
        logger.info("[E2E] no PR refs — skipping CI poll, proceeding to merge sweep")
        return True, []

    deadline = asyncio.get_event_loop().time() + timeout_seconds
    attempt = 0

    while asyncio.get_event_loop().time() < deadline:
        attempt += 1
        all_checks: list[dict[str, Any]] = []
        all_green = True
        any_failed = False

        for pr_ref in pr_refs:
            green, failed, summary = _check_pr_ci(pr_ref)
            all_checks.append(summary)
            if failed:
                any_failed = True
            if not green:
                all_green = False

        logger.info(
            "[E2E] CI poll attempt=%d correlation_id=%s all_green=%s any_failed=%s",
            attempt,
            correlation_id,
            all_green,
            any_failed,
        )

        if all_green:
            return True, all_checks

        if any_failed:
            logger.warning(
                "[E2E] CI failures detected for correlation_id=%s — aborting CI poll",
                correlation_id,
            )
            return False, all_checks

        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_interval, remaining))

    logger.warning(
        "[E2E] CI poll timed out after %ds for correlation_id=%s",
        timeout_seconds,
        correlation_id,
    )
    return False, []


async def _handle_build_completed(
    payload: dict[str, Any],
    producer: Any,
    *,
    ci_timeout: int,
    ci_interval: int,
) -> None:
    correlation_id = str(payload.get("correlation_id", uuid4()))
    pr_refs: list[str] = list(payload.get("pr_refs", []))
    cost_event_keys: list[str] = list(payload.get("cost_event_keys", []))

    logger.info(
        "[E2E] build-completed received correlation_id=%s pr_refs=%d",
        correlation_id,
        len(pr_refs),
    )

    # Write build_completed evidence
    _write_evidence(correlation_id, "build_completed.json", payload)
    _write_evidence(correlation_id, "pr_refs.json", {"pr_refs": pr_refs})
    _write_evidence(
        correlation_id, "cost_event_refs.json", {"cost_event_keys": cost_event_keys}
    )

    # Write run manifest
    manifest: dict[str, Any] = {
        "correlation_id": correlation_id,
        "started_at": datetime.now(tz=UTC).isoformat(),
        "pr_refs": pr_refs,
        "phases": ["build_completed", "ci_poll", "merge_sweep"],
    }
    _write_evidence(correlation_id, "run_manifest.json", manifest)

    # Poll CI
    ci_green, ci_checks = await _poll_ci_until_green(
        pr_refs,
        correlation_id,
        timeout_seconds=ci_timeout,
        poll_interval=ci_interval,
    )
    _write_evidence(
        correlation_id, "ci_checks.json", {"checks": ci_checks, "all_green": ci_green}
    )

    if not ci_green:
        logger.warning(
            "[E2E] CI not green — skipping merge sweep for correlation_id=%s",
            correlation_id,
        )
        return

    # Trigger merge sweep
    run_id = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S") + f"-{correlation_id[:6]}"
    sweep_cmd: dict[str, Any] = {
        "correlation_id": correlation_id,
        "run_id": run_id,
        "dry_run": False,
        "enable_auto_rebase": True,
        "use_dag_ordering": True,
    }
    await producer.send_and_wait(TOPIC_PR_LIFECYCLE_START, sweep_cmd)
    logger.info(
        "[E2E] pr-lifecycle-start published correlation_id=%s run_id=%s",
        correlation_id,
        run_id,
    )


async def _handle_merge_completed(payload: dict[str, Any]) -> None:
    correlation_id = str(payload.get("correlation_id", "unknown"))
    logger.info(
        "[E2E] merge-completed received correlation_id=%s prs_merged=%s",
        correlation_id,
        payload.get("prs_merged"),
    )

    _write_evidence(correlation_id, "merge_completed.json", payload)

    # Build receipts summary
    receipts: dict[str, Any] = {
        "correlation_id": correlation_id,
        "build_completed": True,
        "ci_green": True,
        "merge_completed": True,
        "prs_merged": payload.get("prs_merged", 0),
        "prs_fixed": payload.get("prs_fixed", 0),
        "final_state": payload.get("final_state", "COMPLETE"),
        "verified_at": datetime.now(tz=UTC).isoformat(),
    }
    _write_evidence(correlation_id, "receipts.json", receipts)
    logger.info("[E2E] evidence bundle complete for correlation_id=%s", correlation_id)


async def _run_consumer(
    broker: str,
    group_id: str,
    *,
    ci_timeout: int,
    ci_interval: int,
) -> None:
    try:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
    except ImportError:
        logger.error("aiokafka is not installed. Install with: uv add aiokafka")
        sys.exit(1)

    consumer = AIOKafkaConsumer(
        TOPIC_BUILD_COMPLETED,
        TOPIC_PR_LIFECYCLE_COMPLETED,
        bootstrap_servers=broker,
        group_id=group_id,
        value_deserializer=lambda b: json.loads(b.decode("utf-8")),
        auto_offset_reset="latest",
        enable_auto_commit=True,
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=broker,
        value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
    )

    await consumer.start()
    await producer.start()
    logger.info(
        "e2e-orchestrator consumer started — broker=%s group=%s topics=[%s, %s]",
        broker,
        group_id,
        TOPIC_BUILD_COMPLETED,
        TOPIC_PR_LIFECYCLE_COMPLETED,
    )

    stop_event = asyncio.Event()

    def _handle_signal(sig: int, _: Any) -> None:
        logger.info("received signal %s, shutting down", sig)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)

    try:
        async for msg in consumer:
            if stop_event.is_set():
                break

            raw: dict[str, Any] = msg.value if isinstance(msg.value, dict) else {}
            topic = msg.topic

            try:
                if topic == TOPIC_BUILD_COMPLETED:
                    await _handle_build_completed(
                        raw,
                        producer,
                        ci_timeout=ci_timeout,
                        ci_interval=ci_interval,
                    )
                elif topic == TOPIC_PR_LIFECYCLE_COMPLETED:
                    await _handle_merge_completed(raw)
                else:
                    logger.warning("[E2E] unexpected topic %s", topic)
            except Exception as exc:
                logger.error(
                    "[E2E] error handling message on topic=%s: %s",
                    topic,
                    exc,
                    exc_info=True,
                )
    finally:
        await consumer.stop()
        await producer.stop()
        logger.info("e2e-orchestrator consumer stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    broker = os.environ.get(
        "KAFKA_BOOTSTRAP_SERVERS",
        os.environ.get("KAFKA_BROKER", "localhost:9092"),
    )
    group_id = os.environ.get("E2E_ORCH_GROUP", _DEFAULT_GROUP)
    ci_timeout = int(os.environ.get("CI_POLL_TIMEOUT_SECONDS", _DEFAULT_CI_TIMEOUT))
    ci_interval = int(os.environ.get("CI_POLL_INTERVAL_SECONDS", _DEFAULT_CI_INTERVAL))
    asyncio.run(
        _run_consumer(broker, group_id, ci_timeout=ci_timeout, ci_interval=ci_interval)
    )


if __name__ == "__main__":
    main()
