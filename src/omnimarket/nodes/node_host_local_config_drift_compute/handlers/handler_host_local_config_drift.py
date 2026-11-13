# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compare one host's declared local configuration with its measured snapshot and name each difference.

A pure read over typed facts. The declaration says what the host's local configuration should be:
the ssh aliases it answers to and the hostname behind each, the git safe directories, the local git
keys it may hold, and its environment directories with their live files and backup limits. The
measurement, carried in the command, says what was found — file names only, never contents. Every
difference is a finding named with its kind and subject, and any difference reads NOT_WORKING.
Nothing is spelled here: the wildcard safe directory "*" is as undeclared as any other entry the
declaration does not carry, and a backup is any measured file of a declared directory that is not
one of its live files. The declaration and the measurement must speak of one host; otherwise the
run is refused.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from omnimarket.nodes.node_host_local_config_drift_compute.models.model_host_local_config_drift import (
    EnumLocalConfigDriftKind,
    EnumLocalConfigVerdict,
    ModelLocalConfigDriftCommand,
    ModelLocalConfigDriftResult,
    ModelLocalConfigFinding,
)

HOST_MISMATCH = "measurement host does not match the declaration host"


class HandlerHostLocalConfigDrift:
    """``handle(command) -> result``: WORKING, NOT_WORKING with a finding per difference, or a refusal."""

    def handle(
        self, request: ModelLocalConfigDriftCommand
    ) -> ModelLocalConfigDriftResult:
        declaration = request.declaration
        measured = request.measured
        if declaration.host != measured.host:
            raise ValueError(HOST_MISMATCH)
        findings: list[ModelLocalConfigFinding] = []
        declared_aliases = {
            item.alias: item.hostname for item in declaration.ssh_aliases
        }
        measured_aliases = {item.alias: item.hostname for item in measured.ssh_aliases}
        for alias, hostname in measured_aliases.items():
            if alias not in declared_aliases:
                findings.append(
                    ModelLocalConfigFinding(
                        kind=EnumLocalConfigDriftKind.UNDECLARED_SSH_ALIAS,
                        subject=alias,
                        detail=f"measured hostname {hostname}",
                    )
                )
        for alias, hostname in declared_aliases.items():
            if alias not in measured_aliases:
                findings.append(
                    ModelLocalConfigFinding(
                        kind=EnumLocalConfigDriftKind.MISSING_SSH_ALIAS,
                        subject=alias,
                        detail=f"declared hostname {hostname}",
                    )
                )
            elif measured_aliases[alias] != hostname:
                findings.append(
                    ModelLocalConfigFinding(
                        kind=EnumLocalConfigDriftKind.SSH_ALIAS_TARGET_DIFFERS,
                        subject=alias,
                        detail=f"declared {hostname} measured {measured_aliases[alias]}",
                    )
                )
        for entry in measured.git_safe_directories:
            if entry not in declaration.git_safe_directories:
                findings.append(
                    ModelLocalConfigFinding(
                        kind=EnumLocalConfigDriftKind.UNDECLARED_SAFE_DIRECTORY,
                        subject=entry,
                        detail="measured but not declared",
                    )
                )
        for key in measured.git_local_keys:
            if key not in declaration.git_allowed_local_keys:
                findings.append(
                    ModelLocalConfigFinding(
                        kind=EnumLocalConfigDriftKind.UNDECLARED_LOCAL_GIT_KEY,
                        subject=key,
                        detail="measured but not declared",
                    )
                )
        declared_dirs = {item.directory: item for item in declaration.env_dirs}
        measured_dirs = {item.directory: item.files for item in measured.env_dirs}
        for directory in measured_dirs:
            if directory not in declared_dirs:
                findings.append(
                    ModelLocalConfigFinding(
                        kind=EnumLocalConfigDriftKind.UNDECLARED_ENV_DIR,
                        subject=directory,
                        detail="measured but not declared",
                    )
                )
        for directory, declared_dir in declared_dirs.items():
            # A declared directory with no measured entry counts as holding no files.
            files = measured_dirs.get(directory, ())
            for live_file in declared_dir.live_files:
                if live_file not in files:
                    findings.append(
                        ModelLocalConfigFinding(
                            kind=EnumLocalConfigDriftKind.MISSING_ENV_LIVE_FILE,
                            subject=live_file,
                            detail=f"declared live file of {directory}",
                        )
                    )
            backups = [name for name in files if name not in declared_dir.live_files]
            if len(backups) > declared_dir.max_backups:
                findings.append(
                    ModelLocalConfigFinding(
                        kind=EnumLocalConfigDriftKind.ENV_BACKUPS_OVER_LIMIT,
                        subject=directory,
                        detail=f"{len(backups)} backups, limit {declared_dir.max_backups}",
                    )
                )
        ordered = tuple(sorted(findings, key=lambda item: (item.kind, item.subject)))
        return ModelLocalConfigDriftResult(
            correlation_id=request.correlation_id,
            host=declaration.host,
            verdict=EnumLocalConfigVerdict.NOT_WORKING
            if ordered
            else EnumLocalConfigVerdict.WORKING,
            findings=ordered,
        )


def events_of(
    result: ModelLocalConfigDriftResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id.

    A measurement with findings is announced on the found topic and a measurement that agrees on
    everything on the clean topic; either way the run-completed event follows as the last event.
    """
    body: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "host": result.host,
        "verdict": result.verdict.value,
        "findings": [item.model_dump(mode="json") for item in result.findings],
    }
    return [
        (
            topics["host_local_config_drift_found"]
            if result.findings
            else topics["host_local_config_drift_clean"],
            body,
        ),
        (
            topics["host_local_config_drift_run_completed"],
            {
                "correlation_id": result.correlation_id,
                "failed": 0,
                "findings": len(result.findings),
            },
        ),
    ]


def _failed(
    topics: dict[str, str], correlation_id: str, failure: str
) -> list[tuple[str, bytes]]:
    """The typed failure event and the run-completed event that always follows it."""
    return [
        (
            topics["host_local_config_drift_failed"],
            json.dumps(
                {"correlation_id": correlation_id, "failure": failure},
                sort_keys=True,
            ).encode(),
        ),
        (
            topics["host_local_config_drift_run_completed"],
            json.dumps(
                {"correlation_id": correlation_id, "failed": 1, "findings": 0},
                sort_keys=True,
            ).encode(),
        ),
    ]


def answer_command(
    payload: bytes, handler: HandlerHostLocalConfigDrift, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command, or a measurement of another host than its declaration, is
    answered with a typed failure event followed by the run-completed event, never dropped.
    """
    try:
        command = ModelLocalConfigDriftCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        return _failed(topics, "unknown", f"bad-payload:{fields}")
    try:
        result = handler.handle(command)
    except ValueError:
        return _failed(topics, command.correlation_id, "host-mismatch")
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(result, topics)
    ]


__all__ = [
    "HOST_MISMATCH",
    "HandlerHostLocalConfigDrift",
    "answer_command",
    "events_of",
]
