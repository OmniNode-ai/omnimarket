# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Converge every declared lab daemon on this host to its source branch tip (OMN-19998).

For each daemon the overlay declares on this host, read the installed revision and the source
branch tip. A lag, or a branch install whose expiry has passed, runs the daemon's own installer;
an unexpired branch install is held; a held installer lock skips the tick. An installer that
refuses, an install that leaves the lag, or an unreadable revision publishes an alarm event naming
the blocker and dispatches one fix lane through the remote-lane runner (once per blocker, not once
per tick). Every tick writes the host's drift rows for the throughput tick's deploy-drift line.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.models.model_lab_daemon_drift import ModelLabDaemonDrift
from omnimarket.nodes.node_lab_daemon_sync_effect.models.model_lab_daemon_sync import (
    EnumDaemonDecision,
    EnumDaemonVerdict,
    ModelDaemonDeclaration,
    ModelDaemonFacts,
    ModelDaemonOutcome,
    ModelLabDaemonInstallRefused,
    ModelLabDaemonSyncCommand,
    ModelLabDaemonSyncResult,
    ModelRevisionProbe,
)
from omnimarket.nodes.node_lab_daemon_sync_effect.protocols import (
    ProtocolLabDaemonEventPublisher,
    ProtocolLabDaemonHost,
)

PROBE_TIMEOUT_S = 120
FIX_LANE_TIMEOUT_S = 300
_TAIL = 400
_SETTLED = {"current", "installed"}


def parse_stamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def decide_daemon(facts: ModelDaemonFacts, now: str) -> EnumDaemonDecision:
    """The pure decision for one daemon from what its probes read."""
    if facts.installed_revision is None or facts.source_revision is None:
        return "unreadable"
    if facts.branch_until is not None:
        try:
            expiry = parse_stamp(facts.branch_until)
        except ValueError:
            return "unreadable"
        if expiry > parse_stamp(now):
            return "branch_held"
        if facts.installed_revision != facts.source_revision:
            return "branch_expired"
    if facts.installed_revision == facts.source_revision:
        return "current"
    return "lagging"


def _tail(text: str) -> str:
    text = " ".join(text.split())
    return text[-_TAIL:]


def _dotted(data: object, key: str | None) -> object:
    for part in key.split(".") if key else []:
        if isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        elif isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data


def _lane_name(daemon: str, host: str) -> str:
    return "daemon-sync-fix-" + re.sub(r"[^a-z0-9-]+", "-", f"{daemon}-{host}".lower())


class HandlerLabDaemonSync:
    """Host-timer effect: probe, decide, install, alarm, record."""

    def __init__(
        self,
        host: ProtocolLabDaemonHost,
        publisher: ProtocolLabDaemonEventPublisher,
        refused_topic: str,
        completed_topic: str,
    ) -> None:
        self._host = host
        self._publisher = publisher
        self._refused_topic = refused_topic
        self._completed_topic = completed_topic

    def _probe(self, probe: ModelRevisionProbe) -> tuple[str | None, str | None]:
        if probe.kind == "json_file":
            data = self._host.read_json(probe.path or "")
            if data is None:
                return None, f"{probe.path} absent or not JSON"
            value = _dotted(data, probe.key)
            if not isinstance(value, str) or not value.strip():
                return None, f"{probe.path} has no {probe.key}"
            return value.strip(), None
        try:
            rc, out, err = self._host.run(probe.argv, PROBE_TIMEOUT_S)
        except OSError as exc:
            return None, f"{probe.argv[0]}: {exc}"
        if rc != 0 or not out.strip():
            return None, f"rc={rc} {_tail(err) or 'no output'}"
        if probe.key is None:
            return out.strip().splitlines()[-1].strip(), None
        try:
            value = _dotted(json.loads(out), probe.key)
        except ValueError:
            return None, f"{probe.argv[0]} stdout is not JSON"
        if not isinstance(value, str) or not value.strip():
            return None, f"{probe.argv[0]} stdout has no {probe.key}"
        return value.strip(), None

    def _facts(self, daemon: ModelDaemonDeclaration) -> ModelDaemonFacts:
        installed, installed_error = self._probe(daemon.installed)
        source, source_error = self._probe(daemon.source)
        until = branch_source = None
        if daemon.branch_record is not None:
            record = self._host.read_json(daemon.branch_record.path) or {}
            raw_until = record.get(daemon.branch_record.until_key)
            until = None if raw_until is None else str(raw_until)
            raw_source = record.get(daemon.branch_record.source_key)
            branch_source = None if raw_source is None else str(raw_source)
        return ModelDaemonFacts(
            installed_revision=installed,
            installed_error=installed_error,
            source_revision=source,
            source_error=source_error,
            branch_until=until,
            branch_source=branch_source,
        )

    def _install(
        self, daemon: ModelDaemonDeclaration, facts: ModelDaemonFacts
    ) -> tuple[EnumDaemonVerdict, str | None, str | None]:
        """(verdict, installed revision after, blocker)."""
        if any(self._host.lock_busy(lock) for lock in daemon.locks):
            return "busy", facts.installed_revision, None
        try:
            rc, out, err = self._host.run(daemon.installer, daemon.installer_timeout_s)
        except OSError as exc:
            return (
                "refused",
                facts.installed_revision,
                f"installer did not start: {exc}",
            )
        after, after_error = self._probe(daemon.installed)
        if rc != 0:
            return (
                "refused",
                after,
                f"installer rc={rc}: {_tail(err) or _tail(out) or 'no output'}",
            )
        if after is not None and after == facts.source_revision:
            return "installed", after, None
        if any(self._host.lock_busy(lock) for lock in daemon.locks):
            # The installer met a tick that took its locks after the probe; next tick retries.
            return "busy", after, None
        return (
            "refused",
            after,
            f"installer rc=0 but installed is still {after or after_error} "
            f"(source {facts.source_revision})",
        )

    def _brief(
        self,
        lane: str,
        daemon: ModelDaemonDeclaration,
        command: ModelLabDaemonSyncCommand,
        facts: ModelDaemonFacts,
        blocker: str,
    ) -> str:
        return "\n".join(
            [
                f"# Lane {lane}: {daemon.name} on {command.host} does not run {daemon.source_branch}",
                "",
                f"The lab daemon sync could not converge `{daemon.name}` on host `{command.host}` "
                f"at {command.now}.",
                "",
                f"- installed revision: {facts.installed_revision or facts.installed_error}",
                f"- {daemon.source_branch} tip: {facts.source_revision or facts.source_error}",
                f"- installer: `{' '.join(daemon.installer)}`",
                f"- blocker: {blocker}",
                "",
                "Task: remove the blocker so the daemon's own installer converges it to "
                f"{daemon.source_branch}, through a PR when the installer itself is wrong. Never install "
                "a branch by hand to make this alarm stop; the next host tick reruns the installer and "
                "clears the alarm once the installed revision equals the tip.",
                "",
            ]
        )

    def handle(self, command: ModelLabDaemonSyncCommand) -> ModelLabDaemonSyncResult:
        correlation_id = str(uuid.uuid4())
        state_path = Path(command.state_dir) / f"{command.host}.json"
        previous_doc = self._host.read_json(str(state_path)) or {}
        recorded = previous_doc.get("daemons")
        previous: dict[str, Any] = dict(recorded) if isinstance(recorded, dict) else {}
        now = parse_stamp(command.now)
        outcomes: list[ModelDaemonOutcome] = []
        drifts: list[ModelLabDaemonDrift] = []
        diagnostics: list[str] = []
        state: dict[str, dict[str, Any]] = {}
        refused = False
        for daemon in command.overlay.daemons:
            if "*" not in daemon.hosts and command.host not in daemon.hosts:
                continue
            if daemon.present_when and not self._host.exists(daemon.present_when):
                continue
            before = dict(previous.get(daemon.name) or {})
            facts = self._facts(daemon)
            decision = decide_daemon(facts, command.now)
            installed = facts.installed_revision
            blocker: str | None = None
            verdict: EnumDaemonVerdict
            if decision == "current":
                verdict = "current"
            elif decision == "branch_held":
                verdict = "branch_held"
            elif decision == "unreadable":
                verdict = "refused"
                if facts.installed_revision is None:
                    blocker = f"installed revision unreadable: {facts.installed_error}"
                elif facts.source_revision is None:
                    blocker = f"source revision unreadable: {facts.source_error}"
                else:
                    blocker = f"branch expiry {facts.branch_until!r} does not parse"
            elif command.mode == "check":
                verdict = "lagging"
            else:
                verdict, installed, blocker = self._install(daemon, facts)

            lagging_since = command.now
            if before.get("verdict") not in (None, *_SETTLED) and isinstance(
                before.get("lagging_since"), str
            ):
                lagging_since = before["lagging_since"]
            entry: dict[str, Any] = {
                "decision": decision,
                "verdict": verdict,
                "installed_revision": installed,
                "source_revision": facts.source_revision,
                "branch_until": facts.branch_until,
                "blocker": blocker,
            }
            fix_lane: str | None = None
            if verdict == "refused" and blocker is not None:
                refused = True
                fix_lane = _lane_name(daemon.name, command.host)
                dispatched, fix_error = False, None
                last_at = before.get("fix_lane_at")
                recent = isinstance(last_at, str) and now - parse_stamp(
                    last_at
                ) < timedelta(hours=command.overlay.fix_lane_repeat_hours)
                same = before.get("fix_lane_blocker") == blocker and recent
                if command.mode == "repair" and not same:
                    brief = (
                        Path(command.state_dir)
                        / "briefs"
                        / f"{fix_lane}-{command.now.replace(':', '')}.md"
                    )
                    self._host.write_text(
                        str(brief),
                        self._brief(fix_lane, daemon, command, facts, blocker),
                    )
                    argv = [
                        part.format(
                            lane=fix_lane,
                            brief=str(brief),
                            daemon=daemon.name,
                            host=command.host,
                        )
                        for part in command.overlay.fix_lane
                    ]
                    try:
                        rc, _out, err = self._host.run(argv, FIX_LANE_TIMEOUT_S)
                        dispatched = rc == 0
                        if not dispatched:
                            fix_error = f"rc={rc} {_tail(err)}"
                    except OSError as exc:
                        fix_error = str(exc)
                    if dispatched:
                        entry["fix_lane_blocker"] = blocker
                        entry["fix_lane_at"] = command.now
                    else:
                        diagnostics.append(
                            f"{daemon.name}: fix lane not dispatched: {fix_error}"
                        )
                elif same:
                    entry["fix_lane_blocker"] = before.get("fix_lane_blocker")
                    entry["fix_lane_at"] = before.get("fix_lane_at")
                self._publisher.publish(
                    self._refused_topic,
                    ModelLabDaemonInstallRefused(
                        correlation_id=correlation_id,
                        host=command.host,
                        daemon=daemon.name,
                        blocker=blocker,
                        installed_revision=installed,
                        source_revision=facts.source_revision,
                        lagging_since=lagging_since,
                        fix_lane=fix_lane,
                        fix_lane_dispatched=dispatched,
                        fix_lane_error=fix_error,
                    ),
                )
            if verdict not in _SETTLED:
                entry["lagging_since"] = lagging_since
                drifts.append(
                    ModelLabDaemonDrift(
                        daemon=daemon.name,
                        host=command.host,
                        installed_revision=installed,
                        source_revision=facts.source_revision,
                        lagging_since=lagging_since,
                        branch_until=facts.branch_until
                        if verdict == "branch_held"
                        else None,
                        blocker=blocker,
                    )
                )
            state[daemon.name] = entry
            outcomes.append(
                ModelDaemonOutcome(
                    daemon=daemon.name,
                    host=command.host,
                    decision=decision,
                    verdict=verdict,
                    installed_revision=installed,
                    source_revision=facts.source_revision,
                    branch_until=facts.branch_until,
                    blocker=blocker,
                    fix_lane=fix_lane,
                )
            )
        result = ModelLabDaemonSyncResult(
            correlation_id=correlation_id,
            host=command.host,
            at=command.now,
            mode=command.mode,
            daemons=outcomes,
            drifts=drifts,
            diagnostics=diagnostics,
            exit_code=1 if refused else 0,
        )
        self._host.write_text(
            str(state_path),
            json.dumps(
                {
                    "host": command.host,
                    "written": command.now,
                    "daemons": state,
                    "drifts": [d.model_dump(mode="json") for d in drifts],
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )
        self._publisher.publish(self._completed_topic, result)
        return result
