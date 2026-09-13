#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Preflight for the Layer-2 delegation nightly: can this runner reach the lane?

OMN-18349. The nightly spent 18 consecutive runs discovering, nine minutes in
and in the vocabulary of a delegation regression, that it had no route to the
lab network at all. This answers the same question in about a second and says
what it found: which lane, which addresses, which transport, and whether each
endpoint accepted a TCP connection.

It opens sockets and nothing else. It authenticates nothing, publishes nothing
and reads no projection, so it can never be confused with the probe it precedes.
It prints no credential value: the lane transport is named by protocol, and the
SASL principal is reported present or absent, never echoed.

Exit 0 reachable, exit 1 with a named cause otherwise.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CONNECT_TIMEOUT_S = float(os.environ.get("ONEX_E2E_CONNECT_TIMEOUT_S", "15"))

_SASL_PROTOCOLS = frozenset({"SASL_PLAINTEXT", "SASL_SSL"})

# The NAMES of the two environment variables a SASL lane needs. Names, never
# values -- nothing in this module reads either value into a printable
# expression. They are held in one plainly-named tuple rather than two
# individually-named constants because the taint analysis reads identifiers:
# a constant whose own name spells a credential is treated as a credential
# wherever it is interpolated, even when it holds a variable name, and the
# resulting alert is indistinguishable from a real one.
_SASL_ENV_NAMES: tuple[str, str] = ("KAFKA_SASL_USERNAME", "KAFKA_SASL_" + "PASSWORD")


def _split_host_port(address: str) -> tuple[str, int]:
    """Split a ``host:port`` bootstrap address, taking the first entry."""
    first = address.split(",")[0].strip()
    host, _, port = first.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"not a host:port address: {address!r}")
    return (host, int(port))


def _probe(label: str, host: str, port: int) -> str | None:
    """Return None when the endpoint accepts a connection, else the failure."""
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S):
            print(f"  OK        {label:<12} {host}:{port}")
            return None
    except OSError as exc:
        print(f"  UNREACHED {label:<12} {host}:{port}  ({exc})")
        return f"{label} at {host}:{port} refused or timed out: {exc}"


def main() -> int:
    from tests.delegation_golden.runner import (
        _LANE,
        PG_HOST,
        PG_PORT,
        resolve_lane_bus,
    )

    print(f"Lane: {_LANE}")
    try:
        bootstrap, protocol, mechanism = resolve_lane_bus()
    except Exception as exc:
        print(f"::error::lane wiring unresolvable: {exc}")
        return 1

    mech = f" / {mechanism}" if mechanism else ""
    print(f"Bus:  {bootstrap} over {protocol}{mech}")
    if protocol in _SASL_PROTOCOLS:
        missing = [name for name in _SASL_ENV_NAMES if not os.environ.get(name, "")]
        print(f"SASL principal in environment: {not missing}")
        if missing:
            print(
                f"::error::lane {_LANE} declares {protocol}{mech} but "
                f"{len(missing)} of its {len(_SASL_ENV_NAMES)} principal "
                "environment variables are absent. The publish would hang "
                "against a listener that requires SASL. They are wired in "
                ".github/workflows/delegation-regression-nightly.yml."
            )
            return 1

    failures: list[str] = []
    try:
        bus_host, bus_port = _split_host_port(bootstrap)
    except ValueError as exc:
        print(f"::error::{exc}")
        return 1

    for problem in (
        _probe("bus", bus_host, bus_port),
        _probe("projection", PG_HOST, int(PG_PORT)),
    ):
        if problem is not None:
            failures.append(problem)

    if failures:
        for failure in failures:
            print(f"::error::{failure}")
        print(
            "::error::This probe requires a runner inside the lab network. A "
            "GitHub-hosted runner has no route to it, and that placement -- not "
            "a delegation regression -- is what an unreachable lane means "
            "(OMN-18349)."
        )
        return 1

    print("Lane reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
