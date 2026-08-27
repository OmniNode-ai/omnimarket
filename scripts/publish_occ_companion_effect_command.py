#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# publish_occ_companion_effect_command.py
#
# Thin-publishes onex.cmd.omnimarket.occ-companion-effect-requested.v1 when a
# product PR is opened or synchronized (OMN-14941 born path). The command is
# consumed by node_occ_companion_effect (the canonical RSD-3 write-EFFECT),
# which runs the deterministic read -> compute -> write OCC-companion producer
# cycle and stamps Evidence-Source: OCC#<n> back onto the product PR. Only this
# path mints occ:machine-minted + minted_by_node=true.
#
# Payload (ModelOccCompanionEffectRequest-shaped so payload_type_match routes
# it — the OMN-13990 lesson): {repo, pr_number, mode, correlation_id} and
# NOTHING else. The model is frozen + extra='forbid'; any stray key (the legacy
# occ-autobind block_reason/ticket_id/requested_at/pr_head_sha/event_id/topic
# shape) fails validation and the command is silently DLQ'd — the seam test
# (tests/unit/nodes/node_occ_companion_effect/
# test_publish_occ_companion_effect_command.py) round-trips this publisher's
# emitted JSON through ModelOccCompanionEffectRequest.model_validate to pin it.
#
# mode MUST be "mutate": ModelOccCompanionEffectRequest defaults mode to
# dry_run (fail-safe). An omitted mode would make every published command a
# silent read+compute no-mint — the exact optional-input-silent-skip trap
# (memory feedback_optional_input_means_the_check_does_not_exist). This
# publisher IS the live trigger, so it sets mode="mutate" explicitly, always.
#
# occ_repo / runner / verifier are intentionally OMITTED so the model defaults
# apply (runner=node_occ_companion_compute != verifier=occ-evidence-source-
# autobind, OMN-12791).
#
# PUBLISHER-SIDE IDEMPOTENCY (OMN-14941, line-anchored per OMN-16710): when the
# product PR body already carries a real "Evidence-Source: OCC#<n>" LINE, the
# companion is already bound — publishing would only burn a lease + a full
# read/compute cycle to reach the handler's own already-bound no-op. The
# publisher skips LOUDLY (exit 0) instead. The check is a line-anchored mirror
# of the canonical compat parser, NOT a substring scan: a substring scan let a
# PR body that merely *mentions* the stamp in prose suppress its own companion
# and still report SUCCESS. See product_pr_occ_binding below.
#
# BROKER / LANE RESOLUTION: identical to publish_occ_autobind_command.py
# (OMN-14801 overlay-driven lane resolution, OMN-14813 secret-free concrete dev
# broker, OMN-14639 fail-loud flush, OMN-14451 trusted-runner fail-closed).
# The helpers are REUSED from that script by file path — single source of
# truth, no second copy of the lane-resolution logic to drift.
#
# Ticket: OMN-14941
#
# Required environment variables (when not --dry-run):
#   PR_REPO                   -- repository slug, e.g. OmniNode-ai/omnibase_infra
#   PR_NUMBER                 -- PR number as a string (cast to int here — GHA
#                                env is always a string)
#   PR_HEAD_SHA               -- product PR head commit SHA (log/forensics only;
#                                NOT carried on the wire — the RSD-2 read
#                                re-resolves live state)
#
# Optional:
#   PR_BODY                   -- product PR body; when it already carries a
#                                line-anchored "Evidence-Source: OCC#<n>" stamp
#                                the publish is skipped loudly (already bound).
#                                A prose mention of that literal is NOT a stamp
#                                and does NOT skip (OMN-16710).
#   KAFKA_BOOTSTRAP_SERVERS   -- injected broker (fail-loud-checked against the
#                                overlay-declared lane broker when both exist)
#   KAFKA_SASL_USERNAME       -- SASL username / API key (cloud broker only)
#   KAFKA_SASL_PASSWORD       -- SASL password / API secret (cloud broker only)
#
# Usage:
#   python scripts/publish_occ_companion_effect_command.py --lane dev [--dry-run]

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import uuid
from pathlib import Path
from types import ModuleType

import click


def _load_sibling(module_name: str, filename: str) -> ModuleType:
    """Load a sibling module by file path (never import the omnimarket package).

    This thin GHA script runs on a sparse checkout with only minimal deps
    installed; importing ``omnimarket.*`` would execute the package __init__ and
    pull in the full product dependency graph. Mirrors the file-path loading
    pattern established by publish_occ_autobind_command.py.
    """
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {filename} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Single source of truth for the OMN-14801/OMN-14813 lane-resolution +
# trusted-runner + producer-transport helpers: reuse the autobind publisher's
# implementations rather than committing a second copy that can drift.
_AUTOBIND = _load_sibling(
    "publish_occ_autobind_command_for_companion_effect",
    "publish_occ_autobind_command.py",
)

# SLF001 noqa'd deliberately: these are sibling-script internals reused as the
# single source of truth (both scripts are covered by the same unit suites);
# duplicating them here is the drift vector OMN-14801 exists to close.
_load_lane_overlay = _AUTOBIND._load_lane_overlay  # noqa: SLF001
_resolve_lane_broker = _AUTOBIND._resolve_lane_broker  # noqa: SLF001
_is_trusted_runner = _AUTOBIND._is_trusted_runner  # noqa: SLF001
_kafka_producer_config = _AUTOBIND._kafka_producer_config  # noqa: SLF001
_LANE_OVERLAY_PATH = _AUTOBIND._LANE_OVERLAY_PATH  # noqa: SLF001
_MODE_NO_LANE = _AUTOBIND._MODE_NO_LANE  # noqa: SLF001
_MODE_UNKNOWN_LANE = _AUTOBIND._MODE_UNKNOWN_LANE  # noqa: SLF001
_MODE_INMEMORY = _AUTOBIND._MODE_INMEMORY  # noqa: SLF001
_MODE_FROM_SECRET = _AUTOBIND._MODE_FROM_SECRET  # noqa: SLF001
_MODE_CONCRETE = _AUTOBIND._MODE_CONCRETE  # noqa: SLF001

# Canonical topic constant (single source of truth in omnimarket.events.topics,
# loaded by file path for the same minimal-deps reason).
_TOPICS_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "events" / "topics.py"
)
_TOPICS_SPEC = importlib.util.spec_from_file_location(
    "omnimarket_events_topics_for_occ_companion_effect", _TOPICS_PATH
)
if _TOPICS_SPEC is None or _TOPICS_SPEC.loader is None:
    raise RuntimeError(f"Could not load topic registry from {_TOPICS_PATH}")
_TOPICS_MODULE = importlib.util.module_from_spec(_TOPICS_SPEC)
_TOPICS_SPEC.loader.exec_module(_TOPICS_MODULE)

TOPIC = _TOPICS_MODULE.OCC_COMPANION_EFFECT_COMMAND_TOPIC_V1

# The stamp the canonical producer writes onto a bound product PR body. When a
# real stamp LINE is present, the companion already exists — publishing again is
# pure waste.
#
# OMN-16710: this used to be a bare substring test —
# ``"Evidence-Source: OCC#" in pr_body`` — which matched the literal ANYWHERE,
# including prose that merely discusses OCC evidence. Tripping it is silent
# green on a merge-gating path: nothing is published, no companion is minted,
# no stamp lands, and the job still exits 0. The product PR then hard-fails the
# `verify / verify` receipt gate (OMN-10419) for a stamp that was never minted,
# while the job responsible for minting it reports SUCCESS. Observed live on
# omnimemory#450 during the OMN-16708 canary — a docs PR *about* OCC publisher
# routing suppressed its own companion. Same silent-green shape OMN-14639 /
# OMN-14451 hardened the OTHER branch of this publisher against (the flush
# branch, which now refuses to report success on an undelivered command).
#
# The predicate below is a deliberate line-for-line MIRROR of the authoritative
# parser, ``omnibase_compat.contracts.pr_occ_stamp.pr_occ_metadata_stamp``
# (``_parse_evidence_source`` + ``occ_stamp_authoring.product_pr_occ_binding``)
# — same regex, same IGNORECASE, same per-line ``strip()``/``fullmatch``, same
# "first Evidence-Source line decides" rule. It is copied rather than imported
# because this thin GHA script must not pull in the package dependency graph
# (see ``_load_sibling``). The copy is pinned against the real parser by
# tests/unit/nodes/node_occ_companion_effect/
# test_publish_occ_companion_effect_command.py::
# TestIdempotencyAgreesWithCanonicalParser — a publisher that decides
# "already bound" on different evidence than the handler that does the minting
# is the same defect class as the substring match itself.
_EVIDENCE_SOURCE_PREFIX = "evidence-source:"
_EVIDENCE_SOURCE_OCC_PR_RE = re.compile(
    r"^Evidence-Source:\s+OCC#(\d+)\s*$",
    re.IGNORECASE,
)


def product_pr_occ_binding(pr_body: str) -> int | None:
    """Return the bound OCC PR number, or ``None`` when the PR is not bound.

    Mirrors the canonical compat parser exactly:

    * only a LINE whose stripped form is entirely ``Evidence-Source: OCC#<n>``
      counts — a mid-line mention, a ``> ``-quoted line, a list item, or a
      ``OCC#<n>`` placeholder with no digits does not;
    * the FIRST line beginning ``Evidence-Source:`` decides. If it is a bare
      commit SHA (the unbound state this effect exists to repair) the answer is
      ``None`` even when a later line names an OCC PR.

    Residual, stated rather than silently inherited: a stamp-shaped line inside
    a fenced code block still counts as a binding. That is a property of the
    canonical parser, not of this copy — diverging here would put the publisher
    and the handler on different evidence, which is strictly worse than the
    shared blind spot.
    """
    for line in pr_body.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith(_EVIDENCE_SOURCE_PREFIX):
            continue
        match = _EVIDENCE_SOURCE_OCC_PR_RE.fullmatch(stripped)
        return int(match.group(1)) if match is not None else None
    return None


# Wire literal for the command mode. MUST equal the "mutate" member of
# ModelOccCompanionEffectRequest.mode — asserted by the seam test rather than
# imported here (importing the node's model package into this thin GHA-runner
# script would pull in far more than the minimal deps the workflow installs).
_MODE_MUTATE = "mutate"


def build_payload(
    repo: str,
    pr_number: int,
    correlation_id: str,
    allow_merged_replay: bool = False,
) -> dict[str, object]:
    """Return a companion-effect command payload shaped as ModelOccCompanionEffectRequest.

    The runtime consumes this off
    ``onex.cmd.omnimarket.occ-companion-effect-requested.v1`` and the contract's
    routing validates it against ``ModelOccCompanionEffectRequest``
    (``frozen=True, extra='forbid'``) before dispatching to
    ``HandlerOccCompanionEffect``. The keys here MUST be exactly (a subset of)
    the command model's fields — the occ-autobind born-path bug was a payload
    that never matched its consumer model, so the command was silently DLQ'd and
    the effect never fired (OMN-13990). Specifically:

    * ``mode`` is ALWAYS ``"mutate"``: the model defaults to ``dry_run``
      (fail-safe), so omitting it would silently never mint (the
      optional-input-silent-skip trap).
    * ``pr_number`` is an ``int`` (cast from the GHA string env by the caller).
    * ``occ_repo``/``runner``/``verifier`` are omitted so the model defaults
      apply (runner != verifier, OMN-12791).
    * ``allow_merged_replay`` (OMN-16665) is emitted ONLY when True. It is the
      deliberately-scoped F-17 override that authors the companion for a PR
      which MERGED without one — the merge/queue-latency race in which this
      publisher's job is held long enough that the PR merges before the command
      reaches compute (live: omnimemory#447, published ~90s after the merge).
      Emitting the key unconditionally would be harmless to the model but would
      put a False in every born-path payload; omitting it keeps the born-path
      wire shape byte-identical to pre-16665.
    * NO legacy occ-autobind fields (block_reason/ticket_id/requested_at/
      pr_head_sha/event_id/topic) — ``extra='forbid'`` rejects every one.
    """
    payload: dict[str, object] = {
        "repo": repo,
        "pr_number": pr_number,
        "mode": _MODE_MUTATE,
        "correlation_id": correlation_id,
    }
    if allow_merged_replay:
        payload["allow_merged_replay"] = True
    return payload


def publish_occ_companion_effect_command(
    bootstrap_servers: str,
    username: str,
    password: str,
    repo: str,
    pr_number: int,
) -> str:
    """Publish the companion-effect command to Kafka. Returns the correlation_id."""
    from confluent_kafka import Producer  # type: ignore[import-untyped,unused-ignore]

    correlation_id = str(uuid.uuid4())
    payload = build_payload(
        repo=repo,
        pr_number=pr_number,
        correlation_id=correlation_id,
    )

    producer = Producer(_kafka_producer_config(bootstrap_servers, username, password))

    delivery_error: BaseException | None = None

    def _on_delivery(err: object, _msg: object) -> None:
        nonlocal delivery_error
        if err is not None:
            delivery_error = RuntimeError(str(err))

    message = json.dumps(payload, default=str).encode("utf-8")
    key = f"occ-companion-effect/{repo}/{pr_number}".encode()

    producer.produce(
        topic=TOPIC,
        key=key,
        value=message,
        on_delivery=_on_delivery,
    )
    # OMN-14639: flush() returns the number of messages STILL in the producer
    # queue when the timeout elapses. A broker that refuses the connection
    # leaves the message queued and unacked; librdkafka's per-message delivery
    # timeout is far larger than this 30s flush window, so `_on_delivery` never
    # fires and `delivery_error` stays None. A non-zero remaining count means
    # the command did NOT reach the broker — a hard delivery failure, never
    # success (the "runs green while publishing nothing" class).
    remaining = producer.flush(timeout=30)

    if delivery_error is not None:
        raise RuntimeError(f"Kafka delivery failed: {delivery_error}") from None

    if remaining and remaining > 0:
        raise RuntimeError(
            f"Kafka delivery timed out: {remaining} message(s) still undelivered "
            f"to {bootstrap_servers} after a 30s flush (broker unreachable / "
            "connection refused). Refusing to report success on an undelivered "
            "occ-companion-effect command (OMN-14639)."
        )

    return correlation_id


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
        "Required on the trusted self-hosted runner for an authoring PR."
    ),
)
def main(dry_run: bool, lane: str | None) -> None:
    """Publish onex.cmd.omnimarket.occ-companion-effect-requested.v1 for a product PR.

    All inputs are read from environment variables injected by the GHA workflow:
    PR_REPO, PR_NUMBER, PR_HEAD_SHA, PR_BODY (optional, idempotency skip),
    ALLOW_MERGED_REPLAY (optional, OMN-16665 merged-PR recovery). The bus target
    is resolved from ``--lane`` against config/ci_bus_lanes.yaml.
    """
    repo = os.environ.get("PR_REPO", "")
    pr_number_str = os.environ.get("PR_NUMBER", "")
    pr_head_sha = os.environ.get("PR_HEAD_SHA", "")
    pr_body = os.environ.get("PR_BODY", "")
    # OMN-16665: only the exact literal "true" enables the override. A loose
    # truthiness read ("false" is a non-empty string) would silently arm the
    # F-17 override on every born-path publish that set the variable at all.
    allow_merged_replay = (
        os.environ.get("ALLOW_MERGED_REPLAY", "").strip().lower() == "true"
    )

    if not repo or not pr_number_str or not pr_head_sha:
        click.echo(
            "ERROR: PR_REPO, PR_NUMBER, and PR_HEAD_SHA must all be set",
            err=True,
        )
        sys.exit(1)

    try:
        # GHA env values are ALWAYS strings; the consumer model requires int.
        pr_number = int(pr_number_str)
    except ValueError:
        click.echo(
            f"ERROR: PR_NUMBER must be an integer, got: {pr_number_str!r}", err=True
        )
        sys.exit(1)

    # Publisher-side idempotency (OMN-14941): an already-bound product PR needs
    # no companion — skip loudly, exit 0. This is a cheap pre-filter; the
    # handler's compute no-ops on the same condition as the authoritative check.
    # OMN-16710: keyed on a real line-anchored stamp, never a substring hit —
    # see product_pr_occ_binding.
    bound_occ_pr = product_pr_occ_binding(pr_body)
    if bound_occ_pr is not None:
        click.echo(
            f"SKIP: {repo}#{pr_number} body already carries an "
            f"'Evidence-Source: OCC#{bound_occ_pr}' line — companion already "
            "bound; not publishing a redundant occ-companion-effect command "
            "(publisher-side idempotency, OMN-14941; line-anchored per "
            "OMN-16710). Exiting 0."
        )
        sys.exit(0)

    correlation_id = str(uuid.uuid4())
    payload = build_payload(
        repo=repo,
        pr_number=pr_number,
        correlation_id=correlation_id,
        allow_merged_replay=allow_merged_replay,
    )

    click.echo(
        f"occ-companion-effect command: repo={repo} pr={pr_number} "
        f"head={pr_head_sha} lane={lane!r} "
        f"allow_merged_replay={allow_merged_replay}"
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
    trusted = _is_trusted_runner()

    overlay = _load_lane_overlay()
    mode, declared_broker = _resolve_lane_broker(overlay, lane)

    def _publish_or_die(target_broker: str) -> None:
        try:
            published_id = publish_occ_companion_effect_command(
                bootstrap_servers=target_broker,
                username=username,
                password=password,
                repo=repo,
                pr_number=pr_number,
            )
        except Exception as exc:
            click.echo(f"Delivery error: {exc}", err=True)
            sys.exit(1)
        click.echo(f"Published {TOPIC} event_id={published_id}")

    # --- OMN-14801: overlay-driven lane -> bus-target resolution (same branch
    # semantics as publish_occ_autobind_command.py; helpers reused above) ------
    if mode in (_MODE_NO_LANE, _MODE_UNKNOWN_LANE):
        if trusted:
            lanes_obj = overlay.get("lanes")
            declared = sorted(lanes_obj) if isinstance(lanes_obj, dict) else []
            reason = (
                "--lane was not supplied"
                if mode == _MODE_NO_LANE
                else f"lane {lane!r} is not declared"
            )
            click.echo(
                f"ERROR: {reason} on the TRUSTED self-hosted runner for an "
                "occ-companion-effect-eligible PR. The publishing lane MUST "
                "resolve to a declared bus lane in "
                f"config/{_LANE_OVERLAY_PATH.name} (declared lanes: {declared}). "
                "Refusing to guess the bus lane / silently no-op (OMN-14801).",
                err=True,
            )
            sys.exit(1)
        click.echo(
            f"WARNING: no bus lane resolved (mode={mode}, lane={lane!r}) -- "
            "skipping occ-companion-effect publish (expected on a fork/cloud "
            "runner). Exiting 0.",
            err=True,
        )
        sys.exit(0)

    if mode == _MODE_INMEMORY:
        click.echo(
            f"lane={lane!r} declares the in-memory bus (config-as-data default "
            f"in config/{_LANE_OVERLAY_PATH.name}): no cross-process broker to "
            "publish to. Skipping cross-process publish (no-op). Exiting 0."
        )
        sys.exit(0)

    if mode == _MODE_FROM_SECRET:
        if not bootstrap_servers:
            if trusted:
                click.echo(
                    "ERROR: KAFKA_BOOTSTRAP_SERVERS is not set on the TRUSTED "
                    f"self-hosted runner for lane={lane!r} (declared "
                    "'from-secret'). The broker MUST be resolvable here. Failing "
                    "loudly instead of silently skipping (OMN-14451).",
                    err=True,
                )
                sys.exit(1)
            click.echo(
                "WARNING: KAFKA_BOOTSTRAP_SERVERS is not set -- skipping "
                "occ-companion-effect publish (expected on a fork/cloud runner "
                "with no broker provisioned). Exiting 0.",
                err=True,
            )
            sys.exit(0)
        _publish_or_die(bootstrap_servers)
        return

    # mode == _MODE_CONCRETE: the overlay declares an authoritative host:port
    # for this lane. Prefer the committed value AND fail-loud-check any injected
    # secret against it (the OMN-14800 silent-drift guard). The dev lane is
    # secret-free (OMN-14813): normally no KAFKA_BOOTSTRAP_SERVERS is injected
    # at all and the committed overlay value is used directly.
    if not bootstrap_servers:
        if trusted:
            click.echo(
                "KAFKA_BOOTSTRAP_SERVERS is not injected; publishing to the "
                f"overlay-declared broker for lane={lane!r}: {declared_broker} "
                f"(config/{_LANE_OVERLAY_PATH.name})."
            )
            _publish_or_die(declared_broker)
            return
        click.echo(
            "WARNING: KAFKA_BOOTSTRAP_SERVERS is not set on a fork/cloud runner "
            f"-- skipping occ-companion-effect publish for lane={lane!r}. "
            "Exiting 0.",
            err=True,
        )
        sys.exit(0)

    if bootstrap_servers != declared_broker:
        if trusted:
            click.echo(
                "ERROR: LANE BUS DRIFT -- the injected KAFKA_BOOTSTRAP_SERVERS "
                "(GH-masked in logs) does not match the broker declared for "
                f"lane={lane!r} in config/{_LANE_OVERLAY_PATH.name} (declared: "
                f"{declared_broker}). This is the silent dev->stability repoint "
                "class (OMN-14800): refusing to publish the occ-companion-effect "
                "command to an undeclared broker. Reconcile the injected secret "
                "with the checked-in lane overlay (OMN-14801).",
                err=True,
            )
            sys.exit(1)
        click.echo(
            "WARNING: injected KAFKA_BOOTSTRAP_SERVERS diverges from the "
            f"overlay-declared broker for lane={lane!r} on a fork/cloud runner "
            "-- skipping occ-companion-effect publish. Exiting 0.",
            err=True,
        )
        sys.exit(0)

    _publish_or_die(declared_broker)


if __name__ == "__main__":
    main()
