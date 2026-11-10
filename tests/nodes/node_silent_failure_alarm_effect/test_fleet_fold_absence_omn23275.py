# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The caller builds the fold-absence fact the running-vs-merged decision names hosts from (OMN-23275).

Before this reader nothing in omnimarket built ``fleet_absence``, so the decision's absence read was
reachable only through its request model. Each reading below drives the reader and then the real
decision handler. The controls are the same reader with the declaration withheld or broken, a fold
that exists, and a fold path that is not a missing file: none of those may carry the fact. Host names
are synthetic; the real declaration is the ``host_drift_declaration`` overlay.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_silent_failure_alarm_compute.handlers.handler_running_vs_merged_alarm import (
    HandlerRunningVsMergedAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.fold_absence_reader import (
    FILE_ENV,
    load_declaration,
    read_fleet_request,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
    ModelHostDriftDeclaration,
    ModelHostDriftDeclaredHost,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
UNIT = "example.host-drift"
INSTALLER = "example-install --registry-root <clone>"


def declaration(*hosts: tuple[str, tuple[str, ...]]) -> ModelHostDriftDeclaration:
    return ModelHostDriftDeclaration(
        unit=UNIT,
        manifest="example-declaration.yaml",
        installer=INSTALLER,
        ticket="OMN-0",
        plist_dir="Library/LaunchAgents",
        hosts=tuple(ModelHostDriftDeclaredHost(id=i, hostnames=n) for i, n in hosts),
    )


DECLARED = declaration(
    ("h-a", ("alpha-box",)), ("h-b", ("Beta-Box", "beta.example.test"))
)


def read(
    tmp_path: Path,
    decl: ModelHostDriftDeclaration | None,
    this_host: str,
    *,
    plist: bool = False,
    fleet: Path | None = None,
):
    home = tmp_path / "home"
    if plist:
        target = home / "Library/LaunchAgents" / f"{UNIT}.plist"
        target.parent.mkdir(parents=True)
        target.write_text("<plist/>")
    return read_fleet_request(
        fleet or tmp_path / "state" / "fleet.json",
        NOW,
        declaration=decl,
        hostname=lambda: this_host,
        home=lambda: home,
    )


def decide(request):
    return HandlerRunningVsMergedAlarm().handle(request)


def test_fold_absence_declared_here_without_plist_names_the_installer(tmp_path):
    request = read(tmp_path, DECLARED, "Beta-Box.example.test")
    fact = request.fleet_absence
    assert fact is not None
    assert (fact.declared_here, fact.plist_installed) == (True, False)
    assert fact.owners == ("h-a (alpha-box)", "h-b (Beta-Box/beta.example.test)")
    result = decide(request)
    assert result.state == "UNKNOWN"
    (finding,) = result.findings
    assert "its plist is not installed" in finding.why
    assert INSTALLER in finding.fix


def test_fold_absence_declared_here_with_plist_reads_its_own_run(tmp_path):
    request = read(tmp_path, DECLARED, "alpha-box", plist=True)
    assert request.fleet_absence is not None
    assert (
        request.fleet_absence.declared_here,
        request.fleet_absence.plist_installed,
    ) == (
        True,
        True,
    )
    (finding,) = decide(request).findings
    assert "has written no fold" in finding.why
    assert f"launchctl list {UNIT}" in finding.fix


def test_fold_absence_declared_elsewhere_names_the_declaring_hosts(tmp_path):
    request = read(tmp_path, DECLARED, "gamma-box")
    assert request.fleet_absence is not None
    assert request.fleet_absence.declared_here is False
    (finding,) = decide(request).findings
    assert "h-a" in finding.why
    assert "gamma-box is not in the manifest" in finding.why


def test_fold_absence_nobody_declares_names_the_ticket(tmp_path):
    request = read(tmp_path, declaration(), "alpha-box")
    assert request.fleet_absence is not None
    assert request.fleet_absence.owners == ()
    (finding,) = decide(request).findings
    assert "has no producer" in finding.why
    assert "OMN-0" in finding.fix


def test_fold_absence_control_no_declaration_keeps_the_file_error(tmp_path):
    request = read(tmp_path, None, "alpha-box")
    assert request.fleet_absence is None
    assert request.fleet_readable is False
    (finding,) = decide(request).findings
    assert "unreadable" in finding.why
    assert "fleet.json" in finding.why


def test_fold_absence_control_a_path_that_is_not_a_missing_file_carries_no_fact(
    tmp_path,
):
    folder = tmp_path / "fleet.json"
    folder.mkdir()
    request = read(tmp_path, DECLARED, "alpha-box", fleet=folder)
    assert request.fleet_absence is None
    assert request.fleet_readable is False


def test_fold_absence_control_a_fold_that_exists_carries_no_fact(tmp_path):
    fold = tmp_path / "fleet.json"
    fold.write_text(json.dumps({"hosts": []}))
    request = read(tmp_path, DECLARED, "alpha-box", fleet=fold)
    assert request.fleet_readable is True
    assert request.fleet_absence is None


def test_fold_absence_control_a_fold_that_is_not_an_object_carries_no_fact(tmp_path):
    fold = tmp_path / "fleet.json"
    fold.write_text("[]")
    request = read(tmp_path, DECLARED, "alpha-box", fleet=fold)
    assert request.fleet_readable is False
    assert request.fleet_absence is None
    assert "not an object" in request.unreadable_why


def test_fold_absence_declaration_is_read_from_the_overlay_file(tmp_path):
    overlay = tmp_path / "declaration.yaml"
    overlay.write_text(
        "unit: example.host-drift\n"
        "manifest: example-declaration.yaml\n"
        "installer: example-install\n"
        "ticket: OMN-0\n"
        "plist_dir: Library/LaunchAgents\n"
        "hosts:\n"
        "  - {id: h-a, hostnames: [alpha-box]}\n"
    )
    loaded = load_declaration({FILE_ENV: str(overlay)})
    assert loaded is not None
    assert [h.id for h in loaded.hosts] == ["h-a"]


def test_fold_absence_control_a_broken_or_missing_overlay_reads_as_none(tmp_path):
    broken = tmp_path / "broken.yaml"
    broken.write_text("unit: example.host-drift\nsurprise: 1\n")
    assert load_declaration({FILE_ENV: str(broken)}) is None
    assert load_declaration({}) is None
