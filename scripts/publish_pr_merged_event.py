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
# F3 BROKER DECISION (OMN-13226), re-resolved by verified reachability (OMN-13226
# follow-up):
#   The workflow runs on the self-hosted omnibase-ci runner (which lives on
#   .201) for trusted, non-fork merge events. That runner reaches the LOCAL lane
#   Redpanda broker directly: KAFKA_BOOTSTRAP_SERVERS is sourced from
#   ~/.omnibase/.env on the runner and points at the lane broker (plaintext LAN
#   endpoint, e.g. localhost:19092). The event therefore lands on the same
#   Redpanda lane that node_pr_merged_projection (T3, OMN-13227) consumes, so
#   GET /projection/onex.evt.github.pr-merged.v1 is served by the :3002
#   projection API for local reaper polling (T4, OMN-13228).
#
# Transport is resolved from the env, never hardcoded:
#   - When KAFKA_SASL_USERNAME and KAFKA_SASL_PASSWORD are both set, the producer
#     uses SASL_SSL/PLAIN (cloud Confluent-style broker).
#   - Otherwise it uses a plaintext connection (the local lane Redpanda broker,
#     which has no SASL). The broker endpoint always comes from
#     KAFKA_BOOTSTRAP_SERVERS — no provider or address is hardcoded.
#
# If KAFKA_BOOTSTRAP_SERVERS is unset (true misconfiguration — e.g. the workflow
# was forced onto a cloud runner with no broker provisioned), the publish is
# SKIPPED with a loud warning and exit 0, so a misconfig is visible in the logs
# but does NOT red every merge. On the self-hosted runner the broker IS
# configured, so the event actually publishes.
#
# Ticket: OMN-13226
#
# Required environment variables (when not --dry-run):
#   KAFKA_BOOTSTRAP_SERVERS   -- canonical bus broker endpoint (from ~/.omnibase/.env
#                                on the self-hosted runner; from the runner Infisical
#                                environment for a cloud broker)
#   PR_REPO                   -- repository slug, e.g. OmniNode-ai/omnimarket
#   PR_BRANCH                 -- head branch name of the merged PR
#   PR_NUMBER                 -- PR number as a string
#   PR_MERGED_AT              -- ISO-8601 merge timestamp from GitHub event
#
# Optional:
#   KAFKA_SASL_USERNAME       -- SASL username / API key (cloud broker only)
#   KAFKA_SASL_PASSWORD       -- SASL password / API secret (cloud broker only)
#   PR_TICKET                 -- Linear ticket ID extracted from branch/title
#
# Usage:
#   python scripts/publish_pr_merged_event.py [--dry-run]

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


def _load_pr_merged_topic() -> str:
    """Resolve PR_MERGED_TOPIC_V1 from omnimarket/events/topics.py without
    importing the omnimarket.events package.

    The publisher is bus-native: it needs exactly one string constant and must
    run in the minimal CI env the pr-merged-publisher workflow provisions
    (confluent-kafka + click + pydantic only). Importing
    ``omnimarket.events.topics`` would first execute the parent package
    ``omnimarket/events/__init__.py``, which eagerly re-exports a model graph
    that imports ``omnibase_core`` — a package that is NOT installed in that CI
    env. So the package import crashes the publish path with
    ``ModuleNotFoundError: No module named 'omnibase_core'``.

    To keep ``topics.py`` the single source of truth (no inlined literal that
    could drift) while staying self-contained, load that one module by file
    path via importlib. This executes only ``topics.py`` — a pure-constant
    module with no upstream deps — and never touches the package ``__init__``.
    """
    topics_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "omnimarket"
        / "events"
        / "topics.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_omnimarket_events_topics_standalone", topics_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load topic constants from {topics_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.PR_MERGED_TOPIC_V1)


# Canonical topic constant (single source of truth in
# omnimarket/events/topics.py), loaded standalone so the bus-native publish
# path does not drag in the omnimarket.events package (and its omnibase_core
# dependency) — which is absent from the minimal publisher CI env.
TOPIC = _load_pr_merged_topic()

_TICKET_RE = re.compile(r"OMN-\d+", re.IGNORECASE)


def _extract_ticket(branch: str, title: str = "") -> str:
    """Extract the first Linear ticket reference (OMN-NNN) from branch or PR title."""
    for source in (branch, title):
        m = _TICKET_RE.search(source)
        if m:
            return m.group(0).upper()
    return ""


def _kafka_producer_config(
    bootstrap_servers: str,
    username: str,
    password: str,
) -> dict[str, str | int | float | bool]:
    """Resolve the producer transport from the env.

    SASL_SSL/PLAIN when SASL credentials are supplied (cloud broker); plaintext
    otherwise (the local lane Redpanda broker, which has no SASL). The broker
    endpoint is always taken from ``bootstrap_servers`` (KAFKA_BOOTSTRAP_SERVERS)
    — never hardcoded.
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
    from confluent_kafka import Producer  # type: ignore[import-untyped,unused-ignore]

    event_id = str(uuid.uuid4())
    payload = build_payload(
        repo=repo,
        branch=branch,
        pr_number=pr_number,
        ticket=ticket,
        merged_at=merged_at,
        event_id=event_id,
    )

    producer = Producer(_kafka_producer_config(bootstrap_servers, username, password))

    delivery_error: BaseException | None = None

    def _on_delivery(err: object, _msg: object) -> None:
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
        # True misconfiguration: no broker resolvable from the env. On the
        # self-hosted runner the broker comes from ~/.omnibase/.env, so reaching
        # here means the runner was misrouted (e.g. forced onto a cloud runner
        # with no broker provisioned). Make it loudly visible but do NOT red the
        # merge — a misconfig should not fail every PR's checks.
        click.echo(
            "WARNING: KAFKA_BOOTSTRAP_SERVERS is not set -- skipping pr-merged "
            "publish (no broker resolvable from the environment). The "
            "self-hosted omnibase-ci runner sources this from ~/.omnibase/.env; "
            "if you see this on the trusted runner, the runner env is "
            "misconfigured. Exiting 0 so a misconfig does not fail every merge.",
            err=True,
        )
        sys.exit(0)

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
