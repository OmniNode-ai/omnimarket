#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# publish_pr_merged_event.py
#
# Publishes onex.evt.github.pr-merged.v1 when a PR merges on any repo.
# Called from the pr-merged-publisher GHA workflow on pull_request: closed
# with merged == true.
#
# Payload: {repo, branch, pr_number, ticket, merged_at}
#
# F3 BROKER DECISION (OMN-13226), resolved by verified reachability:
#   This workflow runs on ubuntu-latest (cloud runner) and therefore CANNOT
#   reach the private LAN broker directly. It publishes via the same SASL_SSL
#   transport as OMN-8917 (trigger_rebuild_on_merge.py), emitting to whatever
#   broker KAFKA_BOOTSTRAP_SERVERS resolves to in the runner Infisical
#   environment (the canonical bus endpoint) — the transport is NOT hardcoded
#   to a single provider. The T3 projection node (OMN-13227)
#   bridges/materializes the event onto the .201 Redpanda lane so
#   GET /projection/onex.evt.github.pr-merged.v1 is served by the :3002
#   projection API for local reaper polling (T4, OMN-13228).
#
# Evidence for runner constraint: omnimarket/.github/workflows/
#   runtime-rebuild-trigger.yml uses runs-on: ubuntu-latest; pr-review-bot.yml
#   documents that KAFKA_BOOTSTRAP_SERVERS is pre-mounted in the runner
#   Infisical environment. No workflow in this repo uses a self-hosted runner
#   that reaches the private LAN.
#
# Ticket: OMN-13226
#
# Required environment variables (when not --dry-run, resolved from Infisical):
#   KAFKA_BOOTSTRAP_SERVERS   -- canonical bus broker endpoint
#   KAFKA_SASL_USERNAME       -- SASL username / API key
#   KAFKA_SASL_PASSWORD       -- SASL password / API secret
#   PR_REPO                   -- repository slug, e.g. OmniNode-ai/omnimarket
#   PR_BRANCH                 -- head branch name of the merged PR
#   PR_NUMBER                 -- PR number as a string
#   PR_MERGED_AT              -- ISO-8601 merge timestamp from GitHub event
#
# Optional:
#   PR_TICKET                 -- Linear ticket ID extracted from branch/title
#
# Usage:
#   python scripts/publish_pr_merged_event.py [--dry-run]

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime

import click

TOPIC = "onex.evt.github.pr-merged.v1"  # onex-topic-allow: canonical topic registry; declared in node_pr_merged_projection contract.yaml subscribe_topics (OMN-13226/13227)

_TICKET_RE = re.compile(r"OMN-\d+", re.IGNORECASE)


def _extract_ticket(branch: str, title: str = "") -> str:
    """Extract the first Linear ticket reference (OMN-NNN) from branch or PR title."""
    for source in (branch, title):
        m = _TICKET_RE.search(source)
        if m:
            return m.group(0).upper()
    return ""


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


def build_payload(
    repo: str,
    branch: str,
    pr_number: int,
    ticket: str,
    merged_at: str,
    event_id: str,
) -> dict[str, object]:
    """Return the canonical pr-merged event payload."""
    return {
        "event_id": event_id,
        "topic": TOPIC,
        "repo": repo,
        "branch": branch,
        "pr_number": pr_number,
        "ticket": ticket,
        "merged_at": merged_at,
        "published_at": datetime.now(UTC).isoformat(),
    }


def publish_pr_merged_event(
    bootstrap_servers: str,
    username: str,
    password: str,
    repo: str,
    branch: str,
    pr_number: int,
    ticket: str,
    merged_at: str,
) -> str:
    """Publish onex.evt.github.pr-merged.v1 to Kafka. Returns the event_id."""
    from confluent_kafka import Producer  # type: ignore[import-untyped]

    event_id = str(uuid.uuid4())
    payload = build_payload(
        repo=repo,
        branch=branch,
        pr_number=pr_number,
        ticket=ticket,
        merged_at=merged_at,
        event_id=event_id,
    )

    producer = Producer(_kafka_sasl_config(bootstrap_servers, username, password))

    delivery_error: BaseException | None = None

    def _on_delivery(err: object, _msg: object) -> None:  # type: ignore[misc]
        nonlocal delivery_error
        if err is not None:
            delivery_error = RuntimeError(str(err))

    message = json.dumps(payload, default=str).encode("utf-8")
    key = f"pr-merged/{repo}/{pr_number}".encode()

    producer.produce(
        topic=TOPIC,
        key=key,
        value=message,
        on_delivery=_on_delivery,
    )
    producer.flush(timeout=30)

    if delivery_error is not None:
        raise RuntimeError(f"Kafka delivery failed: {delivery_error}") from None

    return event_id


@click.command()
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print payload without publishing to Kafka",
)
def main(dry_run: bool) -> None:
    """Publish onex.evt.github.pr-merged.v1 for a merged PR.

    All inputs are read from environment variables injected by the GHA workflow:
    PR_REPO, PR_BRANCH, PR_NUMBER, PR_MERGED_AT, PR_TICKET (optional).
    """
    repo = os.environ.get("PR_REPO", "")
    branch = os.environ.get("PR_BRANCH", "")
    pr_number_str = os.environ.get("PR_NUMBER", "")
    merged_at = os.environ.get("PR_MERGED_AT", "")
    ticket_env = os.environ.get("PR_TICKET", "")

    if not repo or not branch or not pr_number_str or not merged_at:
        click.echo(
            "ERROR: PR_REPO, PR_BRANCH, PR_NUMBER, and PR_MERGED_AT must all be set",
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

    # Prefer env-provided ticket; fall back to branch-name extraction
    ticket = ticket_env or _extract_ticket(branch)

    event_id = str(uuid.uuid4())
    payload = build_payload(
        repo=repo,
        branch=branch,
        pr_number=pr_number,
        ticket=ticket,
        merged_at=merged_at,
        event_id=event_id,
    )

    click.echo(
        f"pr-merged event: repo={repo} branch={branch} pr={pr_number} "
        f"ticket={ticket!r} merged_at={merged_at}"
    )

    if dry_run:
        click.echo("(dry-run: skipping Kafka publish)")
        click.echo(json.dumps(payload, indent=2))
        sys.exit(0)

    bootstrap_servers = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "")
    username = os.environ.get("KAFKA_SASL_USERNAME", "")
    password = os.environ.get("KAFKA_SASL_PASSWORD", "")

    if not bootstrap_servers:
        click.echo("KAFKA_BOOTSTRAP_SERVERS is not set -- skipping publish", err=True)
        sys.exit(1)
    if not username or not password:
        click.echo("KAFKA_SASL_USERNAME and KAFKA_SASL_PASSWORD must be set", err=True)
        sys.exit(1)

    try:
        published_id = publish_pr_merged_event(
            bootstrap_servers=bootstrap_servers,
            username=username,
            password=password,
            repo=repo,
            branch=branch,
            pr_number=pr_number,
            ticket=ticket,
            merged_at=merged_at,
        )
    except Exception as exc:
        click.echo(f"Delivery error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Published {TOPIC} event_id={published_id}")


if __name__ == "__main__":
    main()
