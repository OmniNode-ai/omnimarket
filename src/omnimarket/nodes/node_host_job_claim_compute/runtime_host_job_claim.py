# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_job_claim_compute --host <name>``.

Reads the units installed on the host and, against the role baseline, names every installed claimant
of a job that is not its owner as a duplicate and the owner that lost its marker as a second
implementation. It writes nothing: a finding is a reading, never a gate, a stop or an admission.

Output is one ``DRIFT`` line per finding, then one ``VERDICT`` line. Exit 0 when in sync, 1 when
drifted, 2 when the host has no declared role.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_host_job_claim_compute.adapters.installed_units_files import (
    InstalledUnitsFiles,
)
from omnimarket.nodes.node_host_job_claim_compute.handlers.handler_host_job_claim import (
    HandlerHostJobClaim,
)
from omnimarket.nodes.node_host_job_claim_compute.models import (
    ModelHostJobClaimRequest,
)
from omnimarket.nodes.node_host_job_claim_compute.overlay import load_overlay
from omnimarket.nodes.node_host_job_claim_compute.protocols import (
    ProtocolInstalledUnits,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_host_job_claim_compute.overlay import (
        ModelHostJobClaimOverlay,
    )


def _find_role(overlay: ModelHostJobClaimOverlay, host: str) -> str:
    for assignment in overlay.hosts:
        if assignment.host == host:
            return assignment.role
    raise LookupError(host)


def _run(
    host: str,
    correlation_id: str,
    port: ProtocolInstalledUnits | None = None,
) -> int:
    overlay = load_overlay()
    try:
        role_name = _find_role(overlay, host)
    except LookupError:
        sys.stderr.write(f"REFUSED host {host} has no declared role\n")
        return 2
    baseline = next(role for role in overlay.roles if role.role == role_name)
    installed = (port or InstalledUnitsFiles()).installed(
        overlay.unit_dirs, overlay.unit_suffix
    )
    request = ModelHostJobClaimRequest(
        correlation_id=correlation_id,
        host=host,
        installed_units=installed,
        baseline=baseline,
    )
    result = HandlerHostJobClaim().handle(request)
    for finding in result.findings:
        sys.stdout.write(
            f"DRIFT {host} {finding.kind.value} {finding.job_id}:{finding.extra_unit} "
            f"owner={finding.owner_unit}\n"
        )
    if result.ok:
        sys.stdout.write(f"VERDICT {host}: IN_SYNC findings=0\n")
    else:
        sys.stdout.write(f"VERDICT {host}: DRIFTED findings={len(result.findings)}\n")
    return 0 if result.ok else 1


def main(
    argv: Sequence[str] | None = None, *, port: ProtocolInstalledUnits | None = None
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="node_host_job_claim_compute",
        description=__doc__,
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--correlation-id", default="cli")
    args = parser.parse_args(arguments)
    try:
        return _run(args.host, args.correlation_id, port)
    except NodeOverlayError as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
