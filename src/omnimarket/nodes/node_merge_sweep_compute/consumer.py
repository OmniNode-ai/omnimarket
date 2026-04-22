# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Kafka consumer entrypoint for node_merge_sweep.

Subscribes to ``onex.cmd.omnimarket.merge-sweep-start.v1`` via aiokafka and
invokes NodeMergeSweep.  Emits ``onex.evt.omnimarket.merge-sweep-completed.v1``
on completion.

Environment variables (all resolved at startup — no hardcoded strings):
    GH_PAT             GitHub PAT (required — fail-fast if missing)
    KAFKA_BROKER        Redpanda/Kafka bootstrap server (default: localhost:9092)
    ONEX_STATE_DIR      Failure-history state dir (default: ~/.onex_state)
    MERGE_SWEEP_GROUP   Consumer group ID (default: omnimarket.merge_sweep.consume.v1)

Usage (standalone consumer loop):
    python -m omnimarket.nodes.node_merge_sweep_compute.consumer

The loop runs until SIGINT/SIGTERM.  It is safe to run alongside the existing
``python -m omnimarket.nodes.node_merge_sweep`` CLI — they are independent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import signal
import sys
from typing import Any

from omnimarket.nodes.node_merge_sweep_compute.adapter_github_http import (
    GitHubHttpClient,
)
from omnimarket.nodes.node_merge_sweep_compute.branch_protection import (
    BranchProtectionCache,
)
from omnimarket.nodes.node_merge_sweep_compute.handlers.handler_merge_sweep import (
    TOPIC_MERGE_SWEEP_COMPLETED,
    TOPIC_MERGE_SWEEP_START,
    ModelFailureHistoryEntry,
    ModelMergeSweepRequest,
    ModelPRInfo,
    NodeMergeSweep,
)
from omnimarket.nodes.node_merge_sweep_compute.protocols import GitHubTransportError

_log = logging.getLogger(__name__)

_DEFAULT_REPOS = [
    "OmniNode-ai/omniclaude",
    "OmniNode-ai/omnibase_core",
    "OmniNode-ai/omnibase_infra",
    "OmniNode-ai/omnibase_spi",
    "OmniNode-ai/omniintelligence",
    "OmniNode-ai/omnimemory",
    "OmniNode-ai/omninode_infra",
    "OmniNode-ai/omnidash",
    "OmniNode-ai/onex_change_control",
    "OmniNode-ai/omnimarket",
    "OmniNode-ai/omnibase_compat",
    "OmniNode-ai/omniweb",
]


def _is_green(pr: dict[str, Any]) -> bool:
    rollup = pr.get("statusCheckRollup") or []
    required = [c for c in rollup if c.get("isRequired")]
    if not required:
        return True
    return all(c.get("conclusion") == "SUCCESS" for c in required)


def _to_pr_info(
    pr: dict[str, Any], repo: str, required_approving: int | None
) -> ModelPRInfo:
    review_decision_raw = pr.get("reviewDecision")
    review_decision = review_decision_raw if review_decision_raw else None
    return ModelPRInfo(
        number=pr["number"],
        title=pr.get("title", ""),
        repo=repo,
        mergeable=pr.get("mergeable", "UNKNOWN"),
        merge_state_status=pr.get("mergeStateStatus", "UNKNOWN"),
        is_draft=pr.get("isDraft", False),
        review_decision=review_decision,
        required_checks_pass=_is_green(pr),
        labels=[lbl["name"] for lbl in (pr.get("labels") or [])],
        required_approving_review_count=required_approving,
    )


def _load_failure_history(state_dir: str) -> dict[str, ModelFailureHistoryEntry]:
    history_path = pathlib.Path(state_dir) / "merge-sweep" / "failure-history.json"
    if not history_path.exists():
        return {}
    try:
        raw = json.loads(history_path.read_text())
        return {
            k: ModelFailureHistoryEntry(**v)
            for k, v in raw.items()
            if isinstance(v, dict)
        }
    except Exception as exc:
        _log.warning("failed to load failure history: %s", exc)
        return {}


def _build_request(
    github: GitHubHttpClient,
    cmd: dict[str, Any],
    state_dir: str,
) -> ModelMergeSweepRequest:
    """Build a ModelMergeSweepRequest from a Kafka command payload."""
    repos_raw: str | list[str] = cmd.get("repos", "")
    if isinstance(repos_raw, list):
        repos = [r.strip() for r in repos_raw if r.strip()] or _DEFAULT_REPOS
    else:
        repos = [r.strip() for r in repos_raw.split(",") if r.strip()] or _DEFAULT_REPOS

    all_prs: list[ModelPRInfo] = []
    protection = BranchProtectionCache(github)
    for repo in repos:
        try:
            required_approving = protection.required_approving_review_count(repo)
            prs = github.fetch_open_prs(repo)
        except ValueError as exc:
            _log.warning("skipping repo %r — invalid format: %s", repo, exc)
            continue
        except GitHubTransportError as exc:
            _log.error("skipping repo %r — GitHub transport error: %s", repo, exc)
            continue
        for pr in prs:
            all_prs.append(_to_pr_info(pr, repo, required_approving))

    failure_history = _load_failure_history(state_dir)

    return ModelMergeSweepRequest(
        prs=all_prs,
        require_approval=cmd.get("require_approval", True),
        merge_method=cmd.get("merge_method", "squash"),
        max_total_merges=cmd.get("max_total_merges", 0),
        skip_polish=cmd.get("skip_polish", False),
        failure_history=failure_history,
        run_id=cmd.get("correlation_id", ""),
        use_lifecycle_ordering=cmd.get("use_lifecycle_ordering", False),
    )


async def _run_consumer(broker: str, group_id: str, state_dir: str) -> None:
    try:
        from aiokafka import (  # type: ignore[import-untyped]
            AIOKafkaConsumer,
            AIOKafkaProducer,
        )
    except ImportError:
        _log.error(
            "aiokafka is not installed. Install with: uv add aiokafka. "
            "Cannot start Kafka consumer."
        )
        sys.exit(1)

    # Fail-fast: GH_PAT must be present for GitHubHttpClient
    github = GitHubHttpClient()

    consumer = AIOKafkaConsumer(
        TOPIC_MERGE_SWEEP_START,
        bootstrap_servers=broker,
        group_id=group_id,
        value_deserializer=lambda b: json.loads(b.decode("utf-8")),
        auto_offset_reset="latest",
        enable_auto_commit=True,
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=broker,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )

    await consumer.start()
    await producer.start()
    _log.info(
        "merge-sweep consumer started — broker=%s group=%s topic=%s",
        broker,
        group_id,
        TOPIC_MERGE_SWEEP_START,
    )

    handler = NodeMergeSweep()
    stop_event = asyncio.Event()

    def _signal_handler(sig: int, _: Any) -> None:
        _log.info("received signal %s, shutting down", sig)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _signal_handler)

    try:
        async for msg in consumer:
            if stop_event.is_set():
                break

            cmd: dict[str, Any] = msg.value if isinstance(msg.value, dict) else {}
            correlation_id = cmd.get("correlation_id", "unknown")
            _log.info(
                "received merge-sweep-start command correlation_id=%s", correlation_id
            )

            try:
                request = _build_request(github, cmd, state_dir)
                result = handler.handle(request)
                payload: dict[str, Any] = {
                    "correlation_id": correlation_id,
                    "status": result.status,
                    "track_a_count": len(result.track_a_merge),
                    "track_a_resolve_count": len(result.track_a_resolve),
                    "track_b_count": len(result.track_b_polish),
                    "skipped_count": len(result.skipped),
                    "failure_history_summary": result.failure_history_summary.model_dump(),
                }
                await producer.send_and_wait(TOPIC_MERGE_SWEEP_COMPLETED, payload)
                _log.info(
                    "merge-sweep-completed emitted correlation_id=%s status=%s",
                    correlation_id,
                    result.status,
                )
            except Exception as exc:
                _log.error(
                    "merge-sweep failed for correlation_id=%s: %s",
                    correlation_id,
                    exc,
                    exc_info=True,
                )
    finally:
        await consumer.stop()
        await producer.stop()
        _log.info("merge-sweep consumer stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    broker = os.environ.get("KAFKA_BROKER", "localhost:9092")
    group_id = os.environ.get("MERGE_SWEEP_GROUP", "omnimarket.merge_sweep.consume.v1")
    state_dir = os.environ.get("ONEX_STATE_DIR", os.path.expanduser("~/.onex_state"))

    asyncio.run(_run_consumer(broker, group_id, state_dir))


if __name__ == "__main__":
    main()
