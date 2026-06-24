# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file-internal-ip OMN-13552 reason="docstring documents the .201 runtime-lane host this collector probes over SSH; lane host is resolved by lane_target, not hardcoded here as a connection string"
"""LiveMetadataCollector — shell-out collection phase for node_data_flow_sweep.

Runs rpk topic/group describe and psql row-count checks to populate
ModelFlowInput objects from live infrastructure. The handler itself remains
pure compute; all side-effectful I/O lives here.

OMN-13552: probes target a resolved *lane* (``ModelLaneTarget``) rather than
"whatever host this process runs on". A lane resolving to a remote runtime host
(the dev/stability/prod/judge lanes on ``192.168.86.201``) is probed via
``ssh <user>@<host> docker exec <container> ...`` — the same SSH + ``docker
exec`` transport already proven in ``node_integration_sweep_orchestrator``
(OMN-7238 class fix). A lane with an empty ``runtime_host`` (``local``) is probed
via local ``docker exec`` / ``psql`` exactly as before.

Before collecting, the collector runs a reachability preflight against the
target lane's broker. If the lane is unreachable/indeterminate it raises
:class:`LaneUnreachableError` so the caller fails LOUD (status = error/UNKNOWN)
instead of mislabelling every flow PRODUCER_DOWN for a lane that was never
actually probed.

This module is intentionally NOT imported by the handler or any test that
exercises pure classification logic. It is only imported by __main__.py when
the --collect flag is set.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import os
import re
import shlex
import subprocess
from typing import Final

from omnimarket.nodes.node_data_flow_sweep.handlers.handler_data_flow_sweep import (
    EnumProducerStatus,
    ModelFlowInput,
)
from omnimarket.nodes.node_data_flow_sweep.lane_target import ModelLaneTarget

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants resolved from env (never hardcoded)
# ---------------------------------------------------------------------------

_STALE_THRESHOLD_SECONDS: Final[float] = float(
    os.environ.get("ONEX_FLOW_STALE_THRESHOLD_SECONDS", "1800")
)  # fallback-ok: 30m is a safe operational default; overrideable via env


class LaneUnreachableError(RuntimeError):
    """Raised when the target lane's broker is unreachable/indeterminate.

    OMN-13552 DoD: an unreachable target lane must surface as an explicit
    error/UNKNOWN verdict, never a false PRODUCER_DOWN-as-broken or false-clean.
    The collector raises this instead of returning MISSING-for-everything so the
    caller can emit ``status = error`` and a non-zero exit.
    """


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------


def _docker_exec(
    target: ModelLaneTarget,
    container: str,
    inner_argv: list[str],
    *,
    timeout: int = 10,
) -> tuple[int, str]:
    """Run ``docker exec <container> <inner_argv>`` on the target lane.

    Local lane: ``docker exec ...`` directly. Remote lane: the whole
    ``docker exec`` command is shell-quoted and run via ``ssh`` so it executes on
    the lane's host. Returns ``(returncode, combined stdout+stderr)``; never
    raises on a missing binary or timeout (returns 127 / 124).
    """
    docker_argv = ["docker", "exec", container, *inner_argv]
    if target.is_remote:
        remote_command = shlex.join(docker_argv)
        argv = ["ssh", f"{target.ssh_user}@{target.runtime_host}", remote_command]
    else:
        argv = docker_argv
    return _run_argv(argv, timeout=timeout)


def _run_argv(argv: list[str], *, timeout: int) -> tuple[int, str]:
    """Run an argv list; return (returncode, combined stdout+stderr)."""
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout + result.stderr
    except FileNotFoundError as exc:
        _log.debug("command not found: %s — %s", argv[0] if argv else "?", exc)
        return 127, str(exc)
    except subprocess.TimeoutExpired:
        _log.debug("command timed out: %s", " ".join(argv))
        return 124, "timeout"


# ---------------------------------------------------------------------------
# Reachability preflight
# ---------------------------------------------------------------------------


def assert_lane_reachable(target: ModelLaneTarget) -> None:
    """Fail LOUD when the target lane's broker is unreachable/indeterminate.

    Runs ``rpk cluster info`` inside the lane's Redpanda container (over SSH for
    a remote lane). A non-zero return means the lane was never actually probed —
    no local container, SSH unreachable, or the broker is down — so we raise
    :class:`LaneUnreachableError` rather than letting the per-topic probes report
    every flow MISSING/PRODUCER_DOWN against a host that does not run the lane.
    """
    code, out = _docker_exec(
        target, target.redpanda_container, ["rpk", "cluster", "info"], timeout=20
    )
    if code == 0:
        return
    transport = (
        f"ssh {target.ssh_user}@{target.runtime_host} "
        if target.is_remote
        else "local "
    )
    raise LaneUnreachableError(
        f"lane {target.lane!r} broker unreachable via {transport}"
        f"docker exec {target.redpanda_container} rpk cluster info "
        f"(rc={code}): {out.strip()[:300]}"
    )


# ---------------------------------------------------------------------------
# Per-dimension probes
# ---------------------------------------------------------------------------


def probe_producer_status(
    target: ModelLaneTarget, topic: str
) -> tuple[EnumProducerStatus, float | None]:
    """Return (producer_status, newest_message_age_seconds) for the lane's topic.

    Runs ``rpk topic describe`` inside the lane's Redpanda container.
    newest_message_age_seconds is None when it cannot be determined.
    """
    code, out = _docker_exec(
        target,
        target.redpanda_container,
        ["rpk", "topic", "describe", topic, "--print-partitions"],
        timeout=20,
    )

    if code == 0 and ("not found" in out.lower() or "does not exist" in out.lower()):
        return EnumProducerStatus.MISSING, None

    if code == 0 and "PARTITION" in out:
        # Topic exists. ACTIVE when any partition has a non-zero high watermark,
        # else EMPTY (topic present but no messages produced yet).
        if _has_messages(out):
            age = _probe_newest_message_age(target, topic)
            return EnumProducerStatus.ACTIVE, age
        return EnumProducerStatus.EMPTY, None

    # Could not describe the topic on a reachable lane — treat as MISSING.
    _log.debug(
        "cannot determine producer status for %s on lane %s (rc=%d)",
        topic,
        target.lane,
        code,
    )
    return EnumProducerStatus.MISSING, None


def _has_messages(describe_output: str) -> bool:
    """True when ``rpk topic describe --print-partitions`` shows a HW >= 1.

    Output rows look like::

        PARTITION  LEADER  EPOCH  REPLICAS  LOG-START-OFFSET  HIGH-WATERMARK
        0          1       0      [1]       0                 16

    The high watermark is the last column; any partition with HW >= 1 means the
    topic has had at least one message produced.
    """
    for line in describe_output.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        if parts[0].upper() == "PARTITION":
            continue
        if not parts[0].isdigit():
            continue
        try:
            high_watermark = int(parts[-1])
        except ValueError:
            continue
        if high_watermark >= 1:
            return True
    return False


def _probe_newest_message_age(target: ModelLaneTarget, topic: str) -> float | None:
    """Return age in seconds of the newest message in topic, or None.

    Uses ``rpk topic consume`` inside the lane's Redpanda container so the
    timestamp probe rides the same lane transport as every other probe (no host
    kcat dependency that would silently point at the wrong broker).
    """
    code, out = _docker_exec(
        target,
        target.redpanda_container,
        [
            "rpk",
            "topic",
            "consume",
            topic,
            "--offset",
            "end",
            "--num",
            "1",
            "--format",
            "%d\n",
        ],
        timeout=12,
    )
    if code != 0 or not out.strip():
        return None
    first = out.strip().splitlines()[0].strip()
    epoch = re.match(r"^(\d{10,13})$", first)
    if epoch:
        with contextlib.suppress(ValueError, OverflowError):
            digits = epoch.group(1)
            millis = int(digits) if len(digits) >= 13 else int(digits) * 1000
            ts = datetime.datetime.fromtimestamp(millis / 1000, tz=datetime.UTC)
            return (datetime.datetime.now(tz=datetime.UTC) - ts).total_seconds()
        return None
    iso = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", first)
    if not iso:
        return None
    with contextlib.suppress(ValueError):
        ts = datetime.datetime.fromisoformat(iso.group(1)).replace(tzinfo=datetime.UTC)
        return (datetime.datetime.now(tz=datetime.UTC) - ts).total_seconds()
    return None


def probe_consumer_lag(target: ModelLaneTarget, topic: str) -> int:
    """Return consumer lag for the lane's default consumer group on the topic."""
    code, out = _docker_exec(
        target,
        target.redpanda_container,
        ["rpk", "group", "describe", target.consumer_group],
        timeout=20,
    )
    if code != 0:
        return 0  # Unknown — default 0 (handler won't flag LAGGING)

    total_lag = 0
    for line in out.splitlines():
        if topic in line:
            parts = line.split()
            if len(parts) >= 5:
                with contextlib.suppress(ValueError):
                    total_lag += int(parts[-1])
    return total_lag


def probe_table_row_count(target: ModelLaneTarget, table_name: str) -> tuple[int, bool]:
    """Return (row_count, has_recent_data_within_24h) for the lane's table.

    Runs ``psql`` inside the lane's Postgres container. Falls back to (0, False)
    on any error.
    """
    psql_base = [
        "psql",
        "-U",
        target.postgres_user,
        "-d",
        target.postgres_db,
        "-tAc",
    ]

    code, out = _docker_exec(
        target,
        target.postgres_container,
        [*psql_base, f"SELECT COUNT(*) FROM {table_name};"],
        timeout=15,
    )
    if code != 0:
        _log.debug(
            "psql row count failed for %s on lane %s: %s",
            table_name,
            target.lane,
            out.strip(),
        )
        return 0, False
    try:
        row_count = int(out.strip())
    except ValueError:
        return 0, False

    if row_count == 0:
        return 0, False

    recency_sql = (
        f"SELECT CASE WHEN MAX(created_at) > NOW() - INTERVAL '24 hours' "
        f"THEN 1 ELSE 0 END FROM {table_name};"
    )
    code2, out2 = _docker_exec(
        target,
        target.postgres_container,
        [*psql_base, recency_sql],
        timeout=15,
    )
    if code2 != 0:
        # No created_at column or error — treat as recent if rows exist.
        return row_count, True
    try:
        has_recent = int(out2.strip()) == 1
    except ValueError:
        has_recent = True  # fail-open
    return row_count, has_recent


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_flow_metadata(
    flow_descriptor: ModelFlowInput, target: ModelLaneTarget
) -> ModelFlowInput:
    """Populate live metadata for a flow descriptor by probing ``target``'s lane.

    Returns a new (immutable) ModelFlowInput with all live fields filled in.
    The descriptor must have topic, handler_name, and table_name set; all other
    fields are overwritten with live values.

    On a per-probe failure for a *reachable* lane the topic is treated as
    MISSING so the handler classifies it as PRODUCER_DOWN rather than crashing.
    Reachability itself is asserted up-front by the caller via
    :func:`assert_lane_reachable`, so MISSING here means "topic genuinely absent
    on a lane we could reach", not "wrong host".
    """
    try:
        producer_status, newest_age = probe_producer_status(
            target, flow_descriptor.topic
        )
    except Exception:
        _log.debug(
            "probe_producer_status raised for %s — treating as MISSING",
            flow_descriptor.topic,
        )
        producer_status = EnumProducerStatus.MISSING
        newest_age = None

    consumer_lag = 0
    row_count = 0
    has_recent = False

    if producer_status == EnumProducerStatus.ACTIVE:
        try:
            consumer_lag = probe_consumer_lag(target, flow_descriptor.topic)
        except Exception:
            _log.debug(
                "probe_consumer_lag raised for %s — defaulting to 0",
                flow_descriptor.topic,
            )

        try:
            row_count, has_recent = probe_table_row_count(
                target, flow_descriptor.table_name
            )
        except Exception:
            _log.debug(
                "probe_table_row_count raised for %s — defaulting to 0",
                flow_descriptor.table_name,
            )

    return ModelFlowInput(
        topic=flow_descriptor.topic,
        handler_name=flow_descriptor.handler_name,
        table_name=flow_descriptor.table_name,
        dashboard_route=flow_descriptor.dashboard_route,
        producer_status=producer_status,
        consumer_lag=consumer_lag,
        table_row_count=row_count,
        table_has_recent_data=has_recent,
        field_mapping_valid=flow_descriptor.field_mapping_valid,
        newest_message_age_seconds=newest_age,
        stale_threshold_seconds=_STALE_THRESHOLD_SECONDS,
    )


__all__ = [
    "LaneUnreachableError",
    "assert_lane_reachable",
    "collect_flow_metadata",
    "probe_consumer_lag",
    "probe_producer_status",
    "probe_table_row_count",
]
