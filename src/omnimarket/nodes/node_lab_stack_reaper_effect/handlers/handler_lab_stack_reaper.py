# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One bounded, lease-based reaper pass.

A TERMINAL closes exactly the CLAIM it names. File order, ticket membership and clock order
are never ownership evidence. All subprocesses, including ledger appends, use the injected runner.
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.claim_index import ClaimStoreIncompleteError, window_sources

from ..models import (
    ModelLabStackReaperRequest,
    ModelLabStackReaperResult,
    ProjectDecision,
)
from .lab_stack_reaper_leases import (
    LeaseRecord,
    LeaseStore,
    atomic_json,
    digest,
)
from .lab_stack_support import (
    DOCKER_HOST_RE,
    PROJECT_LABEL,
    STAMP_RE,
    Completed,
    Runner,
    acting_user,
    append_argv,
    cell,
    cell_value,
    cells_of,
    config_files,
    foreign_config_paths,
    load_declared,
    project_names,
    run_process,
    tail,
    tickets_of,
    token_in,
)

ClaimKey = tuple[str, str]
HARNESS_LABEL = re.compile(r"^com\.omninode\.([a-z0-9_-]+)-harness$")

TICKET_INVENTORY_SCRIPT = """import json, os, socket, sys
from datetime import datetime, timezone
from pathlib import Path
root = Path(os.environ['OMNI_HOME']).resolve()
paths = (root / 'omni_worktrees', Path.home() / 'lab-run/runs',
         *(Path(extra) for extra in sys.argv[1:]))
entries = []
for path in paths:
    try:
        path.stat()
    except FileNotFoundError:
        path.parent.stat()
        continue
    entries.extend(entry.name for entry in path.iterdir())
print(json.dumps({'host': socket.gethostname(), 'at': datetime.now(timezone.utc).isoformat(),
                  'roots_checked': len(paths), 'entries': entries}))
"""

# Executed on the ledger host by the installed timer, on an interpreter that has omnimarket. It
# uses the shared claim window reader and the writer's mutex, so a concurrent archive roll cannot
# hide an owning CLAIM. Arguments: the ledger path relative to OMNI_HOME, then optionally the ssh
# target of the ticket inventory host and the extra worktree roots that host inventories. The lane
# census path comes from ONEX_LANE_MANIFEST on the ledger host.
SNAPSHOT_SCRIPT = """import json, os, sys, shlex, subprocess
from datetime import UTC, datetime
from pathlib import Path
from omnimarket.handlers.claim_index import window_sources
from omnimarket.nodes.node_lab_stack_reaper_effect.handlers.ledger_writer_lock import (
    LedgerWriterLock,
)
ledger = Path(os.environ['OMNI_HOME']) / sys.argv[1]
manifest = Path(os.environ['ONEX_LANE_MANIFEST'])
inventory = None
if len(sys.argv) > 2:
    probe = shlex.join(['python3', '-c', __TICKET_PROBE__, *sys.argv[3:]])
    done = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                           sys.argv[2], probe], capture_output=True, text=True,
                          check=True, timeout=30)
    inventory = json.loads(done.stdout)
now = datetime.now(UTC)
with LedgerWriterLock(ledger, 30):
    sources = window_sources(ledger, ledger.name, now=now, hours=336)
    print(json.dumps({'at': now.isoformat(), 'census': manifest.read_text(),
                     'ticket_inventory': inventory, 'sources': [
        {'name': s.name, 'text': s.text} for s in sources]}))
""".replace("__TICKET_PROBE__", repr(TICKET_INVENTORY_SCRIPT))


def closed_claims(ledger: Path, now: datetime) -> set[ClaimKey]:
    """Only unambiguous CLAIM identities with an explicit closing TERMINAL are closed."""
    claims: Counter[ClaimKey] = Counter()
    terminals: set[ClaimKey] = set()
    cutoff = now - timedelta(hours=336)
    for source in window_sources(ledger, ledger.name, now=now, hours=336):
        for line in source.text.splitlines():
            cells = cells_of(line)
            if len(cells) < 3 or not STAMP_RE.fullmatch(cells[0]):
                continue
            try:
                stamp = datetime.strptime(cells[0], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=UTC
                )
            except ValueError:
                continue
            if stamp < cutoff:
                continue
            lane = cell_value(cells, "lane").lower()
            if not lane:
                continue
            if cells[1].upper() == "CLAIM":
                claims[lane, cells[0]] += 1
            elif cells[1].upper() == "TERMINAL":
                terminals.add((lane, cell_value(cells, "closes-CLAIM")))
    return {key for key in terminals if claims[key] == 1}


def open_lanes(record: LeaseRecord, closed: set[ClaimKey]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                lease.lane
                for lease in record.leases
                if (lease.lane, lease.claim) not in closed
            }
        )
    )


STANDING_STACKS_ENV = "ONEX_LAB_STANDING_STACKS"
OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
STANDING_STACKS_SCHEMA = "lab-standing-stacks.v1"


def standing_stacks_path() -> Path | None:
    """The explicit pointer, else the first overlay root that holds the declaration."""
    pointer = os.environ.get(STANDING_STACKS_ENV)
    if pointer is not None:
        return Path(pointer)
    for root in os.environ.get(OVERLAY_ROOTS_ENV, "").split(os.pathsep):
        candidate = Path(root) / "node_lab_stack_reaper_effect" / "standing_stacks.yaml"
        if root and candidate.is_file():
            return candidate
    return None


def standing_projects(path: Path | None = None) -> set[str]:
    """The host infrastructure stacks no lane leases; none declared is none, unreadable refuses."""
    path = path or standing_stacks_path()
    if path is None:
        return set()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    entries = data.get("projects") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("schema") != STANDING_STACKS_SCHEMA
        or not isinstance(entries, list)
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("project"), str)
            or not item["project"]
            for item in entries
        )
    ):
        raise ValueError(f"{path}: invalid standing stack declaration")
    return {item["project"] for item in entries}


def declared(request: ModelLabStackReaperRequest) -> tuple[set[str], set[str], str]:
    # Keep the established census discovery and built-in protection in one implementation.
    projects, names, source = load_declared(request)
    return projects | standing_projects(), names, source


class HandlerLabStackReaper:
    def __init__(
        self, run: Runner = run_process, evidence_ledger: Path | None = None
    ) -> None:
        self._run = run
        self._evidence_ledger = evidence_ledger

    def handle(self, request: ModelLabStackReaperRequest) -> ModelLabStackReaperResult:
        if request.verb == "reap" and request.ledger_snapshot_command:
            return self._with_snapshot(request)
        if request.verb == "lease":
            try:
                declared_projects, _, _ = declared(request)
                if request.project in declared_projects:
                    raise ValueError(f"{request.project}: declared lane project")
                LeaseStore(request.lease_dir).add(
                    request.project,
                    request.lane,
                    request.claim,
                    request.generation,
                    request.compose_files,
                )
            except (OSError, ValueError) as exc:
                return ModelLabStackReaperResult("lease", "refused", errors=(str(exc),))
            return ModelLabStackReaperResult("lease", "leased")
        if request.verb != "reap":
            return ModelLabStackReaperResult(
                request.verb, "refused", errors=("unknown verb",)
            )
        now = request.now or datetime.now(UTC)
        store = LeaseStore(request.lease_dir)
        decisions: list[ProjectDecision] = []
        alarms: list[str] = []
        listed_projects: set[str] = set()
        end = time.monotonic() + request.cap_min * 60
        try:
            self._validate(request, now)
            assert request.ledger is not None
            closed = closed_claims(request.ledger, now)
            projects = store.projects()
            # Validate the entire store before any removal: corrupt/unreadable is not unleased.
            records = {project: store.read(project) for project in projects}
            protected, names, _ = declared(request)
            listing = self._listing(request, end)
            listed_projects = project_names(listing)
            previous_run = request.lease_dir / "last-run.json"
            if previous_run.is_file():
                previous = json.loads(previous_run.read_text(encoding="utf-8"))
                if not isinstance(previous, dict):
                    raise ValueError("invalid last-run.json")
                if previous.get("decision") == "running":
                    alarms.extend(
                        f"{project}: previous pass was killed before recording its outcome"
                        for project in sorted(listed_projects)
                    )
            # SIGKILL cannot append a row. Leave a durable unfinished record so the next pass
            # alarms the still-up stacks, even if the killed process never reached a down.
            atomic_json(
                previous_run,
                {
                    "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "decision": "running",
                    "removed": [],
                    "alarms": [],
                },
            )
            for project in projects:
                record = records[project]
                if project not in listed_projects:
                    with store.lock(project):
                        # A fresh daemon observation under the lock precedes clearing any data.
                        fresh = self._listing(request, end)
                        if project not in project_names(fresh):
                            store.clear(project)
                            decisions.append(ProjectDecision(project, "gone"))
                            continue
                        listing = fresh
                reason = self._protection(
                    request, project, listing, protected, names, end
                )
                if reason:
                    decisions.append(
                        ProjectDecision(project, "refused-protected", reason=reason)
                    )
                    continue
                live = open_lanes(record, closed)
                if request.execute and (
                    store.path(project, "fence").exists()
                    or (record.leases and not live)
                ):
                    outcome = self._remove(
                        request, store, project, closed, protected, names, end
                    )
                    decisions.append(outcome)
                    if outcome.decision in {"failed", "refused-teardown-inputs"}:
                        alarms.append(
                            f"{project}: {outcome.decision}: {outcome.reason}"
                        )
                elif not record.leases or live:
                    decisions.append(ProjectDecision(project, "kept-live", live))
                else:
                    decisions.append(ProjectDecision(project, "would-remove"))
            for project in sorted(listed_projects - set(projects) - protected):
                reason = self._protection(
                    request, project, listing, protected, names, end
                )
                if reason:
                    decisions.append(
                        ProjectDecision(project, "refused-protected", reason=reason)
                    )
                    continue
                decisions.append(ProjectDecision(project, "unleased"))
                alarms.append(f"{project}: unleased stack; ownership cannot be proved")
            if request.harnesses:
                decisions.extend(
                    self._harnesses(request, now, closed, protected, names, end)
                )
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            ClaimStoreIncompleteError,
        ) as exc:
            alarms.append(f"pass refused: {exc}")
            return self._finish(request, now, "refused", decisions, alarms)
        except KeyboardInterrupt:
            # SIGKILL cannot be handled. The pre-down fence and unchanged lease record survive it;
            # a later pass recovers them. SIGTERM/interrupts get evidence when there is time.
            alarms.extend(
                f"{project}: pass aborted; removal outcome unknown"
                for project in sorted(listed_projects)
            )
            if not alarms:
                alarms.append("pass aborted before docker inventory completed")
            return self._finish(request, now, "aborted", decisions, alarms)
        return self._finish(request, now, "completed", decisions, alarms)

    def _with_snapshot(
        self, request: ModelLabStackReaperRequest
    ) -> ModelLabStackReaperResult:
        now = request.now or datetime.now(UTC)
        started = time.monotonic()
        try:
            if request.ledger is None:
                raise ValueError("no ledger supplied for run evidence")
            done: Completed | subprocess.CompletedProcess[str]
            if self._run is run_process:
                done = subprocess.run(
                    request.ledger_snapshot_command,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=min(120, request.cap_min * 60),
                )
            else:
                done = self._run(request.ledger_snapshot_command)
            now = request.now or datetime.now(UTC)
            if done.returncode:
                raise ValueError(f"ledger snapshot failed: {tail(done.stderr)}")
            data = json.loads(done.stdout)
            at = datetime.fromisoformat(data["at"])
            if not 0 <= (now - at).total_seconds() <= request.max_ledger_age_s:
                raise ValueError("stale ledger snapshot")
            sources = data["sources"]
            if not isinstance(sources, list) or not sources:
                raise ValueError("empty ledger snapshot")
            with tempfile.TemporaryDirectory(prefix="lab-stack-ledger-") as directory:
                root = Path(directory)
                seen: set[str] = set()
                for source in sources:
                    name, text = source["name"], source["text"]
                    if not isinstance(name, str) or not isinstance(text, str):
                        raise ValueError("invalid ledger snapshot source")
                    relative = Path(name)
                    if (
                        relative.is_absolute()
                        or ".." in relative.parts
                        or name in seen
                        or relative.as_posix() != name
                    ):
                        raise ValueError("invalid ledger snapshot path")
                    seen.add(name)
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                    os.utime(path, (now.timestamp(), now.timestamp()))
                # The snapshot lists the live ledger last, after any roll archives.
                ledger = root / sources[-1]["name"]
                if not ledger.is_file():
                    raise ValueError("ledger snapshot lacks the rolling ledger")
                census_text = data["census"]
                if not isinstance(census_text, str):
                    raise ValueError("ledger snapshot lacks the lane census")
                census = root / "census.yaml"
                census.write_text(census_text, encoding="utf-8")
                entries = request.ticket_lane_entries
                inventory = data.get("ticket_inventory")
                if request.ticket_inventory_required and inventory is None:
                    raise ValueError("missing required ticket-lane inventory")
                if inventory is not None:
                    if not isinstance(inventory, dict) or not isinstance(
                        inventory.get("host"), str
                    ):
                        raise ValueError("invalid ticket-lane inventory")
                    inventory_at = datetime.fromisoformat(inventory["at"])
                    if (
                        inventory["host"].split(".")[0].lower()
                        != socket.gethostname().split(".")[0].lower()
                        or inventory["roots_checked"] != len(request.worktree_roots)
                        or not 0
                        <= (now - inventory_at).total_seconds()
                        <= request.max_ledger_age_s
                        or not isinstance(inventory["entries"], list)
                        or any(
                            not isinstance(entry, str) or "/" in entry
                            for entry in inventory["entries"]
                        )
                    ):
                        raise ValueError("invalid ticket-lane inventory")
                    entries = tuple(inventory["entries"])
                if not any(" | CLAIM | " in source["text"] for source in sources):
                    raise ValueError(
                        "ledger snapshot carries no CLAIM positive control"
                    )
                # Keep the configured append target, independently of this read-only view.
                remaining = request.cap_min - (time.monotonic() - started) / 60
                if remaining <= 0:
                    raise ValueError("ledger snapshot exhausted the pass budget")
                result = self._reap_snapshot(
                    request.model_copy(
                        update={
                            "cap_min": remaining,
                            "census": census,
                            "ticket_lane_entries": entries,
                        }
                    ),
                    ledger,
                )
                return result
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.TimeoutExpired,
        ) as exc:
            return self._finish(request, now, "refused", [], [f"pass refused: {exc}"])

    def _reap_snapshot(
        self, request: ModelLabStackReaperRequest, ledger: Path
    ) -> ModelLabStackReaperResult:
        handler = HandlerLabStackReaper(self._run, evidence_ledger=request.ledger)
        return handler.handle(
            request.model_copy(update={"ledger": ledger, "ledger_snapshot_command": ()})
        )

    @staticmethod
    def _validate(request: ModelLabStackReaperRequest, now: datetime) -> None:
        values = (
            request.interval_min,
            request.cap_min,
            request.deadline_min,
            request.max_ledger_age_s,
            request.fence_wait_s,
        )
        if not all(math.isfinite(v) and v >= 0 for v in values):
            raise ValueError("timing values must be finite and nonnegative")
        if request.interval_min <= 0 or request.cap_min <= 0 or request.retries < 1:
            raise ValueError("interval, cap and retries must be positive")
        if request.interval_min + request.cap_min > request.deadline_min:
            raise ValueError("interval-min + cap-min exceeds deadline-min")
        if request.docker_host and not DOCKER_HOST_RE.fullmatch(request.docker_host):
            raise ValueError("docker host is not ssh://[user@]host")
        if request.harnesses and request.docker_host:
            raise ValueError(
                "harness ticket-lane proof requires the local docker daemon"
            )
        if request.ledger is None or not request.ledger.is_file():
            raise ValueError("ledger is not a file")
        if now.timestamp() - request.ledger.stat().st_mtime > request.max_ledger_age_s:
            raise ValueError("stale ledger view")

    def _docker(
        self, request: ModelLabStackReaperRequest, args: Sequence[str], end: float
    ) -> Completed:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise KeyboardInterrupt
        argv = [
            "docker",
            *(["-H", request.docker_host] if request.docker_host else []),
            *args,
        ]
        if self._run is run_process:
            # Reuse the established runner, with a child timeout below the pass cap. Compose
            # receives no ambient COMPOSE_* selectors and runs from the fixed lease directory.
            unset = {key for key in os.environ if key.startswith("COMPOSE_")}
            unset.update({"DOCKER_HOST", "DOCKER_CONTEXT"})
            try:
                done = subprocess.run(
                    argv,
                    cwd=request.lease_dir.resolve(),
                    env={k: v for k, v in os.environ.items() if k not in unset},
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=max(0.01, min(120, remaining)),
                )
            except subprocess.TimeoutExpired as exc:
                raise ValueError("docker command exceeded the pass budget") from exc
            return Completed(done.returncode, done.stdout, done.stderr)
        return self._run(argv)

    def _listing(self, request: ModelLabStackReaperRequest, end: float) -> str:
        done = self._docker(request, ["compose", "ls", "-a", "--format", "json"], end)
        if done.returncode:
            raise ValueError(f"docker compose ls failed: {tail(done.stderr)}")
        # The reused name helper deliberately tolerates invalid JSON. Here invalid inventory
        # must refuse, rather than appear to prove that every project disappeared.
        data = json.loads(done.stdout)
        if not isinstance(data, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("Name"), str)
            or not item["Name"]
            for item in data
        ):
            raise ValueError("invalid docker compose inventory")
        return done.stdout

    def _protection(
        self,
        request: ModelLabStackReaperRequest,
        project: str,
        listing: str,
        protected: set[str],
        names: set[str],
        end: float,
    ) -> str:
        if project in protected:
            return "declared lane project"
        if foreign_config_paths(
            config_files(listing, project), acting_user(request.docker_host)
        ):
            return "another user's compose files"
        done = self._docker(
            request,
            [
                "ps",
                "-a",
                "--filter",
                f"label={PROJECT_LABEL}={project}",
                "--format",
                "{{.Names}}",
            ],
            end,
        )
        if done.returncode:
            raise ValueError(f"docker ps failed: {tail(done.stderr)}")
        if set(done.stdout.splitlines()) & names:
            return "lane-named container"
        if any(
            re.search(r"dogfood|runner", name, re.IGNORECASE)
            for name in done.stdout.splitlines()
        ):
            return "protected dogfood or runner container"
        return ""

    def _containers(
        self, request: ModelLabStackReaperRequest, ids: list[str], end: float
    ) -> list[dict[str, Any]]:
        if not ids:
            return []
        template = (
            '{"Id":{{json .Id}},"Name":{{json .Name}},'
            '"Created":{{json .Created}},"Labels":{{json .Config.Labels}}}'
        )
        records: list[Any] = []
        done = self._docker(request, ["inspect", "--format", template, *ids], end)
        if done.returncode:
            # A container removed between the listing and this inspection (a lane's own
            # teardown, a runner's job container) is gone, not unknown, and a Dead container
            # whose removal failed in the daemon is listed but cannot be inspected or removed by
            # anyone: two such Dead ids once refused every pass on a host. Inspect each id
            # alone and drop only an id a fresh listing no longer holds or lists as Dead; any
            # other failure still refuses the pass.
            present = self._docker(request, ["ps", "-aq", "--no-trunc"], end)
            dead = self._docker(
                request, ["ps", "-aq", "--no-trunc", "--filter", "status=dead"], end
            )
            if present.returncode or dead.returncode:
                raise ValueError(f"docker inspect failed: {tail(done.stderr)}")
            live = set(present.stdout.splitlines()) - set(dead.stdout.splitlines())
            for identity in ids:
                if identity not in live:
                    continue
                one = self._docker(
                    request, ["inspect", "--format", template, identity], end
                )
                if one.returncode:
                    raise ValueError(f"docker inspect failed: {tail(one.stderr)}")
                records.extend(json.loads(line) for line in one.stdout.splitlines())
            expected = len(set(ids) & live)
        else:
            records = [json.loads(line) for line in done.stdout.splitlines()]
            expected = len(ids)
        if len(records) != expected or any(not isinstance(r, dict) for r in records):
            raise ValueError("incomplete harness inventory")
        return records

    @staticmethod
    def _harness_reason(
        request: ModelLabStackReaperRequest,
        container: dict[str, Any],
        now: datetime,
        closed: set[ClaimKey],
        protected: set[str],
        names: set[str],
    ) -> str:
        labels = container.get("Labels") or {}
        if not isinstance(labels, dict):
            raise ValueError("invalid container labels")
        harness_labels = [key for key in labels if HARNESS_LABEL.fullmatch(key)]
        if not harness_labels:
            return "unlabelled"
        name = str(container["Name"]).lstrip("/")
        project = labels.get(PROJECT_LABEL, "")
        if (
            name in names
            or project in protected
            or re.search(r"dogfood|runner", name, re.IGNORECASE)
            or any(token_in(p, name) for p in protected)
        ):
            return "protected"
        created = datetime.fromisoformat(container["Created"].replace("Z", "+00:00"))
        if (now - created).total_seconds() <= request.deadline_min * 60:
            return "within-age-bound"
        tickets = set().union(*(tickets_of(key) for key in harness_labels))
        lane = str(labels.get("com.omninode.lane", "")).lower()
        claim = str(labels.get("com.omninode.claim", ""))
        if not tickets and not lane:
            return "no-owner-identity"
        assert request.ledger is not None
        owners: set[ClaimKey] = set()
        for source in window_sources(
            request.ledger, request.ledger.name, now=now, hours=336
        ):
            for line in source.text.splitlines():
                cells = cells_of(line)
                if len(cells) < 3 or cells[1] != "CLAIM":
                    continue
                owner = cell_value(cells, "lane").lower()
                owned_tickets = tickets_of(
                    cell_value(cells, "ticket") + " " + cell_value(cells, "tickets")
                )
                if (lane and owner == lane) or owned_tickets & tickets:
                    claimed_at = datetime.strptime(
                        cells[0], "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=UTC)
                    if claimed_at < now - timedelta(hours=336):
                        continue
                    owners.add((owner, cells[0]))
        if any(owner not in closed for owner in owners):
            return "live-or-unproven-claim"
        if lane and claim and (lane, claim) not in closed:
            return "unclosed-labelled-claim"
        if owners:
            return ""
        # A legacy harness label carries its ticket, but predates generation-bound leases.
        # Absence is proved against every configured ticket root, never a failed directory read.
        if not tickets or not request.worktree_roots:
            return "no-ticket-lane-proof"
        if request.ticket_lane_entries is not None:
            if any(
                tickets_of(entry) & tickets or (lane and token_in(lane, entry))
                for entry in request.ticket_lane_entries
            ):
                return "ticket-lane-present"
            return ""
        for root in request.worktree_roots:
            if not root.is_absolute():
                raise ValueError("worktree root must be absolute")
            try:
                root.stat()
            except FileNotFoundError:
                root.parent.stat()  # an absent root is allowed; an unreadable parent is not
                continue
            for entry in root.iterdir():
                if tickets_of(entry.name) & tickets or (
                    lane and token_in(lane, entry.name)
                ):
                    return "ticket-lane-present"
        return ""

    def _harnesses(
        self,
        request: ModelLabStackReaperRequest,
        now: datetime,
        closed: set[ClaimKey],
        protected: set[str],
        names: set[str],
        end: float,
    ) -> list[ProjectDecision]:
        listed = self._docker(request, ["ps", "-aq", "--no-trunc"], end)
        if listed.returncode:
            raise ValueError(f"harness inventory failed: {tail(listed.stderr)}")
        ids = listed.stdout.splitlines()
        if any(not re.fullmatch(r"[a-f0-9]{64}", item) for item in ids):
            raise ValueError("invalid harness container id")
        decisions: list[ProjectDecision] = []
        for container in self._containers(request, ids, end):
            reason = self._harness_reason(
                request, container, now, closed, protected, names
            )
            if reason == "unlabelled":
                continue
            name = str(container["Name"]).lstrip("/")
            if reason:
                decisions.append(ProjectDecision(name, "kept-harness", reason=reason))
                continue
            if not request.execute:
                decisions.append(ProjectDecision(name, "would-remove"))
                continue
            identity = container["Id"]
            if identity not in ids:
                raise ValueError("harness identity differs from inventory")
            inspected = self._containers(request, [identity], end)
            if not inspected:
                decisions.append(ProjectDecision(name, "gone"))
                continue
            fresh = inspected[0]
            if fresh != container or self._harness_reason(
                request, fresh, now, closed, protected, names
            ):
                decisions.append(
                    ProjectDecision(name, "kept-harness", reason="identity changed")
                )
                continue
            removed = self._docker(request, ["rm", "--force", identity], end)
            remaining = self._docker(request, ["ps", "-aq", "--no-trunc"], end)
            if (
                removed.returncode
                or remaining.returncode
                or identity in remaining.stdout.splitlines()
            ):
                raise ValueError(
                    f"harness removal not confirmed for {name}: {tail(removed.stderr)}"
                )
            decisions.append(ProjectDecision(name, "removed"))
        return decisions

    def _remove(
        self,
        request: ModelLabStackReaperRequest,
        store: LeaseStore,
        project: str,
        closed: set[ClaimKey],
        protected: set[str],
        names: set[str],
        end: float,
    ) -> ProjectDecision:
        with store.lock(project):
            record = store.read(project)
            listing = self._listing(request, end)
            fence = store.path(project, "fence")
            if fence.exists():
                until = min(end, time.monotonic() + request.fence_wait_s)
                while project in project_names(listing) and time.monotonic() < until:
                    time.sleep(min(0.25, max(0, until - time.monotonic())))
                    listing = self._listing(request, end)
            if project not in project_names(listing):
                store.clear(project)
                return ProjectDecision(project, "gone")
            reason = self._protection(request, project, listing, protected, names, end)
            if reason:
                return ProjectDecision(project, "refused-protected", reason=reason)
            live = open_lanes(record, closed)
            if not record.leases or live:
                return ProjectDecision(project, "kept-live", live)
            # Persist the fence before a subprocess can start. Exceptions/termination deliberately
            # leave it in place: down may still be running on the daemon.
            with fence.open("w", encoding="utf-8") as stream:
                stream.write("removing\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                files = self._teardown_files(record)
                active_files = config_files(listing, project)
                if files and active_files and files != active_files:
                    raise ValueError(
                        "recorded compose files differ from the project's configuration"
                    )
                self._check_networks(request, project, files, listing, end)
            except (OSError, ValueError) as exc:
                fence.unlink()
                return ProjectDecision(
                    project, "refused-teardown-inputs", reason=str(exc)
                )
            detail = ""
            for _ in range(request.retries):
                down = self._docker(
                    request,
                    [
                        "compose",
                        "-p",
                        project,
                        *[arg for file in files for arg in ("-f", file)],
                        "down",
                    ],
                    end,
                )
                listing = self._listing(request, end)
                if project not in project_names(listing):
                    store.clear(project)
                    return ProjectDecision(project, "removed")
                detail = (
                    f"still listed; down exited {down.returncode}: {tail(down.stderr)}"
                )
            fence.unlink()
            return ProjectDecision(project, "failed", reason=detail)

    def _network_names(
        self,
        request: ModelLabStackReaperRequest,
        project: str,
        files: list[str],
        end: float,
        *,
        owned_only: bool,
    ) -> set[str]:
        rendered = self._docker(
            request,
            [
                "compose",
                "-p",
                project,
                *[arg for file in files for arg in ("-f", file)],
                "config",
                "--format",
                "json",
            ],
            end,
        )
        if rendered.returncode:
            raise ValueError(
                f"cannot prove {project}'s network definition: {tail(rendered.stderr)}"
            )
        data = json.loads(rendered.stdout)
        if not isinstance(data, dict) or not isinstance(data.get("networks", {}), dict):
            raise ValueError(f"invalid compose network definition for {project}")
        networks: set[str] = set()
        for key, value in data.get("networks", {}).items():
            if not isinstance(value, dict):
                raise ValueError(f"invalid network {key} for {project}")
            external = value.get("external", False)
            if not isinstance(external, bool):
                raise ValueError(f"unresolved external network {key} for {project}")
            if owned_only and external:
                continue
            name = value.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError(f"unresolved network name {key} for {project}")
            networks.add(name)
        return networks

    def _check_networks(
        self,
        request: ModelLabStackReaperRequest,
        project: str,
        files: list[str],
        listing: str,
        end: float,
    ) -> None:
        owned = self._network_names(request, project, files, end, owned_only=True)
        if not owned:
            return
        # Container attachments cannot prove exclusive ownership. Inspect every other listed
        # project's desired topology, including external networks with no attached container.
        for other in sorted(project_names(listing) - {project}):
            used = self._network_names(
                request,
                other,
                config_files(listing, other),
                end,
                owned_only=False,
            )
            overlap = owned & used
            if overlap:
                raise ValueError(
                    f"{project} owns networks also expected by {other}: "
                    f"{', '.join(sorted(overlap))}"
                )

    @staticmethod
    def _teardown_files(record: LeaseRecord) -> list[str]:
        files: dict[str, str] = {}
        for lease in record.leases:
            for file in lease.compose_files:
                if file.path in files and files[file.path] != file.sha256:
                    raise ValueError("conflicting recorded compose inputs")
                if digest(Path(file.path)) != file.sha256:
                    raise ValueError(f"changed recorded compose file: {file.path}")
                files[file.path] = file.sha256
        return list(files)

    def _finish(
        self,
        request: ModelLabStackReaperRequest,
        now: datetime,
        decision: str,
        projects: list[ProjectDecision],
        alarms: list[str],
    ) -> ModelLabStackReaperResult:
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [
            f"{stamp} | FRICTION | lane=lab-stack-reaper | ticket={request.ticket}"
            " | cost=est ~0.1 lane-hours; an unreaped lab stack holds lab capacity"
            f" | {cell(alarm, 1200)}"
            for alarm in alarms
        ]
        counts = Counter(p.decision for p in projects)
        summary = (
            "; ".join(f"{key}={value}" for key, value in sorted(counts.items()))
            or "no projects"
        )
        rows.append(
            f"{stamp} | STATUS | lane=lab-stack-reaper | ticket={request.ticket}"
            " | actor=script:lab-stack-reaper | model=none | action=reap"
            f" | decision={decision} | mode={'execute' if request.execute else 'dry-run'}"
            f" | {cell(summary, 1200)}; alarms={len(alarms)}"
        )
        errors: list[str] = []
        if request.ledger is not None:
            for row in rows:
                try:
                    done = self._run(
                        append_argv(self._evidence_ledger or request.ledger, row)
                    )
                    if done.returncode:
                        errors.append(
                            f"ledger append failed: {tail(done.stderr or done.stdout)}"
                        )
                except OSError as exc:
                    errors.append(f"ledger append failed: {exc}")
        else:
            errors.append("no ledger supplied for run evidence")
        try:
            atomic_json(
                request.lease_dir / "last-run.json",
                {
                    "at": stamp,
                    "decision": decision
                    if not alarms and not errors
                    else "refused"
                    if decision == "refused"
                    else "alarmed",
                    "removed": [p.project for p in projects if p.decision == "removed"],
                    "alarms": [*alarms, *errors],
                },
            )
        except OSError as exc:
            errors.append(f"last-run.json write failed: {exc}")
        return ModelLabStackReaperResult(
            request.verb,
            decision,
            projects=tuple(projects),
            alarms=tuple(alarms),
            ledger_rows=tuple(rows),
            errors=tuple(errors),
        )
