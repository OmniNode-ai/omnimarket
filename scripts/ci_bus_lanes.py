#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# ci_bus_lanes.py
#
# The single home for CI-publisher bus-lane resolution (OMN-14801, OMN-17378).
#
# WHY A SHARED MODULE: this is a FAIL-CLOSED policy surface, not a convenience
# helper. It decides (a) which broker a thin CI publisher targets and (b) whether
# an unresolvable broker is an expected fork-runner skip or a red defect. Two
# private copies of that decision WILL diverge, and a divergence in a fail-closed
# gate is exactly how a publisher goes green-but-silent -- the OMN-17378 class.
#
# CURRENT CONSUMERS: `publish_pr_merged_event.py` and
# `trigger_rebuild_on_merge.py` (the latter for the lane-declared TRANSPORT
# added by OMN-18012; its broker still comes from its own env contract).
# `publish_occ_autobind_command.py` still carries its own private copy of this
# logic (`_MODE_*`, `_resolve_lane_broker`, `_is_trusted_runner`, and now
# `_resolve_lane_security` / `_kafka_producer_config`). Porting it
# here is DELIBERATELY out of scope for OMN-17378: that publisher is live,
# gated and working, its module-level private names are referenced by 78 tests
# in tests/unit/nodes/node_occ_companion_effect/, and folding it into this
# change would expand the blast radius of an urgent outage fix onto the one
# publisher that is NOT broken. The consolidation is tracked as its own
# follow-up on OMN-17378 so the duplication is recorded rather than silent.
# OMN-18012 keeps that boundary and adds the missing safeguard: the transport
# half of the duplication is pinned by
# tests/unit/scripts/test_ci_bus_lane_transport.py, which asserts the two
# implementations return the same answer on the real checked-in overlay and
# refuse the same malformed declarations. Duplication nobody compares is how a
# fail-closed gate drifts into a green no-op.
#
# IMPORTABLE FROM A THIN CI SCRIPT: this is a sibling module in `scripts/`, NOT a
# package import. `python scripts/<publisher>.py` puts `scripts/` on sys.path[0],
# so `from ci_bus_lanes import ...` resolves without executing any omnimarket
# package `__init__` (which would drag in omnibase_core, absent from the minimal
# publisher CI env -- see the `_load_*_topic` docstrings in both publishers).
# Runtime deps: pyyaml + click only.
#
# Ticket: OMN-14801, OMN-17378

from __future__ import annotations

import os
import sys
from pathlib import Path

import click
import yaml

# Checked-in lane -> bus-broker overlay. Resolved relative to this module so the
# resolution is machine-portable (no hardcoded absolute paths).
LANE_OVERLAY_PATH = Path(__file__).resolve().parents[1] / "config" / "ci_bus_lanes.yaml"

# Resolution modes returned by resolve_lane_broker.
MODE_NO_LANE = "no-lane"  # --lane was not supplied
MODE_UNKNOWN_LANE = "unknown-lane"  # --lane supplied but absent from the overlay
MODE_INMEMORY = "inmemory"  # in-process bus (contract-as-data default): no-op
MODE_FROM_SECRET = "from-secret"  # trust the injected secret verbatim (no check)
MODE_CONCRETE = "concrete"  # overlay declares a concrete host:port (preferred)


def load_lane_overlay(path: Path | None = None) -> dict[str, object]:
    """Load the checked-in lane->broker overlay (OMN-14801).

    Returns an empty dict when the overlay file is absent so callers can apply
    the documented "no overlay -> in-memory default" fallback. A malformed
    (non-mapping) overlay is likewise treated as empty rather than crashing the
    thin publisher.
    """
    overlay_path = path if path is not None else LANE_OVERLAY_PATH
    if not overlay_path.exists():
        return {}
    with overlay_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        return {}
    return loaded


def resolve_lane_broker(
    overlay: dict[str, object], lane: str | None
) -> tuple[str, str]:
    """Resolve a CI lane id to ``(mode, declared_broker)`` from the overlay.

    ``mode`` is one of the ``MODE_*`` constants. ``declared_broker`` is the
    concrete ``host:port`` only for ``MODE_CONCRETE``; it is ``""`` otherwise.
    The precedence intentionally mirrors the overlay's documented semantics:
    no lane / unknown lane -> caller applies the in-memory default (or fails
    loud on the trusted runner); an explicit ``inmemory`` -> in-process no-op;
    ``from-secret`` -> trust the injected secret; anything else -> a concrete,
    overlay-preferred broker that the injected secret is checked against.
    """
    if lane is None or not lane.strip():
        return (MODE_NO_LANE, "")
    lane_key = lane.strip()
    lanes_obj = overlay.get("lanes")
    lanes = lanes_obj if isinstance(lanes_obj, dict) else {}
    if lane_key not in lanes:
        return (MODE_UNKNOWN_LANE, "")
    entry = lanes[lane_key]
    broker_raw = entry.get("broker") if isinstance(entry, dict) else entry
    broker_val = "" if broker_raw is None else str(broker_raw).strip()
    if not broker_val or broker_val == MODE_INMEMORY:
        return (MODE_INMEMORY, "")
    if broker_val == MODE_FROM_SECRET:
        return (MODE_FROM_SECRET, "")
    return (MODE_CONCRETE, broker_val)


def is_trusted_runner() -> bool:
    """Return whether this run is on the trusted self-hosted lane (OMN-14451).

    Mirrors the same fork/non-fork test the workflow uses to pick `runs-on:`,
    threaded through as ``RUNNER_IS_TRUSTED`` so a publisher can tell an
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


# --- Lane-declared transport (OMN-18012) -------------------------------------
#
# The overlay declares `security_protocol` and, for a SASL protocol,
# `sasl_mechanism` beside each lane's broker. A CI publisher READS them here; it
# never derives them from the shape of its environment. Credential PRESENCE is
# not a statement about transport: on 2026-09-07 the .201 dev-lane Redpanda
# EXTERNAL listener began requiring SASL/SCRAM-SHA-256 over PLAINTEXT -- no TLS
# at all -- while the KAFKA_SASL_* org secrets were injected, and every publisher
# that inferred `SASL_SSL`/`PLAIN` from that presence died on an SSL handshake
# against a listener that speaks none.
SECURITY_PROTOCOL_KEY = "security_protocol"
SASL_MECHANISM_KEY = "sasl_mechanism"
SASL_PROTOCOLS = frozenset({"SASL_PLAINTEXT", "SASL_SSL"})
NON_SASL_PROTOCOLS = frozenset({"PLAINTEXT", "SSL"})
VALID_PROTOCOLS = SASL_PROTOCOLS | NON_SASL_PROTOCOLS


class LaneSecurityError(ValueError):
    """The lane overlay does not declare a usable transport for this lane.

    Raised instead of guessing. Every call site turns this into a loud exit 1
    naming the lane, because a publisher that guesses its transport is the
    OMN-18012 outage.
    """


def resolve_lane_security(
    overlay: dict[str, object], lane: str | None
) -> tuple[str, str]:
    """Resolve a lane id to its DECLARED ``(security_protocol, sasl_mechanism)``.

    Reads the overlay and nothing else. An undeclared, malformed or
    contradictory declaration raises :class:`LaneSecurityError` so the caller
    fails loud rather than publishing over a transport nobody chose.

    Only called on the branches that actually publish (a concrete ``host:port``
    or a ``from-secret`` lane). An ``inmemory`` / unresolvable lane publishes
    nothing cross-process, so it is never asked for a transport.
    """
    lane_key = "" if lane is None else lane.strip()
    lanes_obj = overlay.get("lanes")
    lanes = lanes_obj if isinstance(lanes_obj, dict) else {}
    entry = lanes.get(lane_key)
    if not isinstance(entry, dict):
        raise LaneSecurityError(
            f"lane {lane_key!r} declares no transport in "
            f"config/{LANE_OVERLAY_PATH.name}: the lane entry is not a mapping, "
            f"so it carries no {SECURITY_PROTOCOL_KEY!r}. A publishing lane MUST "
            f"declare {SECURITY_PROTOCOL_KEY} (one of {sorted(VALID_PROTOCOLS)}) "
            f"and, for a SASL protocol, {SASL_MECHANISM_KEY}. Refusing to guess "
            "the transport (OMN-18012)."
        )

    protocol_raw = entry.get(SECURITY_PROTOCOL_KEY)
    protocol = "" if protocol_raw is None else str(protocol_raw).strip().upper()
    if not protocol:
        raise LaneSecurityError(
            f"lane {lane_key!r} does not declare {SECURITY_PROTOCOL_KEY} in "
            f"config/{LANE_OVERLAY_PATH.name}. Declare one of "
            f"{sorted(VALID_PROTOCOLS)} beside the lane's broker. Refusing to "
            "guess the transport -- inferring TLS from the presence of "
            "credentials is exactly the OMN-18012 outage."
        )
    if protocol not in VALID_PROTOCOLS:
        raise LaneSecurityError(
            f"lane {lane_key!r} declares {SECURITY_PROTOCOL_KEY}={protocol!r} in "
            f"config/{LANE_OVERLAY_PATH.name}, which is not a librdkafka "
            f"security protocol. Valid values: {sorted(VALID_PROTOCOLS)}."
        )

    mechanism_raw = entry.get(SASL_MECHANISM_KEY)
    mechanism = "" if mechanism_raw is None else str(mechanism_raw).strip()
    if protocol in SASL_PROTOCOLS and not mechanism:
        raise LaneSecurityError(
            f"lane {lane_key!r} declares {SECURITY_PROTOCOL_KEY}={protocol} but no "
            f"{SASL_MECHANISM_KEY} in config/{LANE_OVERLAY_PATH.name}. A SASL "
            "protocol needs its mechanism declared (e.g. SCRAM-SHA-256). Refusing "
            "to guess it (OMN-18012)."
        )
    if protocol not in SASL_PROTOCOLS and mechanism:
        raise LaneSecurityError(
            f"lane {lane_key!r} declares {SASL_MECHANISM_KEY}={mechanism!r} beside a "
            f"non-SASL {SECURITY_PROTOCOL_KEY}={protocol} in "
            f"config/{LANE_OVERLAY_PATH.name}. That is a contradictory "
            "declaration: fix the overlay rather than let the publisher pick a "
            "half of it."
        )
    return (protocol, mechanism)


def build_producer_config(
    bootstrap_servers: str,
    username: str,
    secret: str,
    security_protocol: str,
    sasl_mechanism: str,
) -> dict[str, str | int | float | bool]:
    """Build a librdkafka producer config from the LANE-DECLARED transport.

    ``security_protocol`` / ``sasl_mechanism`` come from
    :func:`resolve_lane_security` and are required arguments: a default here
    would be the guess this function exists to delete. ``secret`` is the SASL
    password for ``username``; it is only ever placed in the returned config.
    """
    protocol = security_protocol.strip().upper()
    mechanism = sasl_mechanism.strip()
    if protocol not in VALID_PROTOCOLS:
        raise LaneSecurityError(
            f"{SECURITY_PROTOCOL_KEY}={security_protocol!r} is not a librdkafka "
            f"security protocol. Valid values: {sorted(VALID_PROTOCOLS)}."
        )

    config: dict[str, str | int | float | bool] = {
        "bootstrap.servers": bootstrap_servers,
        "security.protocol": protocol,
    }

    if protocol not in SASL_PROTOCOLS:
        if username or secret:
            # Not an error: the declared transport is authoritative and dropping
            # credentials it cannot carry is safe. It IS drift worth naming, so
            # say so rather than let a silently-unauthenticated publish look
            # identical to a correctly-configured one.
            click.echo(
                "WARNING: SASL credentials are present in the environment but "
                f"the lane declares {SECURITY_PROTOCOL_KEY}={protocol}, which "
                "carries none. Publishing unauthenticated per the lane "
                "declaration and IGNORING the injected credentials. If the "
                "broker now requires SASL, the lane overlay is stale -- fix "
                f"config/{LANE_OVERLAY_PATH.name}, never the inference "
                "(OMN-18012).",
                err=True,
            )
        return config

    if not mechanism:
        raise LaneSecurityError(
            f"{SECURITY_PROTOCOL_KEY}={protocol} requires a "
            f"{SASL_MECHANISM_KEY}; none was declared for this lane."
        )
    if not username or not secret:
        raise LaneSecurityError(
            f"the lane declares {SECURITY_PROTOCOL_KEY}={protocol} / "
            f"{SASL_MECHANISM_KEY}={mechanism}, but KAFKA_SASL_USERNAME and/or "
            "KAFKA_SASL_PASSWORD are not set in this job's environment. A SASL "
            "lane cannot be published to without credentials. Wire them through "
            "the calling workflow (`secrets: inherit`) rather than downgrading "
            "the declared transport."
        )
    config.update(
        {
            "sasl.mechanisms": mechanism,
            "sasl.username": username,
            "sasl.password": secret,
        }
    )
    return config
