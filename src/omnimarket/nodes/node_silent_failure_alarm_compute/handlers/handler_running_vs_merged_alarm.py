# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide which hosts run something other than merged main, for over an hour (OMN-19998).

THE INCIDENT. On 2026-10-15 a pulse fix merged to the internal plugin repository hours before it ran anywhere.
The host-drift fleet fold already said why: h101's omni plugin sat at 0.4.406 with 0.4.420 merged
for 72 hours, the dispatch venvs on h101, h201 and h202 were five to seven hours behind omnimarket,
h200's plugin tree carried eight hot-patched files and the landing controller ran a branch install.
Every one was an open item in a file on the operator host, and none of it reached the tick.

WHAT IS DECIDED. Each open item of the fold whose kind is something a merge deploys (the omni
plugin, a dispatch venv, an installed controller, a serve checkout, an installed launchd lib) and that has been open
for longer than ``bound_minutes`` (60) is one MISSING finding, named
``running-vs-merged:<surface>``, with the command that brings that surface to main. Clones, disks,
units and plist content are host drift too, but not merged code a deploy carries, so host-drift's
own alerts own them. A launchd-plist item counts only when it names installed files that differ
from the checkout; a diff of the plist's own content is configuration.

A controller that runs a recorded branch install on purpose (install.sh --branch-hours writes
``branch_until`` and ``branch_source`` into landing_install.json) differs from main as expected: while
``branch_until`` is ahead of ``now`` the item is a note naming the hold and its expiry, with no
MISSING finding and no repair. From ``branch_until`` on it is MISSING again. A ``branch_until`` that
does not parse as YYYY-MM-DDTHH:MM:SSZ holds nothing.

WHAT IS HELD. A surface off main that its installer declared and that has not expired is HELD:
listed under ``held``, never a finding, so the tick sends no repair for it. A controller is held
when host-drift observed ``branch-held until <stamp> by <declaration>`` with the stamp after now
and the declaration an INSTALLED-FROM-BRANCHES.txt entry; a plugin tree is held when every changed
file is a sanctioned hot-patch (``hotpatch-declared``). A live hold that names no entry stays
MISSING, and no controller fix ever replaces a live record. A deliberate drift on one surface may
also be acknowledged, bounded by an expiry and by the content the host still observes; the dev
deploy agent's ``/branch-installs`` readback adds a finding for an install the running container
lacks. This copy holds every behaviour of the frozen one (parity test), except the texts its
parity table names.

A fold that cannot be read, or one older than ``fleet_max_age_minutes`` (45, three missed
fifteen-minute checks), is UNKNOWN, never OK: the alarm that checks deploys must not pass because
the reader that sees them stopped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import yaml
from pydantic import ValidationError

from ..models import (
    ModelBranchInstallReading,
    ModelFleetFoldAbsence,
    ModelRunningSurface,
    ModelRunningVsMergedAcknowledgement,
    ModelRunningVsMergedFinding,
    ModelRunningVsMergedHeld,
    ModelRunningVsMergedRequest,
    ModelRunningVsMergedResult,
)

LOOP = "running-vs-merged"
# The installed files of a launchd lib differ from the canonical checkout; anything else on a
# launchd-plist item (a "@@" diff) is the plist's own content.
LIB_MARKERS = ("differs: ", "missing from installed: ", "only in installed: ")
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# The host-drift fold's own words for a live hold, used when the item carries no install record.
FOLD_HOLD = re.compile(r"branch-held until (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)")
ADOPT = (
    'python3 "$(ls -d ~/.claude/plugins/cache/omninode-internal/omni/*/skills/adopt-merged'
    ' | sort -V | tail -1)/scripts/adopt_merged.py"'
)
# The clause host-drift appends to an off-main controller whose install record holds it (OMN-17427).
BRANCH_HELD = re.compile(r"branch-held until (?P<until>\S+) by (?P<by>.*)$")
INSTALL_RECORD = "~/.omninode/landing-controller/landing_install.json"
FLEET_FIX = (
    "read the host-drift run on the operator host: tail ~/.omninode/host-drift/launchd.out.log "
    "and launchctl list ai.omninode.host-drift; its fold is "
    "$OMNI_HOME/.onex_state/host-drift/fleet.json"
)
BRANCH_INSTALLS = "branch-installs"
BRANCH_INSTALL_STATES_MISSING = ("missing", "failed")
BRANCH_INSTALL_FIX = (
    "the dev deploy agent puts a recorded install back on its next idle tick (two minutes) and "
    "after every dev job; if it stays missing, read the agent's journal on the dev lane host "
    "(journalctl --user -u deploy-agent-dev | grep BRANCH-INSTALL-MISSING) and apply by hand with "
    "python -m omnibase_infra.handlers.handler_dev_lane_branch_install apply, from a checkout "
    "that holds the handler, against the agent's DEPLOY_AGENT_STATE_DIR"
)
BRANCH_INSTALL_AGENT_FIX = (
    "read the dev deploy agent's /branch-installs and its unit: curl the agent's health port at "
    "/branch-installs, and journalctl --user -u deploy-agent-dev on the dev lane host. An agent "
    "that does not serve the path predates omnibase_infra's dev-lane branch install record"
)

__all__ = [
    "HandlerRunningVsMergedAlarm",
    "ModelRunningVsMergedAcknowledgement",
    "ModelRunningVsMergedResult",
    "read_acknowledgements",
    "request_from_fleet",
    "with_branch_installs",
]


def _age(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days * 24 + hours}h{mins:02d}m"
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_stamp(value: object) -> datetime | None:
    try:
        return datetime.strptime(str(value), STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _hold_from_item(
    item: Mapping[str, object],
) -> tuple[datetime | None, str]:
    """The recorded branch hold of one fold item: its install record, else the fold's own text."""
    record = item.get("install_record")
    if isinstance(record, Mapping):
        return _parse_stamp(record.get("branch_until")), str(
            record.get("branch_source") or ""
        )
    found = FOLD_HOLD.search(str(item.get("observed") or ""))
    return (_parse_stamp(found.group(1)) if found else None), ""


def _utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _branch_hold(item: ModelRunningSurface) -> tuple[datetime, str] | None:
    """The end and the declaration of a controller's branch hold, from host-drift's clause."""
    match = BRANCH_HELD.search(item.observed)
    if match is None:
        return None
    until = _utc(match["until"])
    return None if until is None else (until, match["by"])


def _held(item: ModelRunningSurface, now: datetime) -> ModelRunningVsMergedHeld | None:
    """The item as a declared, unexpired hold, or None when it is drift (OMN-17427)."""
    host = item.host
    _, _, name = item.key.partition(":")
    if item.kind == "controller":
        hold = _branch_hold(item)
        if hold is None or hold[0] <= now or hold[1].startswith("undeclared"):
            return None
        stamp = _stamp(hold[0])
        return ModelRunningVsMergedHeld(
            loop=f"{LOOP}:controller:{host}:{name}",
            until=stamp,
            why=f"{host} {item.key} branch-held until {stamp} by {hold[1]}",
        )
    if (
        item.kind == "plugin"
        and "hotpatch-declared" in item.observed
        and "tree modified" not in item.observed
    ):
        return ModelRunningVsMergedHeld(
            loop=f"{LOOP}:plugin:{host}", why=f"{host} {item.key} {item.observed}"
        )
    return None


def _surface(
    item: ModelRunningSurface,
    now: datetime | None = None,
    homes: Mapping[str, str] | None = None,
) -> tuple[str, str] | None:
    """The surface name and its fix, or None when the item is not merged code a deploy carries.

    Without ``now`` no branch hold counts as live, so a controller gets the never-replace fix.
    ``homes`` is the OMNI_HOME each host declares; a host with no row reads its own
    environment.
    """
    host = item.host
    _, _, name = item.key.partition(":")
    if item.kind == "plugin":
        if "hotpatch" in item.observed:
            return (
                f"plugin:{host}",
                f"on {host}: {ADOPT} backups --since {_stamp(item.first_seen_at)}, land or drop "
                "each hot-patched file, then the host-drift repair-plugin action reinstalls the "
                "shipped revision",
            )
        if "plugin-behind:" in item.observed or "plugin-stale:" in item.observed:
            # Behind main at the released version: a marketplace update has nothing to fetch until
            # the bump PR merges, so the fix is the tree install, keeping branch fixes (OMN-20497).
            # install-tree takes no --lane (adopt_merged.py's parser declares --branch, --commit,
            # --clone, --version-dir, --dry-run, --with-installed, --ledger, --no-ledger), and its
            # default clone is read from OMNI_HOME, so the line runs with the host's own.
            home = (homes or {}).get(host, "$OMNI_HOME")
            return (
                f"plugin:{host}",
                f"on {host}: OMNI_HOME={home} {ADOPT} install-tree --branch main --with-installed "
                "(the host-drift install-plugin-tree action; read why it has not run in the sync "
                "hub's state)",
            )
        return (
            f"plugin:{host}",
            f"on {host}: claude plugin marketplace update omninode-internal && "
            f"claude plugin update {name} (the host-drift update-plugin action; read why it has "
            "not run in the sync hub's state)",
        )
    if item.kind == "dispatch-venv":
        return (
            f"dispatch-venv:{host}:{name}",
            f'on {host}: bash "$OMNI_HOME/omnibase_infra/scripts/reconcile-workspace-venvs.sh" '
            f"for the root holding {name} (the host-drift update-venv action; read its last fix "
            "outcome in the sync hub's state)",
        )
    if item.kind == "controller":
        hold = _branch_hold(item)
        # A live hold reaching here is undeclared: a declared one is held, never a finding.
        if hold is not None and now is not None and hold[0] > now:
            stamp = _stamp(hold[0])
            return (
                f"controller:{host}:{name}",
                f"on {host}: a live branch_until ({stamp}) in {INSTALL_RECORD} names no "
                "INSTALLED-FROM-BRANCHES.txt entry: the lane that installed it appends its entry "
                f"(lane, branch, commit, branch_until), or the hold runs out at {stamp}. Never "
                "replace or move landing_install.json while branch_until is live.",
            )
        return (
            f"controller:{host}:{name}",
            f"on {host}: if {INSTALL_RECORD} exists, never replace it: the next controller tick "
            "refreshes from origin/main through its own install path "
            "(landing_g_act.refresh_install); read why it has not in the controller log. Only "
            "when landing_install.json is absent, move the newest "
            "landing_install.json.moved-aside.* back.",
        )
    if item.kind == "serve-checkout":
        return (
            f"serve-checkout:{host}:{name}",
            f"on {host}: the host-drift advance-serve action reruns the node installer of {name} "
            "with --advance (it fetches the serve checkout's ref, syncs the venv and restarts the "
            "unit); read why it has not run in the sync hub's state",
        )
    if item.kind == "launchd-plist" and any(
        marker in item.observed for marker in LIB_MARKERS
    ):
        return (
            f"launchd-lib:{host}:{name}",
            f"on {host}: land or drop any hot-patched file, then rerun the installer of {name} "
            f"from the internal tooling canonical clone at origin/main ({ADOPT} run reruns the "
            "installers it lists)",
        )
    return None


# A missing fold file is not a failed run (OMN-23275): the host-drift manifest gives the unit to named
# hosts only, the unit writes the fold on the host it runs on, and nothing copies that file to another
# host. So the finding names the declaring hosts and whether this one is among them, and its FIX is an
# action that exists on the host it names. Ported from the internal tooling repository (OMN-17427);
# the unit, manifest, installer and ticket words ride on the request, read by the caller from its overlay.


def describe_fold_absence(absence: ModelFleetFoldAbsence) -> tuple[str, str]:
    """The ``why`` and ``fix`` of the running-vs-merged finding for a fold file that is absent."""
    unit, manifest, ticket = absence.unit, absence.manifest, absence.ticket
    head = f"host-drift fleet fold {absence.path} does not exist on {absence.this_host}"
    if not absence.owners:
        return (
            f"{head}: no host in {manifest} declares {unit}, so the fold has no producer",
            f"declare a host for {unit} in {manifest} under {ticket}; until then this loop "
            "cannot be judged",
        )
    owners = ", ".join(absence.owners)
    if absence.declared_here and absence.plist_installed:
        return (
            f"{head}: {unit} is declared for this host and its plist is installed, but it has "
            "written no fold",
            f"read its run on this host: tail ~/.omninode/host-drift/launchd.out.log and "
            f"launchctl list {unit}",
        )
    if absence.declared_here:
        return (
            f"{head}: {unit} is declared for this host ({owners}) and its plist is not installed",
            f"install it with the host drift installer (the operator runs it): "
            f"{absence.installer}",
        )
    return (
        f"{head}: {unit} is declared only for {owners} by {manifest}; {absence.this_host} is not "
        "in the manifest, nothing here writes the fold and no copy of it reaches this host",
        f"read the fold on a declaring host ({owners}): launchctl list {unit} and tail "
        f"~/.omninode/host-drift/launchd.out.log there; to judge this loop from "
        f"{absence.this_host} the operator declares it in {manifest} ({ticket}) and runs "
        f"{absence.installer}",
    )


class HandlerRunningVsMergedAlarm:
    """Which surfaces run something other than merged main past the bound."""

    def handle(
        self, request: ModelRunningVsMergedRequest
    ) -> ModelRunningVsMergedResult:
        """The decision for one fleet fold."""
        if not request.fleet_readable and request.fleet_absence is not None:
            why, fix = describe_fold_absence(request.fleet_absence)
            return ModelRunningVsMergedResult(
                state="UNKNOWN",
                findings=(
                    ModelRunningVsMergedFinding(
                        level="unknown", loop=LOOP, why=why, fix=fix
                    ),
                ),
            )
        fleet, unknown, held = self._fleet_findings(request)
        acknowledged = tuple(
            fleet[loop] for loop in sorted(fleet) if fleet[loop].level == "note"
        )
        missing = {
            loop: finding
            for loop, finding in fleet.items()
            if finding.level == "missing"
        }
        branch_missing, branch_unknown = self._branch_install_findings(request)
        missing = {**missing, **branch_missing}
        unknown = [*unknown, *branch_unknown]
        if missing:
            return ModelRunningVsMergedResult(
                state="MISSING",
                findings=(*(missing[loop] for loop in sorted(missing)), *unknown),
                acknowledged=acknowledged,
                held=held,
            )
        if unknown:
            return ModelRunningVsMergedResult(
                state="UNKNOWN",
                findings=tuple(unknown),
                acknowledged=acknowledged,
                held=held,
            )
        return ModelRunningVsMergedResult(
            state="OK", acknowledged=acknowledged, held=held
        )

    def _fleet_findings(
        self, request: ModelRunningVsMergedRequest
    ) -> tuple[
        dict[str, ModelRunningVsMergedFinding],
        list[ModelRunningVsMergedFinding],
        tuple[ModelRunningVsMergedHeld, ...],
    ]:
        if not request.fleet_readable:
            return (
                {},
                [
                    self._unknown(
                        "host-drift fleet fold unreadable: "
                        f"{request.unreadable_why or 'no reason given'}"
                    )
                ],
                (),
            )
        if request.folded_at is None:
            return {}, [self._unknown("host-drift fleet fold names no folded_at")], ()
        stale = request.now - request.folded_at
        if stale > timedelta(minutes=request.fleet_max_age_minutes):
            return (
                {},
                [
                    self._unknown(
                        f"host-drift fleet fold is {_age(stale)} old "
                        f"(folded {_stamp(request.folded_at)}), "
                        f"over {request.fleet_max_age_minutes}m"
                    )
                ],
                (),
            )
        bound = timedelta(minutes=request.bound_minutes)
        findings: dict[str, ModelRunningVsMergedFinding] = {}
        holds: dict[str, ModelRunningVsMergedHeld] = {}
        homes = {row.host: row.home for row in request.host_homes}
        for item in request.items:
            age = request.now - item.first_seen_at
            if age <= bound:
                continue
            hold = _held(item, request.now)
            if hold is not None:
                holds.setdefault(hold.loop, hold)
                continue
            surface = _surface(item, request.now, homes)
            if surface is None:
                continue
            name, fix = surface
            loop = f"{LOOP}:{name}"
            why = (
                f"{item.host} {item.key} differs from merged main for {_age(age)} "
                f"(since {_stamp(item.first_seen_at)}): {item.verdict} {item.observed}"
            )
            if (
                item.kind == "controller"
                and item.branch_until is not None
                and request.now < item.branch_until
                and _branch_hold(item) is None
            ):
                # A recorded branch install on purpose (install.sh --branch-hours writes
                # branch_until and branch_source into landing_install.json): an expected
                # difference, a note with no repair, while branch_until is ahead.
                source = f" of {item.branch_source}" if item.branch_source else ""
                findings.setdefault(
                    loop,
                    ModelRunningVsMergedFinding(
                        level="note",
                        loop=loop,
                        why=(
                            f"{item.host} {item.key} runs a recorded branch install{source} until "
                            f"{_stamp(item.branch_until)} (landing_install.json branch_until); "
                            f"expected difference, no repair: {why}"
                        ),
                        fix="",
                    ),
                )
                continue
            # the latest-expiring entry for the surface wins, so a renewal beside an old entry holds
            ack = max(
                (ack for ack in request.acknowledgements if ack.surface == name),
                key=lambda ack: ack.expires_at,
                default=None,
            )
            if ack is not None:
                if request.now >= ack.expires_at:
                    why += f" (acknowledgement by {ack.by} expired {_stamp(ack.expires_at)})"
                elif ack.while_observed not in item.observed:
                    why += (
                        f" (acknowledged drift no longer matches: {ack.while_observed!r} "
                        "is not in what the host observes)"
                    )
                else:
                    findings.setdefault(
                        loop,
                        ModelRunningVsMergedFinding(
                            level="note",
                            loop=loop,
                            why=(
                                f"acknowledged by {ack.by}: {ack.why} (cites {ack.cites}; "
                                f"expires {_stamp(ack.expires_at)}); {why}"
                            ),
                            fix="",
                        ),
                    )
                    continue
            findings.setdefault(
                loop,
                ModelRunningVsMergedFinding(
                    level="missing", loop=loop, why=why, fix=fix
                ),
            )
        return findings, [], tuple(holds[loop] for loop in sorted(holds))

    def _branch_install_findings(
        self, request: ModelRunningVsMergedRequest
    ) -> tuple[
        dict[str, ModelRunningVsMergedFinding], list[ModelRunningVsMergedFinding]
    ]:
        """A fix installed into the dev lane from its branch that the running container lacks.

        The deploy agent re-applies a recorded install within two minutes, so an install its last
        readback calls missing is a defect now, with no hour of grace. A readback that cannot be
        read, that is older than ``branch_installs_max_age_minutes`` or that leaves a declared
        install unread is UNKNOWN: the check must not pass because the reader stopped.
        """
        if not request.branch_installs_requested:
            return {}, []
        unknown_loop = f"{LOOP}:{BRANCH_INSTALLS}"

        def unknown(
            why: str,
        ) -> tuple[
            dict[str, ModelRunningVsMergedFinding], list[ModelRunningVsMergedFinding]
        ]:
            return {}, [
                ModelRunningVsMergedFinding(
                    level="unknown",
                    loop=unknown_loop,
                    why=why,
                    fix=BRANCH_INSTALL_AGENT_FIX,
                )
            ]

        if not request.branch_installs_readable:
            return unknown(
                "dev deploy agent /branch-installs unreadable: "
                f"{request.branch_installs_why or 'no reason given'}"
            )
        declared = set(request.branch_installs_declared)
        if not declared:
            return {}, []
        if request.branch_installs_checked_at is None:
            return unknown(
                f"dev deploy agent declares {len(declared)} branch install(s) and has not read "
                "any back"
            )
        stale = request.now - request.branch_installs_checked_at
        if stale > timedelta(minutes=request.branch_installs_max_age_minutes):
            return unknown(
                f"dev deploy agent's branch install readback is {_age(stale)} old (read "
                f"{_stamp(request.branch_installs_checked_at)}), over "
                f"{request.branch_installs_max_age_minutes}m"
            )
        findings: dict[str, ModelRunningVsMergedFinding] = {}
        read = {r.install_key for r in request.branch_installs}
        for reading in request.branch_installs:
            if reading.state not in BRANCH_INSTALL_STATES_MISSING:
                continue
            loop = f"{LOOP}:{BRANCH_INSTALLS}:{reading.install_key}"
            files = "; ".join(reading.unwhole) or "no file detail"
            findings[loop] = ModelRunningVsMergedFinding(
                level="missing",
                loop=loop,
                why=(
                    f"dev lane {reading.repo}#{reading.pr} installed from its branch is "
                    f"{reading.state} in the running container (agent readback "
                    f"{_stamp(request.branch_installs_checked_at)}): {files}"
                ),
                fix=BRANCH_INSTALL_FIX,
            )
        unread = sorted(declared - read)
        if unread:
            return findings, [
                ModelRunningVsMergedFinding(
                    level="unknown",
                    loop=unknown_loop,
                    why=f"declared branch install(s) with no readback: {', '.join(unread)}",
                    fix=BRANCH_INSTALL_AGENT_FIX,
                )
            ]
        return findings, []

    @staticmethod
    def _unknown(why: str) -> ModelRunningVsMergedFinding:
        return ModelRunningVsMergedFinding(
            level="unknown", loop=LOOP, why=why, fix=FLEET_FIX
        )


def with_branch_installs(
    request: ModelRunningVsMergedRequest, payload: object, why: str = ""
) -> ModelRunningVsMergedRequest:
    """The request with the dev deploy agent's ``/branch-installs`` payload folded in.

    ``payload`` is the parsed JSON body, or ``None`` with ``why`` when it could not be fetched.
    A body the agent marks unreadable (``installs: null``) or that is not the shape the agent
    serves is unreadable, never an empty list."""
    base = {"branch_installs_requested": True}
    if payload is None:
        return request.model_copy(
            update={
                **base,
                "branch_installs_readable": False,
                "branch_installs_why": why,
            }
        )
    try:
        if not isinstance(payload, Mapping):
            raise TypeError("not an object")
        if "installs" in payload and payload["installs"] is None:
            raise ValueError(
                f"the agent says: {payload.get('reason') or 'no reason given'}"
            )
        declared_raw = payload["declared"]
        if not isinstance(declared_raw, list):
            raise TypeError("declared is not a list")
        declared = tuple(str(d["id"]) for d in declared_raw)
        last = payload.get("last_check")
        checked_at: datetime | None = None
        readings: list[ModelBranchInstallReading] = []
        if isinstance(last, Mapping):
            checked_at = datetime.fromisoformat(
                str(last["checked_at"]).replace("Z", "+00:00")
            )
            for row in last.get("readbacks") or ():
                files = row.get("files") or ()
                readings.append(
                    ModelBranchInstallReading(
                        install_key=str(row["install_key"]),
                        repo=str(row["repo"]),
                        pr=int(row["pr"]),
                        state=str(row["state"]),
                        unwhole=tuple(
                            f"{f['path']} expected {str(f['expected_sha256'])[:12]} observed "
                            f"{str(f['observed_sha256'])[:12] or 'absent'}"
                            for f in files
                            if f.get("state") not in ("present", "reapplied")
                        ),
                    )
                )
        return request.model_copy(
            update={
                **base,
                "branch_installs_declared": declared,
                "branch_installs_checked_at": checked_at,
                "branch_installs": tuple(readings),
            }
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        return request.model_copy(
            update={
                **base,
                "branch_installs_readable": False,
                "branch_installs_why": f"malformed: {' '.join(str(exc).split())[:200]}",
            }
        )


def read_acknowledgements(text: str) -> tuple[ModelRunningVsMergedAcknowledgement, ...]:
    """Read typed acknowledgements; malformed content must not silently disable the check."""
    document = yaml.safe_load(text)
    if not isinstance(document, Mapping) or not isinstance(
        document.get("acknowledgements"), list
    ):
        raise ValueError("acknowledgements must be a list in a mapping")
    return tuple(
        ModelRunningVsMergedAcknowledgement.model_validate(entry)
        for entry in document["acknowledgements"]
    )


def request_from_fleet(
    fleet: Mapping[str, object],
    now: datetime,
    *,
    bound_minutes: int = 60,
    acknowledgements: tuple[ModelRunningVsMergedAcknowledgement, ...] = (),
) -> ModelRunningVsMergedRequest:
    """The request for a host-drift fleet fold payload (fleet.json).

    A malformed fold is unreadable."""
    try:
        hosts = fleet.get("hosts")
        if not isinstance(hosts, list):
            raise TypeError("hosts is not a list")
        items: list[ModelRunningSurface] = []
        for host in hosts:
            if not isinstance(host, Mapping):
                raise TypeError("a host is not an object")
            for item in host.get("open") or ():
                if not isinstance(item, Mapping):
                    raise TypeError("an open item is not an object")
                until, source = _hold_from_item(item)
                items.append(
                    ModelRunningSurface(
                        host=str(host["host"]),
                        key=str(item["key"]),
                        kind=str(item["kind"]),
                        observed=str(item.get("observed") or ""),
                        first_seen_at=item["first_seen_at"],
                        verdict=str(item.get("verdict") or "DRIFT"),
                        branch_until=until,
                        branch_source=source,
                    )
                )
        return ModelRunningVsMergedRequest.model_validate(
            {
                "now": now,
                "folded_at": fleet.get("folded_at"),
                "items": tuple(items),
                "bound_minutes": bound_minutes,
                "acknowledgements": acknowledgements,
            }
        )
    except (KeyError, TypeError, ValidationError) as exc:
        why = " ".join(str(exc).split())[:200]
        return ModelRunningVsMergedRequest(
            now=now,
            fleet_readable=False,
            unreadable_why=f"malformed: {why}",
            acknowledgements=acknowledgements,
        )
