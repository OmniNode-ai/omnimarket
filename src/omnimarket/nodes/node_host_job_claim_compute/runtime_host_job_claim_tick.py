# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One timer tick of the host job claim read, the unit the host installer renders (OMN-25093).

``python -m omnimarket.nodes.node_host_job_claim_compute.runtime_host_job_claim_tick --omni-home <root> --state-dir <dir>``
reads the unit files of the machine it runs on (the host the installer put the unit on, named by
``ONEX_LEDGER_HOST_NAME``), puts the run record on the bus as the contract's run-completed topic and writes
``<state-dir>/last-run.json``. ``--probe`` loads the overlay and prints ``PROBE ok``: it reads no unit, publishes nothing and
writes nothing (the installer runs a probe before it loads the unit).

Exit codes: 0 in sync and the record was published; 1 the claim read found a duplicate or a second implementation (the
record is still published); 2 the host has no declared role; 3 the overlay cannot be loaded, the host is not named or the
bus did not accept the record. The evidence file is written for every code.

The run record goes to the broker the node's overlay ``event_bus`` declares (a lane id; the unit holds no broker setting).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from omnimarket.handlers.bus_binding.declared_broker_publisher import (
    DeclaredBrokerPublisher,
    host_name_from_env,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_host_job_claim_compute.models import (
    ModelHostJobClaimResult,
)
from omnimarket.nodes.node_host_job_claim_compute.overlay import load_overlay
from omnimarket.nodes.node_host_job_claim_compute.runtime_host_job_claim import (
    read_claim,
)

NODE = "node_host_job_claim_compute"
EVIDENCE_FILE = "last-run.json"
HOST_ENV = "ONEX_LEDGER_HOST_NAME"
EVENT_TYPE = "host_job_claim.run_completed"
EXIT_DRIFTED = 1
EXIT_NO_ROLE = 2
EXIT_INDETERMINATE = 3


def _contract() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(
        (Path(__file__).with_name("contract.yaml")).read_text(encoding="utf-8")
    )
    return document


def _host_name() -> str:
    return host_name_from_env(HOST_ENV)


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


def evidence_for(
    result: ModelHostJobClaimResult, stamp: datetime, *, emitted: bool
) -> dict[str, Any]:
    return {
        "node": NODE,
        "ran_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": result.host,
        "role": result.role,
        "correlation_id": result.correlation_id,
        "verdict": "IN_SYNC" if result.ok else "DRIFTED",
        "findings": [
            {
                "kind": finding.kind.value,
                "job_id": finding.job_id,
                "extra_unit": finding.extra_unit,
                "owner_unit": finding.owner_unit,
            }
            for finding in result.findings
        ],
        "emitted": emitted,
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    publisher: DeclaredBrokerPublisher | None = None,
    now: datetime | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog=f"{NODE}.tick", description=__doc__)
    parser.add_argument("--omni-home", default="")
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    stamp = now or datetime.now(UTC)
    evidence_path = args.state_dir / EVIDENCE_FILE if args.state_dir else None
    try:
        load_overlay()
    except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
        error = f"{type(exc).__name__}: {exc}"[-2000:]
        sys.stderr.write(f"INDETERMINATE: {error}\n")
        if evidence_path is not None and not args.probe:
            reading = {
                "node": NODE,
                "ran_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "overlay_unreadable",
                "error": error,
            }
            _write_atomically(evidence_path, json.dumps(reading, sort_keys=True) + "\n")
        return EXIT_INDETERMINATE
    if args.probe:
        sys.stdout.write("PROBE ok\n")
        return 0
    host = _host_name().strip()
    if not host or evidence_path is None:
        sys.stderr.write(
            f"INDETERMINATE: {HOST_ENV} and --state-dir are required to run a pass\n"
        )
        return EXIT_INDETERMINATE
    result = read_claim(host, f"host-job-claim-{uuid.uuid4().hex[:12]}")
    if result is None:
        reading = {
            "node": NODE,
            "ran_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host": host,
            "status": "no_declared_role",
        }
        _write_atomically(evidence_path, json.dumps(reading, sort_keys=True) + "\n")
        sys.stderr.write(f"REFUSED host {host} has no declared role\n")
        return EXIT_NO_ROLE
    topic = str(_contract()["topics"]["host_job_claim_run_completed"])
    emitted = (publisher or DeclaredBrokerPublisher()).publish(
        topic=topic,
        event_type=EVENT_TYPE,
        payload=evidence_for(result, stamp, emitted=True),
        event_id=result.correlation_id,
        correlation_id=result.correlation_id,
    )
    reading = evidence_for(result, stamp, emitted=emitted)
    _write_atomically(evidence_path, json.dumps(reading, sort_keys=True) + "\n")
    for finding in result.findings:
        sys.stdout.write(
            f"DRIFT {host} {finding.kind.value} {finding.job_id}:{finding.extra_unit} "
            f"owner={finding.owner_unit}\n"
        )
    sys.stdout.write(
        f"VERDICT {host}: {reading['verdict']} findings={len(result.findings)} "
        f"emitted={str(emitted).lower()} correlation_id={result.correlation_id}\n"
    )
    if not emitted:
        return EXIT_INDETERMINATE
    return 0 if result.ok else EXIT_DRIFTED


__all__ = ["EVIDENCE_FILE", "DeclaredBrokerPublisher", "evidence_for", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
