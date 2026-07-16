#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# publish_occ_autobind_command.py
#
# Thin-publishes onex.cmd.omnimarket.occ-autobind.v1 when a product PR is opened or
# synchronized (OMN-13317 / F1). The command is consumed by
# node_pr_lifecycle_fix_effect, which routes it to the OccCompanionEmitter under
# the receipt_evidence_source_autobind block reason: it detects the ticket,
# generates a receipt stamped with the real PR head + number, opens/syncs the
# OCC binding PR, recomputes contract_sha256 across all matching receipts, and
# PATCHes Evidence-Source: OCC#<n> back onto the product PR so occ-preflight
# goes green with zero manual edits.
#
# Payload (ModelPrLifecycleFixCommand-shaped so payload_type_match routes it —
# OMN-13990): {correlation_id, pr_number, repo, block_reason, ticket_id,
# requested_at}. The head SHA is NOT carried; the adapter re-resolves it.
#
# Emitted by the call-occ-autobind GHA workflow on pull_request:
# [opened, synchronize].
#
# BROKER DECISION (mirrors publish_pr_merged_event.py, OMN-13226):
#   Runs on the self-hosted omnibase-ci runner for trusted (non-fork) PR events.
#   That runner reaches the LOCAL lane Redpanda broker directly:
#   KAFKA_BOOTSTRAP_SERVERS is sourced from ~/.omnibase/.env on the runner
#   (plaintext LAN endpoint). Transport is resolved from the env, never
#   hardcoded: SASL_SSL/PLAIN when SASL creds are set (cloud broker), plaintext
#   otherwise (local lane). When no broker is resolvable (fork PR on a cloud
#   runner with no broker provisioned) the publish is SKIPPED with a loud
#   warning and exit 0, so a misconfig is visible but does not red every PR.
#
# Ticket: OMN-13317
#
# Required environment variables (when not --dry-run):
#   KAFKA_BOOTSTRAP_SERVERS   -- canonical bus broker endpoint
#   PR_REPO                   -- repository slug, e.g. OmniNode-ai/omnibase_infra
#   PR_NUMBER                 -- PR number as a string
#   PR_HEAD_SHA               -- product PR head commit SHA
#
# Optional:
#   KAFKA_SASL_USERNAME       -- SASL username / API key (cloud broker only)
#   KAFKA_SASL_PASSWORD       -- SASL password / API secret (cloud broker only)
#   PR_TITLE                  -- PR title for ticket extraction
#   PR_TICKET                 -- Linear ticket ID (overrides extraction)
#
# Usage:
#   python scripts/publish_occ_autobind_command.py [--dry-run]

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import click

# Canonical topic constant (single source of truth in omnimarket.events.topics).
# Load the module by file path so this thin GHA script does not execute
# omnimarket.events.__init__, which imports the full product dependency graph.
_TOPICS_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "events" / "topics.py"
)
_TOPICS_SPEC = importlib.util.spec_from_file_location(
    "omnimarket_events_topics_for_occ_autobind", _TOPICS_PATH
)
if _TOPICS_SPEC is None or _TOPICS_SPEC.loader is None:
    raise RuntimeError(f"Could not load topic registry from {_TOPICS_PATH}")
_TOPICS_MODULE = importlib.util.module_from_spec(_TOPICS_SPEC)
_TOPICS_SPEC.loader.exec_module(_TOPICS_MODULE)

TOPIC = _TOPICS_MODULE.OCC_AUTOBIND_COMMAND_TOPIC_V1

_TICKET_RE = re.compile(r"OMN-\d+", re.IGNORECASE)

# Wire literal for the block reason the runtime routes this command under.
# MUST equal EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND.value — asserted
# by tests/unit/nodes/node_pr_lifecycle_fix_effect/test_publish_occ_autobind_command.py
# rather than imported here (importing the node's model package into this thin
# GHA-runner script would pull in far more than the minimal deps the workflow
# installs). OMN-13990.
_BLOCK_REASON_AUTOBIND = "receipt_evidence_source_autobind"


def _is_trusted_runner() -> bool:
    """Return whether this run is on the trusted self-hosted lane (OMN-14451).

    Mirrors the same fork/non-fork test the workflow uses to pick `runs-on:`,
    threaded through as ``RUNNER_IS_TRUSTED`` so this script can tell an
    expected fork-runner skip (no broker provisioned, ubuntu-latest) apart
    from a real misconfiguration on the trusted runner, where the broker MUST
    be resolvable. Required (not defaulted) so a wiring gap fails loudly
    instead of silently choosing the permissive branch.
    """
    raw = os.environ.get("RUNNER_IS_TRUSTED", "").strip().lower()
    if raw not in ("true", "false"):
        click.echo(
            f"ERROR: RUNNER_IS_TRUSTED must be 'true' or 'false', got {raw!r}. "
            "Refusing to guess whether a missing broker is an expected fork "
            "skip or a trusted-runner misconfiguration.",
            err=True,
        )
        sys.exit(1)
    return raw == "true"


def _extract_ticket(title: str, branch: str = "") -> str:
    """Extract the first Linear ticket reference (OMN-NNN) from title or branch."""
    for source in (title, branch):
        m = _TICKET_RE.search(source)
        if m:
            return m.group(0).upper()
    return ""


def _kafka_producer_config(
    bootstrap_servers: str,
    username: str,
    password: str,
) -> dict[str, str | int | float | bool]:
    """Resolve the producer transport from the env (mirrors pr-merged publisher).

    SASL_SSL/PLAIN when SASL credentials are supplied (cloud broker); plaintext
    otherwise (the local lane Redpanda broker, which has no SASL). The broker
    endpoint is always taken from ``bootstrap_servers`` — never hardcoded.
    """
    config: dict[str, str | int | float | bool] = {
        "bootstrap.servers": bootstrap_servers,
    }
    if username and password:
        config["security.protocol"] = "SASL_SSL"
        config["sasl.mechanisms"] = "PLAIN"
        config["sasl.username"] = username
        config["sasl.password"] = password
    return config


def build_payload(
    repo: str,
    pr_number: int,
    ticket: str,
    correlation_id: str,
) -> dict[str, object]:
    """Return an occ-autobind command payload shaped as ModelPrLifecycleFixCommand.

    The runtime consumes this off ``onex.cmd.omnimarket.occ-autobind.v1`` and the
    contract's ``payload_type_match`` routing validates it against
    ``ModelPrLifecycleFixCommand`` (``extra='forbid'``) before dispatching to
    ``HandlerPrLifecycleFix`` under the ``receipt_evidence_source_autobind`` block
    reason. The keys here MUST be exactly the command model's fields — the old
    ``{event_id, topic, pr_head_sha, ticket}`` shape never matched, so the command
    was silently DLQ'd and the emitter never fired (OMN-13990). The adapter
    re-resolves the head SHA from GitHub, so it is not carried on the wire.
    """
    return {
        "correlation_id": correlation_id,
        "pr_number": pr_number,
        "repo": repo,
        "block_reason": _BLOCK_REASON_AUTOBIND,
        "ticket_id": ticket or None,
        "requested_at": datetime.now(UTC).isoformat(),
    }


def publish_occ_autobind_command(
    bootstrap_servers: str,
    username: str,
    password: str,
    repo: str,
    pr_number: int,
    ticket: str,
) -> str:
    """Publish onex.cmd.omnimarket.occ-autobind.v1 to Kafka. Returns the correlation_id."""
    from confluent_kafka import Producer  # type: ignore[import-untyped,unused-ignore]

    correlation_id = str(uuid.uuid4())
    payload = build_payload(
        repo=repo,
        pr_number=pr_number,
        ticket=ticket,
        correlation_id=correlation_id,
    )

    producer = Producer(_kafka_producer_config(bootstrap_servers, username, password))

    delivery_error: BaseException | None = None

    def _on_delivery(err: object, _msg: object) -> None:
        nonlocal delivery_error
        if err is not None:
            delivery_error = RuntimeError(str(err))

    message = json.dumps(payload, default=str).encode("utf-8")
    key = f"occ-autobind/{repo}/{pr_number}".encode()

    producer.produce(
        topic=TOPIC,
        key=key,
        value=message,
        on_delivery=_on_delivery,
    )
    # OMN-14639: flush() returns the number of messages STILL in the producer
    # queue when the timeout elapses. A broker that refuses the connection (e.g.
    # the target lane is down / KAFKA_BOOTSTRAP_SERVERS is mispointed) leaves the
    # message queued and unacked; librdkafka's per-message delivery timeout
    # (message.timeout.ms, default 300000ms) is far larger than this 30s flush
    # window, so `_on_delivery` never fires and `delivery_error` stays None. The
    # old code ignored the flush return and therefore reported success on an
    # UNDELIVERED command — the exact "runs green while publishing nothing" bug
    # class OMN-14451 set out to kill, but only for the *unset broker* case. A
    # non-zero remaining count means the command did NOT reach the broker, so it
    # is a hard delivery failure, not success.
    remaining = producer.flush(timeout=30)

    if delivery_error is not None:
        raise RuntimeError(f"Kafka delivery failed: {delivery_error}") from None

    if remaining and remaining > 0:
        raise RuntimeError(
            f"Kafka delivery timed out: {remaining} message(s) still undelivered "
            f"to {bootstrap_servers} after a 30s flush (broker unreachable / "
            "connection refused). Refusing to report success on an undelivered "
            "occ-autobind command (OMN-14639)."
        )

    return correlation_id


@click.command()
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print payload without publishing to Kafka",
)
def main(dry_run: bool) -> None:
    """Publish onex.cmd.omnimarket.occ-autobind.v1 for a product PR open/synchronize.

    All inputs are read from environment variables injected by the GHA workflow:
    PR_REPO, PR_NUMBER, PR_HEAD_SHA, PR_TITLE (optional), PR_TICKET (optional).
    """
    repo = os.environ.get("PR_REPO", "")
    pr_number_str = os.environ.get("PR_NUMBER", "")
    pr_head_sha = os.environ.get("PR_HEAD_SHA", "")
    title = os.environ.get("PR_TITLE", "")
    ticket_env = os.environ.get("PR_TICKET", "")

    if not repo or not pr_number_str or not pr_head_sha:
        click.echo(
            "ERROR: PR_REPO, PR_NUMBER, and PR_HEAD_SHA must all be set",
            err=True,
        )
        sys.exit(1)

    try:
        pr_number = int(pr_number_str)
    except ValueError:
        click.echo(
            f"ERROR: PR_NUMBER must be an integer, got: {pr_number_str!r}", err=True
        )
        sys.exit(1)

    ticket = ticket_env or _extract_ticket(title)

    correlation_id = str(uuid.uuid4())
    payload = build_payload(
        repo=repo,
        pr_number=pr_number,
        ticket=ticket,
        correlation_id=correlation_id,
    )

    click.echo(
        f"occ-autobind command: repo={repo} pr={pr_number} "
        f"head={pr_head_sha} ticket={ticket!r}"
    )

    if dry_run:
        click.echo("(dry-run: skipping Kafka publish)")
        click.echo(json.dumps(payload, indent=2))
        sys.exit(0)

    bootstrap_servers = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "")
    username = os.environ.get("KAFKA_SASL_USERNAME", "")
    password = os.environ.get("KAFKA_SASL_PASSWORD", "")

    if not bootstrap_servers:
        if _is_trusted_runner():
            # OMN-14451: this is the exact bug class that let the publisher run
            # green while publishing nothing for its entire lifetime. On the
            # trusted self-hosted lane for an occ-autobind-eligible PR, a
            # broker MUST be resolvable (secrets.KAFKA_BOOTSTRAP_SERVERS) --
            # a publisher that silently no-ops here is worse than one that is
            # absent, so this fails the job instead of exiting 0.
            click.echo(
                "ERROR: KAFKA_BOOTSTRAP_SERVERS is not set on the TRUSTED "
                "self-hosted runner for an occ-autobind-eligible PR. The "
                "broker MUST be resolvable here. Failing loudly instead of "
                "silently skipping (OMN-14451).",
                err=True,
            )
            sys.exit(1)
        click.echo(
            "WARNING: KAFKA_BOOTSTRAP_SERVERS is not set -- skipping "
            "occ-autobind publish (expected on a fork/cloud runner with no "
            "broker provisioned). Exiting 0.",
            err=True,
        )
        sys.exit(0)

    try:
        published_id = publish_occ_autobind_command(
            bootstrap_servers=bootstrap_servers,
            username=username,
            password=password,
            repo=repo,
            pr_number=pr_number,
            ticket=ticket,
        )
    except Exception as exc:
        click.echo(f"Delivery error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Published {TOPIC} event_id={published_id}")


if __name__ == "__main__":
    main()
