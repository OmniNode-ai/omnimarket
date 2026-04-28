#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# trigger_rebuild_on_merge.py
#
# Publishes onex.cmd.deploy.rebuild-requested.v1 when a merged PR contains
# runtime changes. Called from the runtime-rebuild-trigger GHA workflow on
# push to main.
#
# Triggers when:
#   - PR had the "runtime_change" label, OR
#   - Any changed file matches src/omnimarket/** or src/omnibase_infra/nodes/**
#
# Ticket: OMN-8917
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
#     --git-ref "origin/main" \
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
from typing import Any

import click

TOPIC = "onex.cmd.deploy.rebuild-requested.v1"
COMPLETED_TOPIC = "onex.evt.deploy.rebuild-completed.v1"

_RUNTIME_PATH_PATTERNS = [
    "src/omnimarket/*",
    "src/omnibase_infra/nodes/*",
]

_RUNTIME_LABEL = "runtime_change"


def should_trigger(changed_files: list[str], labels: list[str]) -> bool:
    """Return True if a rebuild should be triggered."""
    if _RUNTIME_LABEL in labels:
        return True
    for f in changed_files:
        for pattern in _RUNTIME_PATH_PATTERNS:
            if fnmatch.fnmatch(f, pattern) or f.startswith(pattern.rstrip("*")):
                return True
    return False


def _sign_envelope(envelope: dict, secret: str) -> dict:
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


def publish_rebuild_event(
    bootstrap_servers: str,
    username: str,
    password: str,
    hmac_secret: str,
    git_ref: str,
    correlation_id: str,
    requested_by: str,
) -> None:
    """Publish a signed rebuild-requested event to Kafka via SASL_SSL."""
    from confluent_kafka import Producer  # type: ignore[import-untyped]

    envelope = {
        "correlation_id": correlation_id,
        "requested_by": requested_by,
        "scope": "runtime",
        "services": [],
        "git_ref": git_ref,
        "requested_at": datetime.now(UTC).isoformat(),
    }
    signed = _sign_envelope(envelope, hmac_secret)

    producer = Producer(_kafka_sasl_config(bootstrap_servers, username, password))

    delivery_error: BaseException | None = None

    def _on_delivery(err: object, _msg: object) -> None:  # type: ignore[misc]
        nonlocal delivery_error
        if err is not None:
            delivery_error = RuntimeError(str(err))

    message = json.dumps(signed, default=str).encode("utf-8")
    key = f"gha-rebuild/{correlation_id}".encode()

    producer.produce(
        topic=TOPIC,
        key=key,
        value=message,
        on_delivery=_on_delivery,
    )
    producer.flush(timeout=30)

    if delivery_error is not None:
        raise RuntimeError(f"Kafka delivery failed: {delivery_error}") from None


def wait_for_rebuild_completion(
    bootstrap_servers: str,
    username: str,
    password: str,
    correlation_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Wait for a deploy-agent rebuild-completed event by correlation ID."""
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
    "--git-ref",
    default="origin/main",
    help="Git ref to rebuild (default: origin/main)",
)
@click.option(
    "--requested-by",
    default="gha-runtime-rebuild-trigger",
    help="Identifier for who is requesting the rebuild",
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
    git_ref: str,
    requested_by: str,
    correlation_id: str,
    dry_run: bool,
    wait_for_completion: bool,
    completion_timeout_seconds: float,
) -> None:
    """Publish rebuild-requested event if PR contains runtime changes.

    Triggers when PR had runtime_change label OR changed files match
    src/omnimarket/** or src/omnibase_infra/nodes/**.
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

    if not should_trigger(files, label_list):
        click.echo(
            "No rebuild trigger: no runtime_change label or runtime path changes detected."
        )
        sys.exit(0)

    click.echo(
        f"Rebuild triggered: git_ref={git_ref} correlation_id={corr_id} "
        f"labels={label_list} files_matched={[f for f in files if any(f.startswith(p.rstrip('*')) for p in _RUNTIME_PATH_PATTERNS)]}"
    )

    if dry_run:
        click.echo("(dry-run: skipping Kafka publish)")
        sys.exit(0)

    bootstrap_servers = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "")
    username = os.environ.get("KAFKA_SASL_USERNAME", "")
    password = os.environ.get("KAFKA_SASL_PASSWORD", "")
    hmac_secret = os.environ.get("DEPLOY_AGENT_HMAC_SECRET", "")

    if not bootstrap_servers:
        click.echo("KAFKA_BOOTSTRAP_SERVERS is not set -- skipping publish")
        sys.exit(0)
    if not username or not password:
        click.echo("KAFKA_SASL_USERNAME and KAFKA_SASL_PASSWORD must be set", err=True)
        sys.exit(1)
    if not hmac_secret:
        click.echo("DEPLOY_AGENT_HMAC_SECRET must be set", err=True)
        sys.exit(1)

    try:
        publish_rebuild_event(
            bootstrap_servers=bootstrap_servers,
            username=username,
            password=password,
            hmac_secret=hmac_secret,
            git_ref=git_ref,
            correlation_id=corr_id,
            requested_by=requested_by,
        )
    except Exception as exc:
        click.echo(f"Delivery error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Published rebuild-requested to {TOPIC} (correlation_id={corr_id})")

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
