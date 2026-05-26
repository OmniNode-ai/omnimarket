# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""LiveMetadataCollector — shell-out collection phase for node_data_flow_sweep.

Runs rpk topic/group describe and psql row-count checks to populate
ModelFlowInput objects from live infrastructure.  The handler itself remains
pure compute; all side-effectful I/O lives here.

This module is intentionally NOT imported by the handler or any test that
exercises pure classification logic.  It is only imported by __main__.py
when the --collect flag is set.
"""

from __future__ import annotations

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

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants resolved from env (never hardcoded)
# ---------------------------------------------------------------------------

# fallback-ok: collector is a CLI probe tool; dev lane defaults let local runs
# work without requiring every env var to be set.  Production runs always set
# these via Infisical / the compose env block.
_KAFKA_BROKERS: Final[str] = os.environ.get(
    "KAFKA_BOOTSTRAP_SERVERS", "localhost:19092"
)  # fallback-ok: dev lane default for local probe runs
_POSTGRES_HOST: Final[str] = os.environ.get(
    "POSTGRES_HOST", "localhost"
)  # fallback-ok: dev lane default for local probe runs
_POSTGRES_PORT: Final[str] = os.environ.get(
    "POSTGRES_PORT", "5436"
)  # fallback-ok: dev lane default for local probe runs
_POSTGRES_DB: Final[str] = os.environ.get(
    "POSTGRES_DB", "omnidash_analytics"
)  # fallback-ok: dev lane default for local probe runs
_POSTGRES_USER: Final[str] = os.environ.get(
    "POSTGRES_USER", "postgres"
)  # fallback-ok: dev lane default for local probe runs
_CONSUMER_GROUP: Final[str] = os.environ.get(
    "OMNIDASH_CONSUMER_GROUP", "omnidash-read-model-v2"
)  # fallback-ok: dev lane default for local probe runs
_STALE_THRESHOLD_SECONDS: Final[float] = float(
    os.environ.get("ONEX_FLOW_STALE_THRESHOLD_SECONDS", "1800")
)  # fallback-ok: 30m is a safe operational default; overrideable via env

# rpk container name — can differ per lane
_RPK_CONTAINER: Final[str] = os.environ.get(
    "ONEX_RPK_CONTAINER", "omnibase-infra-redpanda"
)  # fallback-ok: dev lane container name default


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------


def _run(cmd: str, *, timeout: int = 10) -> tuple[int, str]:
    """Run a shell command; return (returncode, combined stdout+stderr)."""
    try:
        result = subprocess.run(
            shlex.split(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        return result.returncode, output
    except FileNotFoundError as exc:
        _log.debug("command not found: %s — %s", cmd.split()[0], exc)
        return 127, str(exc)
    except subprocess.TimeoutExpired:
        _log.debug("command timed out: %s", cmd)
        return 124, "timeout"


# ---------------------------------------------------------------------------
# Per-dimension probes
# ---------------------------------------------------------------------------


def probe_producer_status(topic: str) -> tuple[EnumProducerStatus, float | None]:
    """Return (producer_status, newest_message_age_seconds).

    Tries docker exec rpk first; falls back to kcat if docker is unavailable.
    newest_message_age_seconds is None when it cannot be determined.
    """
    # Attempt 1: docker exec rpk topic describe
    code, out = _run(
        f"docker exec {_RPK_CONTAINER} rpk topic describe {topic} --format json",
        timeout=15,
    )
    if code == 0 and topic in out:
        # Topic exists; try to read newest message timestamp via kcat
        age = _probe_newest_message_age(topic)
        # Determine ACTIVE vs EMPTY from offset presence
        if re.search(r'"high_watermark"\s*:\s*[1-9]', out):
            return EnumProducerStatus.ACTIVE, age
        return EnumProducerStatus.EMPTY, None

    # Check for explicit "not found" / "does not exist"
    if code == 0 and ("not found" in out.lower() or "does not exist" in out.lower()):
        return EnumProducerStatus.MISSING, None

    # Attempt 2: kcat -L (broker metadata)
    code2, out2 = _run(
        f"kcat -L -b {_KAFKA_BROKERS} -t {topic} 2>&1",
        timeout=10,
    )
    if code2 == 0 and topic in out2:
        age = _probe_newest_message_age(topic)
        return EnumProducerStatus.ACTIVE, age
    if "unknown topic" in out2.lower() or "no topic" in out2.lower():
        return EnumProducerStatus.MISSING, None

    # Cannot determine — treat as MISSING (fail-safe)
    _log.debug("cannot determine producer status for %s (code=%d)", topic, code)
    return EnumProducerStatus.MISSING, None


def _probe_newest_message_age(topic: str) -> float | None:
    """Return age in seconds of the newest message in topic, or None."""
    # Read the last message header; kcat -C -o end -c 1 -e prints it with -T flag
    code, out = _run(
        f"kcat -C -b {_KAFKA_BROKERS} -t {topic} -o end -c 1 -e -T",
        timeout=8,
    )
    if code != 0 or not out.strip():
        return None
    # kcat -T prepends ISO timestamp like "2025-05-22T18:00:00.000Z  {"
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", out.strip())
    if not m:
        return None
    try:
        import datetime

        ts = datetime.datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
        now = datetime.datetime.now(tz=datetime.UTC)
        return (now - ts).total_seconds()
    except Exception:
        return None


def probe_consumer_lag(topic: str) -> int:
    """Return consumer lag for the default consumer group on the given topic."""
    code, out = _run(
        f"docker exec {_RPK_CONTAINER} rpk group describe {_CONSUMER_GROUP}",
        timeout=15,
    )
    if code != 0:
        return 0  # Unknown — default 0 (handler won't flag LAGGING)

    # Parse lag column for this topic
    # rpk output format: TOPIC  PARTITION  ...  LAG
    import contextlib

    total_lag = 0
    for line in out.splitlines():
        if topic in line:
            parts = line.split()
            if len(parts) >= 5:
                with contextlib.suppress(ValueError):
                    total_lag += int(parts[-1])
    return total_lag


def probe_table_row_count(table_name: str) -> tuple[int, bool]:
    """Return (row_count, has_recent_data_within_24h).

    has_recent_data is True when at least one row has created_at within 24h.
    Falls back to (0, False) on any error.
    """
    psql_base = (
        f"psql -h {_POSTGRES_HOST} -p {_POSTGRES_PORT} "
        f"-U {_POSTGRES_USER} -d {_POSTGRES_DB} -tAc"
    )

    # Row count
    code, out = _run(f'{psql_base} "SELECT COUNT(*) FROM {table_name};"', timeout=10)
    if code != 0:
        _log.debug("psql row count failed for %s: %s", table_name, out.strip())
        return 0, False
    try:
        row_count = int(out.strip())
    except ValueError:
        return 0, False

    if row_count == 0:
        return 0, False

    # Recency check — try created_at column; not all tables have it
    recency_sql = (
        f"SELECT CASE WHEN MAX(created_at) > NOW() - INTERVAL '24 hours' "
        f"THEN 1 ELSE 0 END FROM {table_name};"
    )
    code2, out2 = _run(f'{psql_base} "{recency_sql}"', timeout=10)
    if code2 != 0:
        # No created_at column or error — treat as recent if rows exist
        return row_count, True
    try:
        has_recent = int(out2.strip()) == 1
    except ValueError:
        has_recent = True  # fail-open
    return row_count, has_recent


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_flow_metadata(flow_descriptor: ModelFlowInput) -> ModelFlowInput:
    """Populate live metadata for a flow descriptor by running rpk/psql probes.

    Returns a new (immutable) ModelFlowInput with all live fields filled in.
    The descriptor must have topic, handler_name, and table_name set; all other
    fields will be overwritten with live values.

    Never raises — on any unexpected probe failure, the topic is treated as
    MISSING so the handler classifies it as PRODUCER_DOWN rather than crashing.
    """
    try:
        producer_status, newest_age = probe_producer_status(flow_descriptor.topic)
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
            consumer_lag = probe_consumer_lag(flow_descriptor.topic)
        except Exception:
            _log.debug(
                "probe_consumer_lag raised for %s — defaulting to 0",
                flow_descriptor.topic,
            )

        try:
            row_count, has_recent = probe_table_row_count(flow_descriptor.table_name)
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
