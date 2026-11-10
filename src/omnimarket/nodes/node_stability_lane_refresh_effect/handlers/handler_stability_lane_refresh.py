# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refresh the stability lane from its committed dated release pin, on this host's overlay (OMN-21613).

``handle(ModelStabilityRefreshCommand) -> ModelStabilityRefreshResult``. The decision is the node's; the
build, the targeted recreate, the health gate and the rollback stay in ``refresh_stability_lane.sh``, which
this node runs through an injected runner. A request whose tag set is not the committed pin's exits nonzero
unless it carries ``bump``; a request for the set already applied runs nothing. Every tag is a release tag
of a clone on the host, checked before anything is read from the lane or run.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_stability_lane_refresh_effect.models.model_stability_lane_refresh import (
    RELEASE_TAG_PATTERN,
    EnumStabilityRefreshOutcome,
    ModelReleasePinSet,
    ModelStabilityPinRecord,
    ModelStabilityRefreshCommand,
    ModelStabilityRefreshResult,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.overlay import (
    ModelStabilityHostOverlay,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.protocols import (
    ProtocolAppliedPinStore,
    ProtocolLaneHealth,
    ProtocolPinSource,
    ProtocolRefreshRunner,
    ProtocolReleaseTagCheck,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_PIN_REPOS = ("omnibase_infra", "omnibase_core", "omnimarket")
_TAG = re.compile(RELEASE_TAG_PATTERN)

EXIT_OK = 0
EXIT_REFUSED_NOT_RELEASE_TAG = 64
EXIT_REFUSED_PIN_DIFFERS = 65
EXIT_REFUSED_PIN_UNREADABLE = 66
EXIT_REFRESH_FAILED = 1


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerStabilityLaneRefresh:
    """Decide and run one refresh of the stability lane on the overlay's host."""

    def __init__(
        self,
        *,
        overlay: ModelStabilityHostOverlay,
        pin_source: ProtocolPinSource,
        applied: ProtocolAppliedPinStore,
        runner: ProtocolRefreshRunner,
        tags: ProtocolReleaseTagCheck,
        health: ProtocolLaneHealth | None = None,
    ) -> None:
        self._overlay = overlay
        self._pin_source = pin_source
        self._applied = applied
        self._runner = runner
        self._tags = tags
        self._health = health

    def _result(
        self,
        command: ModelStabilityRefreshCommand,
        outcome: EnumStabilityRefreshOutcome,
        exit_code: int,
        *,
        pin_date: str = "",
        tags: ModelReleasePinSet | None = None,
        running: ModelReleasePinSet | None = None,
        argv: tuple[str, ...] = (),
        env_names: tuple[str, ...] = (),
        stale: bool = False,
        detail: str = "",
    ) -> ModelStabilityRefreshResult:
        return ModelStabilityRefreshResult(
            correlation_id=command.correlation_id,
            outcome=outcome,
            exit_code=exit_code,
            lane=self._overlay.lane,
            box_id=self._overlay.box_id,
            pin_date=pin_date,
            tags=tags.as_pairs() if tags else (),
            running_tags=running.as_pairs() if running else (),
            argv=argv,
            env_names=env_names,
            pin_record_stale=stale,
            detail=detail,
        )

    def _read_pin(
        self, command: ModelStabilityRefreshCommand
    ) -> ModelStabilityPinRecord | ModelStabilityRefreshResult:
        try:
            document = yaml.safe_load(self._pin_source.read_text())
        except (OSError, yaml.YAMLError) as error:
            return self._result(
                command,
                EnumStabilityRefreshOutcome.REFUSED_PIN_UNREADABLE,
                EXIT_REFUSED_PIN_UNREADABLE,
                detail=f"pin file unreadable: {type(error).__name__}",
            )
        pin = document.get("pin") if isinstance(document, dict) else None
        if not isinstance(pin, dict):
            return self._result(
                command,
                EnumStabilityRefreshOutcome.REFUSED_PIN_UNREADABLE,
                EXIT_REFUSED_PIN_UNREADABLE,
                detail="pin file has no pin mapping",
            )
        for repo in _PIN_REPOS:
            if not _TAG.match(str(pin.get(repo, ""))):
                return self._result(
                    command,
                    EnumStabilityRefreshOutcome.REFUSED_NOT_RELEASE_TAG,
                    EXIT_REFUSED_NOT_RELEASE_TAG,
                    detail=f"release-tag-only: {repo} pin {pin.get(repo)!r} is not a v<major>.<minor>.<patch> tag",
                )
        try:
            return ModelStabilityPinRecord.model_validate(document)
        except ValidationError as error:
            return self._result(
                command,
                EnumStabilityRefreshOutcome.REFUSED_PIN_UNREADABLE,
                EXIT_REFUSED_PIN_UNREADABLE,
                detail=f"pin file invalid: {error.error_count()} errors, first at {error.errors()[0]['loc']}",
            )

    def _missing_tag(self, target: ModelReleasePinSet) -> str | None:
        for repo, tag in target.as_pairs():
            if not self._tags.has_tag(repo, tag):
                return f"release-tag-only: {repo} has no tag {tag} in this host's clone"
        return None

    def _environment(self) -> dict[str, str]:
        env = {
            "ONEX_DEPLOY_REASON": self._overlay.deploy_reason,
            "ONEX_WORKSPACE_CONFIG_ROOT": self._overlay.workspace_config_root,
            "OMNI_HOME": self._overlay.registry_root,
            "LANE_PROBE_HOST": self._overlay.probe_host,
        }
        env.update(self._overlay.environment())
        return env

    def handle(
        self, request: ModelStabilityRefreshCommand
    ) -> ModelStabilityRefreshResult:
        command = request
        record = self._read_pin(command)
        if isinstance(record, ModelStabilityRefreshResult):
            return record
        pin_date = record.date.isoformat()
        if record.lane != self._overlay.lane:
            return self._result(
                command,
                EnumStabilityRefreshOutcome.REFUSED_PIN_UNREADABLE,
                EXIT_REFUSED_PIN_UNREADABLE,
                pin_date=pin_date,
                detail=f"pin file is for lane {record.lane}, overlay is for {self._overlay.lane}",
            )
        target = command.requested or record.pin
        differs = target != record.pin
        missing = self._missing_tag(target)
        if missing:
            return self._result(
                command,
                EnumStabilityRefreshOutcome.REFUSED_NOT_RELEASE_TAG,
                EXIT_REFUSED_NOT_RELEASE_TAG,
                pin_date=pin_date,
                tags=target,
                detail=missing,
            )
        if differs and not command.bump:
            return self._result(
                command,
                EnumStabilityRefreshOutcome.REFUSED_PIN_DIFFERS,
                EXIT_REFUSED_PIN_DIFFERS,
                pin_date=pin_date,
                tags=target,
                detail="the requested tag set is not the committed pin's; bump the pin file in a data pull request or pass bump",
            )
        running = self._applied.read()
        if running == target:
            return self._result(
                command,
                EnumStabilityRefreshOutcome.ALREADY_PINNED,
                EXIT_OK,
                pin_date=pin_date,
                tags=target,
                running=running,
                detail="the lane already runs the pinned set; nothing was run",
            )
        pin_path = self._overlay.pin_file
        if differs:
            pin_path = self._applied.render_pin_file(_pin_text(self._overlay, target))
        argv = [self._overlay.refresh_script, "--pin-file", pin_path]
        if command.execute:
            argv.append("--execute")
        env = self._environment()
        outcome = self._runner.run(argv, env, self._overlay.refresh_timeout_seconds)
        if outcome.return_code != 0:
            return self._result(
                command,
                EnumStabilityRefreshOutcome.REFRESH_FAILED,
                outcome.return_code or EXIT_REFRESH_FAILED,
                pin_date=pin_date,
                tags=target,
                running=running,
                argv=tuple(argv),
                env_names=tuple(sorted(env)),
                stale=differs,
                detail=f"refresh exited {outcome.return_code}: {outcome.output_tail}",
            )
        if command.execute:
            self._applied.write(target)
        return self._result(
            command,
            EnumStabilityRefreshOutcome.REFRESHED
            if command.execute
            else EnumStabilityRefreshOutcome.PLANNED,
            EXIT_OK,
            pin_date=pin_date,
            tags=target,
            running=running,
            argv=tuple(argv),
            env_names=tuple(sorted(env)),
            stale=differs,
            detail=outcome.output_tail,
        )

    def read_health(self, pin_infra_tag: str) -> tuple[bool, str]:
        """The lane's /health: version is the pinned omnibase_infra release, environment the overlay's, wiring healthy."""
        if self._health is None:
            return False, "no health probe configured"
        try:
            reading = self._health.read()
        except OSError as error:
            return False, f"health unreadable: {type(error).__name__}"
        problems = []
        if reading.version != pin_infra_tag.removeprefix("v"):
            problems.append(f"version {reading.version} is not {pin_infra_tag}")
        if reading.environment != self._overlay.expected_environment:
            problems.append(f"environment {reading.environment}")
        if not reading.wiring_healthy:
            problems.append("wiring not healthy")
        return (not problems, "; ".join(problems) or "healthy")


def _pin_text(overlay: ModelStabilityHostOverlay, target: ModelReleasePinSet) -> str:
    lines = [f"lane: {overlay.lane}", "pin:"]
    lines.extend(f"  {repo}: {tag}" for repo, tag in target.as_pairs())
    return "\n".join(lines) + "\n"


def answer_command(
    raw: bytes | str,
    handler: HandlerStabilityLaneRefresh,
    topics: Mapping[str, str],
) -> list[tuple[str, bytes]]:
    """Answer one refresh command from the bus as events: (topic, body) in order, all under its correlation id."""
    try:
        command = ModelStabilityRefreshCommand.model_validate_json(raw)
    except ValidationError as error:
        body = {"failure": f"bad-payload: {error.error_count()} errors"}
        return [(topics["stability_refresh_failed"], json.dumps(body).encode())]
    result = handler.handle(command)
    base = {"correlation_id": result.correlation_id, "lane": result.lane}
    refused = result.outcome in (
        EnumStabilityRefreshOutcome.REFUSED_PIN_DIFFERS,
        EnumStabilityRefreshOutcome.REFUSED_NOT_RELEASE_TAG,
        EnumStabilityRefreshOutcome.REFUSED_PIN_UNREADABLE,
    )
    if refused:
        body = {**base, "outcome": result.outcome.value, "detail": result.detail}
        return [(topics["stability_refresh_refused"], json.dumps(body).encode())]
    accepted = {**base, "tags": dict(result.tags), "pin_date": result.pin_date}
    events = [(topics["stability_refresh_pin_accepted"], json.dumps(accepted).encode())]
    terminal = (
        "stability_refresh_failed"
        if result.outcome is EnumStabilityRefreshOutcome.REFRESH_FAILED
        else "stability_refresh_completed"
    )
    done = {
        **base,
        "outcome": result.outcome.value,
        "exit_code": result.exit_code,
        "detail": result.detail,
    }
    events.append((topics[terminal], json.dumps(done).encode()))
    return events


__all__ = [
    "CONTRACT",
    "EXIT_OK",
    "EXIT_REFRESH_FAILED",
    "EXIT_REFUSED_NOT_RELEASE_TAG",
    "EXIT_REFUSED_PIN_DIFFERS",
    "EXIT_REFUSED_PIN_UNREADABLE",
    "HandlerStabilityLaneRefresh",
    "answer_command",
    "load_topics",
]
