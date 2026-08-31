#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# publish_pr_merged_event.py
#
# Publishes onex.evt.github.pr-merged.v1 when a PR merges on any repo.
# Called from the pr-merged-publisher GHA workflow on pull_request: closed
# with merged == true (and on workflow_dispatch for backfill/proof).
#
# Payload: {repo, branch, pr_number, ticket, merged_at}
#
# ============================================================================
# OMN-17378 -- THIS PUBLISHER WAS GREEN-BUT-SILENT FOR FOUR DAYS. READ THIS.
# ============================================================================
# Between 2026-08-27 and 2026-08-31 every run of this publisher reported
# `success` and published NOTHING. Dev-lane `onex.evt.github.pr-merged.v1` sat
# at HIGH-WATERMARK 97 (last message PR #2159, merged 2026-08-27) while
# omnimarket merged through PR #2249. Run 33436788824 is the forensic record.
#
# TWO independent silent-success paths produced that, and BOTH are now closed:
#
#   1. IGNORED FLUSH RETURN (the one that actually fired). The org/repo runner
#      variable `OMNI_TRUSTED_CI_RUNS_ON_JSON` was flipped to `["ubuntu-latest"]`
#      (the public-repo hosted-runner policy), which moved this LAN-bound job
#      onto a GitHub-hosted runner. `source ~/.omnibase/.env` no-ops there, but
#      the `KAFKA_BOOTSTRAP_SERVERS` secret WAS set, so the old "broker unset ->
#      skip" branch never triggered. librdkafka then logged
#      `Failed to resolve '***': Name or service not known`, `flush(timeout=30)`
#      returned with 1 message still queued, the return value was DISCARDED, the
#      delivery callback never fired (message.timeout.ms defaults to 300000ms,
#      far beyond the 30s flush window) -- and the script printed
#      `Published onex.evt.github.pr-merged.v1 event_id=...` and exited 0. An
#      affirmative FALSE claim of publication, which is worse than a skip.
#      This is the identical bug OMN-14639 fixed in publish_occ_autobind_command.py;
#      it was never ported here. It is ported now: a non-zero flush remainder
#      raises.
#
#   2. EXIT-0 SKIP ON AN UNSET BROKER, applied unconditionally. Graceful skip is
#      only correct on the fork/`ubuntu-latest` fallback path, where no broker is
#      provisioned by design. On the TRUSTED path an unresolvable broker is a
#      defect. The two are now distinguished EXPLICITLY by `RUNNER_IS_TRUSTED`
#      (threaded from the workflow's own fork test), not implicitly by which env
#      vars happen to be set.
#
# ROUTING DECISION (OMN-17378, recorded on the ticket with the rejected
# alternative): this workflow is pinned back to the self-hosted `omnibase-ci`
# fleet via a DEDICATED `OMNI_PR_MERGED_PUBLISHER_RUNS_ON_JSON` variable. That is
# not an override of the hosted-runner policy -- it is that policy's own written
# carve-out ("ineligible: deploys, LAN-reaching, fleet-secret-bound stay
# self-hosted with an inline reason comment"), and it is the exact remedy
# OMN-16691/OMN-16682 constraint 8 already applied to call-occ-autobind.yml after
# the same variable flip broke OCC autobind fleet-wide on 2026-08-26. The
# rejected alternative -- routing to a managed/cloud broker reachable from a
# hosted runner -- is rejected on evidence: no cloud broker is provisioned (the
# repo holds no `KAFKA_SASL_*` secret at all), and the consumer this feed exists
# to serve, node_pr_merged_projection, consumes the dev-lane Redpanda. Moving the
# producer to a broker the consumer does not read would fix the green check and
# leave the feed just as dead.
#
# BROKER RESOLUTION -- SECRET-FREE, OVERLAY-DRIVEN (OMN-14801 point 5, closed
# here): the broker is resolved from the checked-in `config/ci_bus_lanes.yaml`
# overlay via `--lane dev`, which declares the concrete dev-lane Redpanda
# external listener. No `KAFKA_BOOTSTRAP_SERVERS` secret is injected any more,
# and `source ~/.omnibase/.env` is gone -- both were opaque, unreviewable config
# surfaces, and the secret is what let the hosted-runner misroute masquerade as a
# configured broker. If a secret IS supplied anyway it is fail-loud-checked
# against the overlay (the OMN-14800 silent dev->stability repoint guard).
#
# Transport is resolved from the env, never hardcoded: SASL_SSL/PLAIN when SASL
# credentials are present (a cloud broker); plaintext otherwise (the dev-lane
# Redpanda, which has no SASL).
#
# PUBLISH RECEIPT: on success the script writes topic + event_id + partition +
# offset + broker to $GITHUB_STEP_SUMMARY, so "did it actually publish" is
# readable from the run page without a broker probe (OMN-17378 fix item 3).
#
# Ticket: OMN-13226, OMN-14639, OMN-14801, OMN-17378
#
# Required environment variables (when not --dry-run):
#   RUNNER_IS_TRUSTED         -- 'true' on the self-hosted trusted lane, 'false'
#                                on the fork/hosted fallback. No default.
#   PR_REPO                   -- repository slug, e.g. OmniNode-ai/omnimarket
#   PR_BRANCH                 -- head branch name of the merged PR
#   PR_NUMBER                 -- PR number as a string
#   PR_MERGED_AT              -- ISO-8601 merge timestamp from GitHub event
#
# Optional:
#   KAFKA_BOOTSTRAP_SERVERS   -- injected broker; checked against the overlay
#   KAFKA_SASL_USERNAME       -- SASL username / API key (cloud broker only)
#   KAFKA_SASL_PASSWORD       -- SASL password / API secret (cloud broker only)
#   PR_TICKET                 -- Linear ticket ID extracted from branch/title
#   GITHUB_STEP_SUMMARY       -- receipt sink (set by GitHub Actions)
#
# Usage:
#   python scripts/publish_pr_merged_event.py --lane dev [--dry-run]

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
from ci_bus_lanes import (
    LANE_OVERLAY_PATH,
    MODE_FROM_SECRET,
    MODE_INMEMORY,
    MODE_NO_LANE,
    MODE_UNKNOWN_LANE,
    is_trusted_runner,
    load_lane_overlay,
    resolve_lane_broker,
)


def _load_pr_merged_topic() -> str:
    """Resolve PR_MERGED_TOPIC_V1 from omnimarket/events/topics.py without
    importing the omnimarket.events package.

    The publisher is bus-native: it needs exactly one string constant and must
    run in the minimal CI env the pr-merged-publisher workflow provisions
    (confluent-kafka + click + pydantic + pyyaml only). Importing
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

# Flush window for the produce->ack round trip, in seconds. Deliberately far
# below librdkafka's default per-message timeout (message.timeout.ms = 300000)
# so an unreachable broker surfaces as a non-zero flush remainder here rather
# than hanging the job until the GHA step timeout.
_FLUSH_TIMEOUT_SECONDS = 30.0


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
    otherwise (the dev-lane Redpanda broker, which has no SASL). The broker
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
) -> tuple[str, int, int]:
    """Publish onex.evt.github.pr-merged.v1 to Kafka.

    Returns ``(event_id, partition, offset)`` — the broker-assigned coordinates
    of the delivered message, which the caller records as the publish receipt.
    Raises ``RuntimeError`` on any delivery failure, including a flush that
    leaves the message undelivered. This function NEVER returns on a message
    that did not reach the broker (OMN-14639 / OMN-17378).
    """
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
    delivered: tuple[int, int] | None = None

    def _on_delivery(err: object, msg: object) -> None:
        nonlocal delivery_error, delivered
        if err is not None:
            delivery_error = RuntimeError(str(err))
            return
        delivered = (int(msg.partition()), int(msg.offset()))  # type: ignore[attr-defined]

    message = json.dumps(payload, default=str).encode("utf-8")
    key = f"pr-merged/{repo}/{pr_number}".encode()

    producer.produce(
        topic=TOPIC,
        key=key,
        value=message,
        on_delivery=_on_delivery,
    )

    # OMN-17378 / OMN-14639: flush() returns the number of messages STILL in the
    # producer queue when the timeout elapses. A broker that cannot be resolved
    # or refuses the connection leaves the message queued and unacked;
    # librdkafka's per-message delivery timeout (message.timeout.ms, default
    # 300000ms) is far larger than this flush window, so `_on_delivery` never
    # fires and `delivery_error` stays None. The old code DISCARDED this return
    # value and therefore printed "Published ..." on an UNDELIVERED event — the
    # exact green-but-silent failure this publisher exhibited from 2026-08-27 to
    # 2026-08-31. A non-zero remainder is a hard delivery failure.
    remaining = producer.flush(timeout=_FLUSH_TIMEOUT_SECONDS)

    if delivery_error is not None:
        raise RuntimeError(f"Kafka delivery failed: {delivery_error}") from None

    if remaining:
        raise RuntimeError(
            f"Kafka delivery timed out: {remaining} message(s) still undelivered "
            f"to {bootstrap_servers} after a {_FLUSH_TIMEOUT_SECONDS:.0f}s flush "
            "(broker unreachable / connection refused / name not resolvable). "
            "Refusing to report success on an unpublished pr-merged event "
            "(OMN-17378)."
        )

    if delivered is None:
        # flush() drained the queue but no delivery callback ever ran. There is
        # no broker-assigned offset, so there is no proof of publication, so
        # this is not a success. Never synthesise coordinates.
        raise RuntimeError(
            "Kafka reported no delivery callback for the pr-merged event even "
            f"though the producer queue drained (broker {bootstrap_servers}). "
            "No broker-assigned partition/offset means no proof of publication; "
            "refusing to report success (OMN-17378)."
        )

    partition, offset = delivered
    return event_id, partition, offset


def _write_publish_receipt(
    event_id: str,
    partition: int,
    offset: int,
    broker: str,
    repo: str,
    pr_number: int,
    lane: str | None,
) -> None:
    """Append the publish receipt to $GITHUB_STEP_SUMMARY (OMN-17378 item 3).

    Makes "did it actually publish" answerable from the run page alone. The
    broker is the committed, overlay-declared endpoint (config, not secret), so
    printing it leaks nothing.
    """
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not summary_path:
        return
    lines = [
        "### pr-merged publish receipt",
        "",
        f"- **topic**: `{TOPIC}`",
        f"- **lane**: `{lane}`",
        f"- **broker**: `{broker}`",
        f"- **repo / PR**: `{repo}` #{pr_number}",
        f"- **event_id**: `{event_id}`",
        f"- **partition / offset**: `{partition}` / `{offset}`",
        "",
        f"Broker-acked at offset {offset}. Verify: "
        f"`rpk topic consume {TOPIC} -o {offset} -n 1`",
        "",
    ]
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


@click.command()
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print payload without publishing to Kafka",
)
@click.option(
    "--lane",
    default=None,
    help=(
        "CI bus lane id (e.g. 'dev') resolved against config/ci_bus_lanes.yaml "
        "to decide the bus target and to fail-loud-check any injected "
        "KAFKA_BOOTSTRAP_SERVERS against the lane's declared broker (OMN-14801). "
        "Required on the trusted self-hosted runner."
    ),
)
def main(dry_run: bool, lane: str | None) -> None:
    """Publish onex.evt.github.pr-merged.v1 for a merged PR.

    All inputs are read from environment variables injected by the GHA workflow:
    PR_REPO, PR_BRANCH, PR_NUMBER, PR_MERGED_AT, PR_TICKET (optional), plus
    RUNNER_IS_TRUSTED. The bus target is resolved from ``--lane`` against
    config/ci_bus_lanes.yaml.
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
        f"ticket={ticket!r} merged_at={merged_at} lane={lane!r}"
    )

    if dry_run:
        click.echo("(dry-run: skipping Kafka publish)")
        click.echo(json.dumps(payload, indent=2))
        sys.exit(0)

    bootstrap_servers = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "").strip()
    username = os.environ.get("KAFKA_SASL_USERNAME", "")
    password = os.environ.get("KAFKA_SASL_PASSWORD", "")

    # Read once; a wiring gap (RUNNER_IS_TRUSTED unset/invalid) fails fast here
    # rather than silently choosing the permissive branch (OMN-14451).
    trusted = is_trusted_runner()

    overlay = load_lane_overlay()
    mode, declared_broker = resolve_lane_broker(overlay, lane)

    def _publish_or_die(target_broker: str) -> None:
        try:
            published_id, partition, offset = publish_pr_merged_event(
                bootstrap_servers=target_broker,
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
        click.echo(
            f"Published {TOPIC} event_id={published_id} "
            f"partition={partition} offset={offset}"
        )
        _write_publish_receipt(
            event_id=published_id,
            partition=partition,
            offset=offset,
            broker=target_broker,
            repo=repo,
            pr_number=pr_number,
            lane=lane,
        )

    # --- OMN-14801/OMN-17378: overlay-driven lane -> bus-target resolution -----
    # The lane overlay (config/ci_bus_lanes.yaml) is the checked-in, reviewable
    # truth. On the trusted runner an unresolvable/undeclared lane is a wiring
    # gap and fails loud; on the fork/hosted fallback it degrades to a graceful
    # no-op skip, which is the ONLY path on which a skip is correct.
    if mode in (MODE_NO_LANE, MODE_UNKNOWN_LANE):
        if trusted:
            lanes_obj = overlay.get("lanes")
            declared = sorted(lanes_obj) if isinstance(lanes_obj, dict) else []
            reason = (
                "--lane was not supplied"
                if mode == MODE_NO_LANE
                else f"lane {lane!r} is not declared"
            )
            click.echo(
                f"ERROR: {reason} on the TRUSTED self-hosted runner for a merged "
                "PR. The publishing lane MUST resolve to a declared bus lane in "
                f"config/{LANE_OVERLAY_PATH.name} (declared lanes: {declared}). "
                "Refusing to guess the bus lane / silently no-op (OMN-17378).",
                err=True,
            )
            sys.exit(1)
        click.echo(
            f"WARNING: no bus lane resolved (mode={mode}, lane={lane!r}) -- "
            "skipping pr-merged publish (expected on a fork/hosted runner). "
            "Exiting 0.",
            err=True,
        )
        sys.exit(0)

    if mode == MODE_INMEMORY:
        # Contract-as-data default: the in-memory bus reaches no cross-process
        # consumer from a throwaway CI process, so publishing is a no-op. Loud,
        # visible, exit 0.
        click.echo(
            f"lane={lane!r} declares the in-memory bus (config-as-data default "
            f"in config/{LANE_OVERLAY_PATH.name}): no cross-process broker to "
            "publish to. Skipping cross-process publish (no-op). Exiting 0."
        )
        sys.exit(0)

    if mode == MODE_FROM_SECRET:
        # The injected secret IS the broker and there is no independent overlay
        # truth to check it against.
        if not bootstrap_servers:
            if trusted:
                click.echo(
                    "ERROR: KAFKA_BOOTSTRAP_SERVERS is not set on the TRUSTED "
                    f"self-hosted runner for lane={lane!r} (declared "
                    "'from-secret'). The broker MUST be resolvable here. Failing "
                    "loudly instead of silently skipping (OMN-17378).",
                    err=True,
                )
                sys.exit(1)
            click.echo(
                "WARNING: KAFKA_BOOTSTRAP_SERVERS is not set -- skipping "
                "pr-merged publish (expected on a fork/hosted runner with no "
                "broker provisioned). Exiting 0.",
                err=True,
            )
            sys.exit(0)
        _publish_or_die(bootstrap_servers)
        return

    # mode == MODE_CONCRETE: the overlay declares an authoritative host:port for
    # this lane. Prefer the committed value AND fail-loud-check any injected
    # secret against it (the OMN-14800 silent-drift guard).
    if not bootstrap_servers:
        if trusted:
            click.echo(
                "KAFKA_BOOTSTRAP_SERVERS is not injected; publishing to the "
                f"overlay-declared broker for lane={lane!r}: {declared_broker} "
                f"(config/{LANE_OVERLAY_PATH.name})."
            )
            _publish_or_die(declared_broker)
            return
        click.echo(
            "WARNING: KAFKA_BOOTSTRAP_SERVERS is not set on a fork/hosted runner "
            f"-- skipping pr-merged publish for lane={lane!r}. Exiting 0.",
            err=True,
        )
        sys.exit(0)

    if bootstrap_servers != declared_broker:
        if trusted:
            # A silently-repointed secret publishing to an undeclared broker. The
            # injected value is GH-masked in logs; only the committed
            # (non-secret) declared value is printed.
            click.echo(
                "ERROR: LANE BUS DRIFT -- the injected KAFKA_BOOTSTRAP_SERVERS "
                "(GH-masked in logs) does not match the broker declared for "
                f"lane={lane!r} in config/{LANE_OVERLAY_PATH.name} (declared: "
                f"{declared_broker}). This is the silent dev->stability repoint "
                "class (OMN-14800): refusing to publish the pr-merged event to "
                "an undeclared broker. Reconcile the injected secret with the "
                "checked-in lane overlay (OMN-14801).",
                err=True,
            )
            sys.exit(1)
        click.echo(
            "WARNING: injected KAFKA_BOOTSTRAP_SERVERS diverges from the "
            f"overlay-declared broker for lane={lane!r} on a fork/hosted runner "
            "-- skipping pr-merged publish. Exiting 0.",
            err=True,
        )
        sys.exit(0)

    # Injected secret agrees with the overlay-declared broker: prefer the
    # committed overlay value (identical to the secret) and publish.
    _publish_or_die(declared_broker)


if __name__ == "__main__":
    main()
