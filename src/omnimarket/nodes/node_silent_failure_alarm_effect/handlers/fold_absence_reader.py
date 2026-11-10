# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the running-vs-merged request from the host-drift fleet fold, with the absence fact (OMN-23275).

Reads only; the decision is HandlerRunningVsMergedAlarm. A fold file that does not exist is a
declaration question: this reader gives the decision the fact it needs to name the hosts the
deployment declares the host-drift unit for, whether this host is one of them and whether its plist is
installed. The declaration is the ``host_drift_declaration`` overlay; the host name and the home
directory are injected, so a test names them and a run reads the machine's own. A declaration that cannot
be read leaves the plain file error on the request, never an empty fact that reads as nobody declaring.
Ported from the internal tooling repository (OMN-17427).
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.model_running_vs_merged_alarm import (
    ModelFleetFoldAbsence,
    ModelRunningVsMergedRequest,
)
from omnimarket.nodes.node_silent_failure_alarm_compute.handlers.handler_running_vs_merged_alarm import (
    request_from_fleet,
)

from ..models import ModelHostDriftDeclaration

OVERLAY_ID = "host_drift_declaration"
FILE_ENV = "ONEX_HOST_DRIFT_DECLARATION_FILE"
WHY_LIMIT = 200


def load_declaration(
    environ: Mapping[str, str] | None = None,
) -> ModelHostDriftDeclaration | None:
    """The deployment's host-drift declaration, or None when it is absent or invalid."""
    try:
        return load_structured_overlay(
            OVERLAY_ID, ModelHostDriftDeclaration, environ, file_env=FILE_ENV
        )
    except NodeOverlayError:
        return None


def _label(name: str) -> str:
    return name.split(".", 1)[0].casefold()


def build_fold_absence(
    fleet: Path,
    declaration: ModelHostDriftDeclaration,
    this_host: str,
    plist_installed: bool,
) -> ModelFleetFoldAbsence:
    """The fact for a fold at ``fleet`` that does not exist on ``this_host``."""
    this = _label(this_host)
    owners = tuple(
        f"{host.id} ({'/'.join(host.hostnames)})" for host in declaration.hosts
    )
    declared_here = any(
        this in {_label(name) for name in host.hostnames} for host in declaration.hosts
    )
    return ModelFleetFoldAbsence(
        path=str(fleet),
        this_host=this,
        unit=declaration.unit,
        manifest=declaration.manifest,
        installer=declaration.installer,
        ticket=declaration.ticket,
        owners=owners,
        declared_here=declared_here,
        plist_installed=plist_installed,
    )


def read_fleet_request(
    fleet: Path,
    at: datetime,
    *,
    declaration: ModelHostDriftDeclaration | None,
    hostname: Callable[[], str] = socket.gethostname,
    home: Callable[[], Path] = Path.home,
) -> ModelRunningVsMergedRequest:
    """The request for the fold file at ``fleet``; an absent file carries the absence fact."""
    try:
        payload = json.loads(fleet.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        absence = None
        if isinstance(exc, FileNotFoundError) and declaration is not None:
            plist = home() / declaration.plist_dir / f"{declaration.unit}.plist"
            absence = build_fold_absence(
                fleet, declaration, hostname(), plist.is_file()
            )
        return ModelRunningVsMergedRequest(
            now=at,
            fleet_readable=False,
            unreadable_why=f"{fleet}: {' '.join(str(exc).split())[:WHY_LIMIT]}",
            fleet_absence=absence,
        )
    if not isinstance(payload, dict):
        return ModelRunningVsMergedRequest(
            now=at, fleet_readable=False, unreadable_why=f"{fleet}: not an object"
        )
    return request_from_fleet(payload, at)
