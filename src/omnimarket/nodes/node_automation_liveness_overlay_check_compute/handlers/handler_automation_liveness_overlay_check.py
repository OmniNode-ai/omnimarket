# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hold the private liveness overlay and every installer to each other (OMN-20804, plan 4.11).

Three things are checked, none of them by trusting a list:

1. The overlay loads through the seam's own model (omnimarket), so a broken deadline, a cron
   entry with no completion record, a duplicate id or an empty positive control is refused with
   the seam's message. Beyond the seam, a class drill must name its own trigger kind.
2. Every file the scan finds that installs something is covered: a unit, plist or cron file by an
   overlay entry of the same native id, installer code by a line in the sources declaration whose
   named units each have an entry.
3. The declaration and the overlay agree on lifecycle: a ``not_installed`` entry has an installer
   and a ``retired`` entry has none.

A declared source the scan no longer finds is as much a finding as an undeclared one, so the
declaration cannot rot into a list of files that used to matter.
"""

from __future__ import annotations

import re
from collections import defaultdict

import yaml
from pydantic import ValidationError

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationProcessState,
    EnumPositiveControlKind,
    ModelAutomationLivenessEntry,
    ModelAutomationLivenessOverlay,
)
from omnimarket.nodes.node_automation_liveness_overlay_check_compute.models import (
    EnumAutomationLivenessFindingKind,
    EnumAutomationLivenessSourceClass,
    ModelAutomationLivenessFinding,
    ModelAutomationLivenessFoundSource,
    ModelAutomationLivenessInstallerScanRequest,
    ModelAutomationLivenessOverlayCheckRequest,
    ModelAutomationLivenessOverlayCheckResult,
    ModelAutomationLivenessSources,
)

from .handler_automation_liveness_installer_scan import (
    HandlerAutomationLivenessInstallerScan,
)

_Kind = EnumAutomationLivenessFindingKind
_PROCESS_PREFIX = re.compile(r"^(?P<process>[a-z0-9][a-z0-9._/-]*):")
_REPLAY_NAMESPACE = "replay/"
_CLASS_DRILL_NAMESPACE = "class-drill/"


def _finding(kind: _Kind, subject: str, detail: str) -> ModelAutomationLivenessFinding:
    return ModelAutomationLivenessFinding(kind=kind, subject=subject, detail=detail)


def _load_overlay(
    text: str, findings: list[ModelAutomationLivenessFinding]
) -> tuple[ModelAutomationLivenessEntry, ...] | None:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        findings.append(_finding(_Kind.OVERLAY_INVALID, "overlay", f"not YAML: {exc}"))
        return None
    try:
        overlay = ModelAutomationLivenessOverlay.model_validate(raw)
    except ValidationError as exc:
        processes = raw.get("processes") if isinstance(raw, dict) else None
        for error in exc.errors():
            message = str(error["msg"]).removeprefix("Value error, ")
            subject = "overlay"
            location = error["loc"]
            if (
                len(location) >= 2
                and location[0] == "processes"
                and isinstance(location[1], int)
                and isinstance(processes, list)
                and location[1] < len(processes)
                and isinstance(processes[location[1]], dict)
            ):
                subject = str(processes[location[1]].get("process_id", subject))
                field = ".".join(str(part) for part in location[2:])
                message = f"{field}: {message}" if field else message
            else:
                named = _PROCESS_PREFIX.match(message)
                if named is not None:
                    subject = named.group("process")
            findings.append(_finding(_Kind.OVERLAY_INVALID, subject, message))
        return None
    return overlay.processes


def _load_sources(
    text: str, findings: list[ModelAutomationLivenessFinding]
) -> ModelAutomationLivenessSources | None:
    try:
        return ModelAutomationLivenessSources.model_validate(yaml.safe_load(text))
    except (yaml.YAMLError, ValidationError) as exc:
        findings.append(_finding(_Kind.SOURCES_INVALID, "sources", str(exc)))
        return None


def _check_positive_controls(
    entries: tuple[ModelAutomationLivenessEntry, ...],
    findings: list[ModelAutomationLivenessFinding],
) -> None:
    for entry in entries:
        control = entry.positive_control
        if control.kind is EnumPositiveControlKind.CLASS_DRILL:
            expected = f"{_CLASS_DRILL_NAMESPACE}{entry.trigger.kind.value}"
            if control.ref != expected:
                findings.append(
                    _finding(
                        _Kind.POSITIVE_CONTROL_MISMATCH,
                        entry.process_id,
                        f"a class drill for a {entry.trigger.kind.value} entry is "
                        f"'{expected}', not '{control.ref}'",
                    )
                )
        elif not control.ref.startswith(_REPLAY_NAMESPACE):
            findings.append(
                _finding(
                    _Kind.POSITIVE_CONTROL_MISMATCH,
                    entry.process_id,
                    f"a replay fixture ref starts with '{_REPLAY_NAMESPACE}', got '{control.ref}'",
                )
            )


class HandlerAutomationLivenessOverlayCheck:
    """Check the overlay, the sources declaration and the scan against each other."""

    def handle(
        self, request: ModelAutomationLivenessOverlayCheckRequest
    ) -> ModelAutomationLivenessOverlayCheckResult:
        findings: list[ModelAutomationLivenessFinding] = []
        entries = _load_overlay(request.overlay_text, findings)
        declaration = _load_sources(request.sources_text, findings)
        scan = HandlerAutomationLivenessInstallerScan().handle(
            ModelAutomationLivenessInstallerScanRequest(files=request.files)
        )
        if entries is not None:
            _check_positive_controls(entries, findings)
        if entries is not None and declaration is not None:
            self._cross_check(request, entries, declaration, scan.sources, findings)
        findings.sort(key=lambda f: (f.kind.value, f.subject, f.detail))
        return ModelAutomationLivenessOverlayCheckResult(
            findings=tuple(findings),
            entries_checked=len(entries or ()),
            sources_found=len(scan.sources),
            repositories_scanned=request.scanned_repositories,
            complete=request.complete,
        )

    def _cross_check(
        self,
        request: ModelAutomationLivenessOverlayCheckRequest,
        entries: tuple[ModelAutomationLivenessEntry, ...],
        declaration: ModelAutomationLivenessSources,
        found: tuple[ModelAutomationLivenessFoundSource, ...],
        findings: list[ModelAutomationLivenessFinding],
    ) -> None:
        by_native: dict[str, list[ModelAutomationLivenessEntry]] = defaultdict(list)
        for entry in entries:
            by_native[entry.trigger.native_id].append(entry)
        scanned = set(request.scanned_repositories)
        if request.complete:
            for repository in declaration.repositories:
                if repository not in scanned:
                    findings.append(
                        _finding(
                            _Kind.REPOSITORY_NOT_SCANNED,
                            repository,
                            "the declaration covers this repository and the run did not scan it",
                        )
                    )
        declared = {(s.repository, s.path): s for s in declaration.sources}
        found_keys = {(s.repository, s.path) for s in found}
        installed: set[str] = set()

        for source in declaration.sources:
            for name in source.installs:
                installed.add(name)
                if name not in by_native:
                    findings.append(
                        _finding(
                            _Kind.UNDECLARED_UNIT,
                            f"{source.repository}/{source.path}",
                            f"installs '{name}', which no overlay entry declares as its native id",
                        )
                    )
            key = (source.repository, source.path)
            if source.repository in scanned and key not in found_keys:
                findings.append(
                    _finding(
                        _Kind.STALE_SOURCE,
                        f"{source.repository}/{source.path}",
                        "declared as a source, but the scan finds no unit, template, cron file "
                        "or installer there; remove the line and retire what it installed",
                    )
                )

        for item in found:
            key = (item.repository, item.path)
            subject = f"{item.repository}/{item.path}"
            if item.source_class is EnumAutomationLivenessSourceClass.UNIT_MANIFEST:
                for unit in item.required_units:
                    if not any(name in by_native for name in unit.alternatives):
                        findings.append(
                            _finding(
                                _Kind.UNDECLARED_UNIT,
                                subject,
                                f"the manifest names '{unit.name}', which no overlay entry "
                                f"declares (native id one of {list(unit.alternatives)})",
                            )
                        )
                continue
            matched = [name for name in item.implied_native_ids if name in by_native]
            installed.update(matched)
            if key in declared or matched:
                continue
            if item.source_class is EnumAutomationLivenessSourceClass.INSTALLER_CODE:
                detail = (
                    "installer code writes a launchd agent, systemd unit, crontab or cron.d "
                    "file; list it in the sources declaration with the units it installs, each "
                    "declared in the overlay"
                )
            else:
                detail = (
                    f"a {item.source_class.value} with no overlay entry; declare an entry whose "
                    f"native id is one of {list(item.implied_native_ids)}, or list the file in "
                    "the sources declaration with the units it installs"
                )
            findings.append(_finding(_Kind.UNDECLARED_SOURCE, subject, detail))

        for entry in entries:
            native = entry.trigger.native_id
            # A partial run cannot see an installer that lives in a repository it did not scan.
            if (
                request.complete
                and entry.state is EnumAutomationProcessState.NOT_INSTALLED
                and native not in installed
            ):
                findings.append(
                    _finding(
                        _Kind.NOT_INSTALLED_WITHOUT_INSTALLER,
                        entry.process_id,
                        f"state not_installed, but no installer installs '{native}'; "
                        "mark it retired",
                    )
                )
            if (
                entry.state is EnumAutomationProcessState.RETIRED
                and native in installed
            ):
                findings.append(
                    _finding(
                        _Kind.RETIRED_STILL_INSTALLED,
                        entry.process_id,
                        f"state retired, but an installer still installs '{native}'; delete "
                        "the installer target in its repository",
                    )
                )
