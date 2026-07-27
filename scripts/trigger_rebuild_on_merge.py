#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# trigger_rebuild_on_merge.py
#
# Publishes onex.cmd.omnimarket.redeploy-start.v1 (consumed by
# node_redeploy_orchestrator) when a merged PR contains runtime changes. Called
# from the runtime-rebuild-trigger GHA workflow on PR merge to dev or main.
#
# node_redeploy_orchestrator owns the deployment lifecycle (lane policy via the
# prod-gate compute, digest pinning, readiness, rollback) and its deploy
# publish-monitor EFFECT is the SOLE emitter of
# onex.cmd.deploy.rebuild-requested.v1 to the deploy agent. CI publishes a typed
# start command only; it never talks to the deploy agent directly.
#
# Triggers when:
#   - PR had the "runtime_change" label, OR
#   - Any changed file matches src/omnimarket/** or src/omnibase_infra/nodes/**
#
# Lane policy (the triggering ref decides the lane — no hardcoded origin/main):
#   - merge to dev  -> runtime_lane=dev,            source_branch=dev
#   - merge to main -> runtime_lane=stability-test, source_branch=main
#     (dev->main promotion proves the stability lane; prod deploys the
#      stability-proven digest later via node_redeploy_orchestrator, not from CI)
#
# Fail-closed effect discipline (RT-5, OMN-14470): once a runtime change is
# detected and this is not a dry run, the job's PURPOSE is to emit exactly one
# redeploy-start command. Emitting zero — because a publish precondition (broker,
# SASL creds, HMAC secret) is missing, or because the emit delivered nothing —
# MUST fail closed (non-zero, red), never "skip publish" green. This is why this
# copy keeps require_producer_preconditions / assert_producer_emitted even though
# the omnibase_infra sibling does not.
#
# Tickets: OMN-8917 (original auto-trigger), OMN-12573 (re-point to
#          node_redeploy), OMN-14470 (RT-5 fail-closed), OMN-14702 (omnimarket
#          re-point — completes the OMN-12573 omnimarket half).
#
# Required environment variables (when not --dry-run):
#   KAFKA_BOOTSTRAP_SERVERS   -- broker address(es), e.g. host:9092
#   KAFKA_SASL_USERNAME       -- SASL username / API key
#   KAFKA_SASL_PASSWORD       -- SASL password / API secret
#   DEPLOY_AGENT_HMAC_SECRET  -- HMAC secret for payload signing
#
# Usage:
#   python scripts/trigger_rebuild_on_merge.py \
#     --changed-files "src/omnimarket/nodes/foo/handler.py,README.md" \
#     --labels "runtime_change,bug" \
#     --base-branch "dev" \
#     --source-sha "<merge_commit_sha>" \
#     [--dry-run]

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

# The RT-5 fail-closed assertion (producer_effect_assertion.py) is co-located in
# scripts/. Ensure scripts/ is importable when this file is loaded by path
# (CI / tests via importlib), not only when run as `python scripts/...`.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# CI publishes the node_redeploy_orchestrator start command; the orchestrator's
# deploy publish-monitor effect is the sole emitter of the deploy-agent rebuild
# command downstream.
TOPIC = "onex.cmd.omnimarket.redeploy-start.v1"
COMPLETED_TOPIC = "onex.evt.deploy.rebuild-completed.v1"

_RUNTIME_PATH_PATTERNS = [
    "src/omnimarket/*",
    "src/omnibase_infra/nodes/*",
]

_RUNTIME_LABEL = "runtime_change"

# Maps the merged PR's base branch to a runtime lane. Values match
# deploy_agent.events.EnumRuntimeLane (dev | stability-test | prod). prod is not
# triggerable from CI: production deploys the stability-proven digest through
# node_redeploy_orchestrator's promotion gate, never from a merge event.
_BASE_BRANCH_LANES: dict[str, str] = {
    "dev": "dev",
    "main": "stability-test",
}


def should_trigger(changed_files: list[str], labels: list[str]) -> bool:
    """Return True if a rebuild should be triggered."""
    if _RUNTIME_LABEL in labels:
        return True
    for f in changed_files:
        for pattern in _RUNTIME_PATH_PATTERNS:
            if fnmatch.fnmatch(f, pattern) or f.startswith(pattern.rstrip("*")):
                return True
    return False


def lane_for_base_branch(base_branch: str) -> str:
    """Map a merged PR's base branch to a node_redeploy_orchestrator runtime lane.

    Fails closed on unmapped branches: a misconfigured trigger must not silently
    pick a default lane and rebuild the wrong runtime. prod is intentionally
    absent from the mapping — no CI merge event can select the prod lane.
    """
    lane = _BASE_BRANCH_LANES.get(base_branch)
    if lane is None:
        allowed = ", ".join(sorted(_BASE_BRANCH_LANES))
        msg = (
            f"No runtime lane mapping for base branch {base_branch!r}; "
            f"allowed base branches: {allowed}"
        )
        raise ValueError(msg)
    return lane


def _sign_envelope(envelope: dict[str, object], secret: str) -> dict[str, object]:
    body_dict = {k: v for k, v in envelope.items() if k != "_signature"}
    body = json.dumps(body_dict, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {**envelope, "_signature": signature}


def _kafka_sasl_config(
    bootstrap_servers: str,
    username: str,
    password: str,
) -> dict[str, str | int | float | bool]:
    return {
        "bootstrap.servers": bootstrap_servers,
        "security.protocol": "SASL_SSL",
        "sasl.mechanisms": "PLAIN",
        "sasl.username": username,
        "sasl.password": password,
    }


def build_redeploy_start_envelope(
    *,
    runtime_lane: str,
    source_branch: str,
    source_sha: str,
    correlation_id: str,
    requested_by: str,
    hmac_secret: str,
) -> dict[str, object]:
    """Build and sign the canonical redeploy-start command envelope.

    Field-for-field the same shape node_redeploy_orchestrator consumes and the
    same shape the omnibase_infra sibling emits (correlation_id, requested_by,
    runtime_lane, source_branch, source_sha, requires_occ, requires_readiness_gate,
    requested_at, then the HMAC-SHA256 _signature over the sorted-key compact
    JSON body). Given a fixed requested_at this is deterministic — the dry-run
    proof and cross-boundary tests build the exact candidate bytes through this
    function without touching a broker.
    """
    envelope: dict[str, object] = {
        "correlation_id": correlation_id,
        "requested_by": requested_by,
        "runtime_lane": runtime_lane,
        "source_branch": source_branch,
        "source_sha": source_sha,
        # dev dogfoods OCC drafting; stability gates on readiness before prod.
        "requires_occ": True,
        "requires_readiness_gate": runtime_lane != "dev",
        "requested_at": datetime.now(UTC).isoformat(),
    }
    return _sign_envelope(envelope, hmac_secret)


def publish_redeploy_start_event(
    bootstrap_servers: str,
    username: str,
    password: str,
    hmac_secret: str,
    runtime_lane: str,
    source_branch: str,
    source_sha: str,
    correlation_id: str,
    requested_by: str,
) -> int:
    """Publish a signed redeploy-start command to node_redeploy_orchestrator via SASL_SSL.

    Returns the number of events delivered (``1`` on success). Raises on any
    delivery failure so the caller can assert a non-zero emit count — a producer
    that delivers nothing must fail closed, never report success.
    """
    from confluent_kafka import Producer  # type: ignore[import-untyped]

    signed = build_redeploy_start_envelope(
        runtime_lane=runtime_lane,
        source_branch=source_branch,
        source_sha=source_sha,
        correlation_id=correlation_id,
        requested_by=requested_by,
        hmac_secret=hmac_secret,
    )

    producer = Producer(_kafka_sasl_config(bootstrap_servers, username, password))

    delivery_error: BaseException | None = None

    def _on_delivery(err: object, _msg: object) -> None:  # type: ignore[misc]
        nonlocal delivery_error
        if err is not None:
            delivery_error = RuntimeError(str(err))

    message = json.dumps(signed, default=str).encode("utf-8")
    key = f"gha-redeploy/{correlation_id}".encode()

    producer.produce(
        topic=TOPIC,
        key=key,
        value=message,
        on_delivery=_on_delivery,
    )
    producer.flush(timeout=30)

    if delivery_error is not None:
        raise RuntimeError(f"Kafka delivery failed: {delivery_error}") from None

    # Exactly one redeploy-start command was delivered; the caller asserts N>0.
    return 1


def wait_for_rebuild_completion(
    bootstrap_servers: str,
    username: str,
    password: str,
    correlation_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Wait for a deploy-agent rebuild-completed event by correlation ID.

    node_redeploy_orchestrator propagates the start command's correlation_id
    through its deploy publish-monitor effect, so the downstream
    rebuild-completed.v1 event carries the SAME correlation_id CI minted here.
    A timeout raises TimeoutError naming the correlation_id — the durable failed
    correlation receipt for the run.
    """
    from confluent_kafka import Consumer  # type: ignore[import-untyped]

    consumer_config = {
        **_kafka_sasl_config(bootstrap_servers, username, password),
        "group.id": f"gha-runtime-rebuild-trigger-{correlation_id[:8]}",
        "auto.offset.reset": "latest",
        "enable.auto.commit": False,
    }
    consumer = Consumer(consumer_config)
    consumer.subscribe([COMPLETED_TOPIC])

    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            error = msg.error()
            if error is not None:
                raise RuntimeError(f"Kafka consumer error: {error}")

            raw = msg.value()
            if isinstance(raw, bytes | bytearray):
                payload = json.loads(raw.decode("utf-8"))
            elif isinstance(raw, str):
                payload = json.loads(raw)
            else:
                payload = raw

            if not isinstance(payload, dict):
                continue
            if payload.get("correlation_id") != correlation_id:
                continue
            return payload
    finally:
        consumer.close()

    raise TimeoutError(
        f"Timed out after {timeout_seconds:.0f}s waiting for {COMPLETED_TOPIC} "
        f"correlation_id={correlation_id}"
    )


@click.command()
@click.option(
    "--changed-files",
    default="",
    help="Comma-separated list of changed file paths",
)
@click.option(
    "--labels",
    default="",
    help="Comma-separated list of PR label names",
)
@click.option(
    "--base-branch",
    required=True,
    help="Merged PR base branch (dev | main) — decides the runtime lane",
)
@click.option(
    "--source-sha",
    required=True,
    help="Merge commit SHA of the triggering PR (the ref node_redeploy_orchestrator rebuilds)",
)
@click.option(
    "--requested-by",
    default="gha-runtime-rebuild-trigger",
    help="Identifier for who is requesting the redeploy",
)
@click.option(
    "--correlation-id",
    default="",
    help="Correlation ID (auto-generated if not provided)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Check trigger conditions and print decision without publishing",
)
@click.option(
    "--wait-for-completion",
    is_flag=True,
    default=False,
    help="Wait for matching deploy-agent rebuild-completed event after publishing",
)
@click.option(
    "--completion-timeout-seconds",
    default=900.0,
    type=float,
    show_default=True,
    help="Seconds to wait for rebuild-completed when --wait-for-completion is set",
)
def main(
    changed_files: str,
    labels: str,
    base_branch: str,
    source_sha: str,
    requested_by: str,
    correlation_id: str,
    dry_run: bool,
    wait_for_completion: bool,
    completion_timeout_seconds: float,
) -> None:
    """Publish a node_redeploy_orchestrator start command if a PR contains runtime changes.

    Triggers when PR had the runtime_change label OR changed files match
    src/omnimarket/** or src/omnibase_infra/nodes/**. The triggering base branch
    decides the runtime lane; the merge SHA is the ref node_redeploy_orchestrator
    rebuilds. There is no hardcoded origin/main.
    """
    files: list[str] = (
        [f.strip() for f in changed_files.split(",") if f.strip()]
        if changed_files
        else []
    )
    label_list: list[str] = (
        [lb.strip() for lb in labels.split(",") if lb.strip()] if labels else []
    )

    corr_id = correlation_id or str(uuid.uuid4())

    # Fail closed BEFORE any trigger decision on an unmapped base branch: a
    # misconfigured workflow ref must never silently pick a default lane. prod is
    # not in the mapping, so no CI event can select the prod lane.
    runtime_lane = lane_for_base_branch(base_branch)

    if not should_trigger(files, label_list):
        click.echo(
            "No rebuild trigger: no runtime_change label or runtime path changes detected."
        )
        sys.exit(0)

    click.echo(
        f"Redeploy triggered: runtime_lane={runtime_lane} source_branch={base_branch} "
        f"source_sha={source_sha} correlation_id={corr_id} labels={label_list} "
        f"files_matched={[f for f in files if any(f.startswith(p.rstrip('*')) for p in _RUNTIME_PATH_PATTERNS)]}"
    )

    if dry_run:
        click.echo("(dry-run: skipping Kafka publish)")
        sys.exit(0)

    # Co-located RT-5 fail-closed assertion (see producer_effect_assertion.py).
    from producer_effect_assertion import (
        ProducerZeroOutputError,
        assert_producer_emitted,
        require_producer_preconditions,
    )

    bootstrap_servers = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "")
    username = os.environ.get("KAFKA_SASL_USERNAME", "")
    password = os.environ.get("KAFKA_SASL_PASSWORD", "")
    hmac_secret = os.environ.get("DEPLOY_AGENT_HMAC_SECRET", "")

    # A runtime change was detected and this is not a dry run, so the job's
    # PURPOSE is now to publish exactly one redeploy-start command. If any
    # precondition for publishing is absent (broker, SASL creds, HMAC secret),
    # this producer CANNOT emit — that is zero output and MUST fail closed
    # (RT-5 / OMN-14470), never green.
    try:
        require_producer_preconditions(
            artifact=TOPIC,
            preconditions={
                "KAFKA_BOOTSTRAP_SERVERS": bootstrap_servers,
                "KAFKA_SASL_USERNAME": username,
                "KAFKA_SASL_PASSWORD": password,
                "DEPLOY_AGENT_HMAC_SECRET": hmac_secret,
            },
        )
    except ProducerZeroOutputError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    try:
        delivered = publish_redeploy_start_event(
            bootstrap_servers=bootstrap_servers,
            username=username,
            password=password,
            hmac_secret=hmac_secret,
            runtime_lane=runtime_lane,
            source_branch=base_branch,
            source_sha=source_sha,
            correlation_id=corr_id,
            requested_by=requested_by,
        )
    except Exception as exc:
        click.echo(f"Delivery error: {exc}", err=True)
        sys.exit(1)

    # "Produced N>0, and here it is": a completed publish that delivered zero
    # events is a silent-producer failure and must go red, not report success.
    try:
        assert_producer_emitted(
            delivered, artifact=TOPIC, detail=f"correlation_id={corr_id}"
        )
    except ProducerZeroOutputError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    click.echo(
        f"Published redeploy-start to {TOPIC} "
        f"(correlation_id={corr_id}, runtime_lane={runtime_lane}, delivered={delivered})"
    )

    if wait_for_completion:
        try:
            completion = wait_for_rebuild_completion(
                bootstrap_servers=bootstrap_servers,
                username=username,
                password=password,
                correlation_id=corr_id,
                timeout_seconds=completion_timeout_seconds,
            )
        except TimeoutError as exc:
            click.echo(str(exc), err=True)
            sys.exit(1)
        except Exception as exc:
            click.echo(f"Completion monitor error: {exc}", err=True)
            sys.exit(1)

        status = str(completion.get("status", "")).lower()
        click.echo(
            "Received rebuild-completed "
            f"status={status or '<missing>'} correlation_id={corr_id}"
        )
        if status != "success":
            errors = completion.get("errors", [])
            click.echo(f"Deploy-agent rebuild failed: {errors}", err=True)
            sys.exit(1)


if __name__ == "__main__":
    main()
