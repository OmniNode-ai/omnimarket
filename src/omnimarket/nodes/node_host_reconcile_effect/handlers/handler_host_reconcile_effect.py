# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host-local reconciliation effects around the existing repair delegates.

All content verdicts, remedies and floor eligibility come from the evaluator.
The verifier script still owns health, refusal ownership, observation and floor I/O.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Literal

import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.models.model_host_reconcile import (
    ModelGatePurityFact,
    ModelGuardFact,
    ModelHostReconcileCommand,
    ModelHostReconcileDecisions,
    ModelHostReconcileEvaluateRequest,
    ModelHostReconcileRunResult,
    ModelHostReconcileSlackCommand,
    ModelPathShadowFact,
    ModelSurfaceDecision,
    ModelSurfaceFact,
    ModelSurfaceIndeterminateFact,
    ModelSurfaceMovementFact,
    ModelSurfaceUncoveredFact,
    ModelSurfaceUnhealthyFact,
)
from omnimarket.nodes.node_host_reconcile_effect.protocols import (
    ProtocolHostReconcileEvaluator,
    ProtocolHostReconcileEventPublisher,
)

from .adapter_holder import (
    ReconcileStopError,
    _live,
    _signal_holder,
    confirm_reconcile_holder,
    read_holder,
    terminate_holder,
)


class HandlerHostReconcileEffect:
    """Host-local effects; every request owns independent run state."""

    def __init__(
        self,
        *,
        evaluator: ProtocolHostReconcileEvaluator,
        publisher: ProtocolHostReconcileEventPublisher,
        completed_topic: str,
        slack_topic: str,
    ) -> None:
        self.evaluator = evaluator
        self.publisher = publisher
        self.completed_topic = completed_topic
        self.slack_topic = slack_topic

    def handle(self, command: ModelHostReconcileCommand) -> ModelHostReconcileRunResult:
        previous: dict[int, Callable[[int, FrameType | None], object] | int | None] = {}

        def interrupt(signum: int, frame: FrameType | None) -> None:
            raise ReconcileStopError(
                3, f"INDETERMINATE: interrupted by signal {signum}"
            )

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, interrupt)
        try:
            result = _ReconcilePass(
                command, self.evaluator, self.publisher, self.slack_topic
            ).run()
        finally:
            for restored_signum, handler in previous.items():
                signal.signal(restored_signum, handler)
        try:
            self.publisher.publish(self.completed_topic, result)
        except Exception as exc:
            result = result.model_copy(
                update={
                    "diagnostics": (
                        *result.diagnostics,
                        f"WARNING: could not publish run-completed event: {exc}",
                    )
                }
            )
        return result


class _ReconcilePass:
    def __init__(
        self,
        request: ModelHostReconcileCommand,
        evaluator: ProtocolHostReconcileEvaluator,
        publisher: ProtocolHostReconcileEventPublisher,
        slack_topic: str,
    ) -> None:
        self.request = request
        self.publisher = publisher
        self.slack_topic = slack_topic
        contract = yaml.safe_load(
            (Path(__file__).parent.parent / "contract.yaml").read_text()
        )
        self.receipt_schema = str(contract["metadata"]["receipt_schema"])
        self.evaluator = evaluator
        self.facts: list[ModelSurfaceFact] = []
        self.deadline = time.monotonic() + request.run_timeout_s
        self.root = Path(request.workspace_root)
        self.scripts = self.root / "omnibase_infra" / "scripts"
        self.verifier = self.scripts / "reconcile_verify_movement.py"
        self.host = socket.gethostname()
        self.owner_prefix: list[str] = []
        self.surfaces: list[ModelSurfaceDecision] = []
        self.diagnostics: list[str] = []
        self.floor_stamped = False
        self.child: subprocess.Popen[str] | None = None
        self.lock = self.root / ".onex-reconcile-host.lock"
        self.acquired = False
        self.holder_record = ""
        self.env = scrub_git_location_env()
        self.env["GIT_OPTIONAL_LOCKS"] = "0"

    def say(self, text: str) -> None:
        self.diagnostics.append(text)

    def command(
        self,
        args: list[str],
        *,
        owner: bool = False,
        input_text: str | None = None,
        discard_stdout: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        label = " ".join([Path(args[0]).name, *args[1:3]])
        if Path(args[0]).name == "git":
            skip = False
            for arg in args[1:]:
                if skip:
                    skip = False
                elif arg == "-C":
                    skip = True
                elif not arg.startswith("-"):
                    label = f"git {arg}"
                    break
        return self._spawn(
            label,
            args,
            owner=owner,
            input_text=input_text,
            discard_stdout=discard_stdout,
        )

    def _store_holder(self, text: str) -> None:
        # Replace the record atomically: a reader (a reclaimer, or a child that
        # copies it) must never catch the file truncated and half written.
        staging = self.lock / "holder.tmp"
        staging.write_text(text)
        os.replace(staging, self.lock / "holder")

    def _write_holder(self) -> None:
        if self.acquired:
            text = self.holder_record
            if self.child is not None:
                text += (
                    f"child_pgid={self.child.pid}\n"
                    f"child_started_at={datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}\n"
                )
            # The child line is for a reclaimer; failing to record it must not
            # replace the run's own outcome.
            with suppress(OSError):
                self._store_holder(text)

    def _spawn(
        self,
        label: str,
        args: list[str],
        *,
        owner: bool = False,
        input_text: str | None = None,
        discard_stdout: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ReconcileStopError(
                5,
                f"TIMEOUT: the reconcile run exceeded its {self.request.run_timeout_s}s bound "
                f"before {label}",
            )
        run_bound = remaining < self.request.step_timeout_s
        command = (self.owner_prefix if owner else []) + args
        self.child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.DEVNULL if discard_stdout else subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=self.env,
        )
        try:
            self._write_holder()
            stdout, stderr = self.child.communicate(
                input=input_text, timeout=min(self.request.step_timeout_s, remaining)
            )
            return subprocess.CompletedProcess(
                command, self.child.returncode, stdout, stderr
            )
        except subprocess.TimeoutExpired as exc:
            self.stop_child()
            bound = (
                f"the reconcile run's {self.request.run_timeout_s}s bound"
                if run_bound
                else f"{self.request.step_timeout_s}s"
            )
            raise ReconcileStopError(
                5, f"TIMEOUT: {label} exceeded {bound}; killed its process group"
            ) from exc
        except BaseException:
            self.stop_child()
            raise
        finally:
            self.child = None
            self._write_holder()

    def verify(
        self, *args: str, owner: bool = False
    ) -> subprocess.CompletedProcess[str]:
        return self.command([sys.executable, str(self.verifier), *args], owner=owner)

    def git(self, clone: Path, *args: str, owner: bool = False) -> str:
        result = self.command(["git", "-C", str(clone), *args], owner=owner)
        return result.stdout.strip() if result.returncode == 0 else ""

    def setup(self) -> tuple[list[str], list[str]]:
        if self.request.target_host and self.request.target_host != self.host:
            raise ReconcileStopError(
                3,
                (
                    f"INDETERMINATE: command targets {self.request.target_host}, not {self.host}"
                ),
            )
        if not self.root.is_dir():
            raise ReconcileStopError(
                3, f"INDETERMINATE: OMNI_HOME does not exist: {self.root}"
            )
        if shutil.which("git") is None:
            raise ReconcileStopError(3, "INDETERMINATE: git is not on PATH.")
        for path, label in (
            (self.verifier, "verifier"),
            (
                self.scripts / "runtime_build/sibling_clone_manifest.sh",
                "clone manifest",
            ),
            (self.scripts / "reconcile_privilege_lib.sh", "privilege library"),
        ):
            if not path.is_file():
                raise ReconcileStopError(3, f"INDETERMINATE: {label} missing at {path}")
        # Read the shared manifest; never maintain a second sibling census.
        probe = self.command(
            [
                "bash",
                "-c",
                'source "$1"; printf "%s\\n" "${SIBLING_CLONE_MANIFEST[@]}"; '
                'printf "%s\\n" "---DISTS---"; '
                'printf "%s\\n" "${SIBLING_CLONE_MANIFEST_DIST_NAMES[@]}"',
                "manifest",
                str(self.scripts / "runtime_build/sibling_clone_manifest.sh"),
            ]
        )
        # printf's format beginning with '-' needs an explicit format string.
        lines = probe.stdout.splitlines()
        if "---DISTS---" not in lines:
            raise ReconcileStopError(3, "INDETERMINATE: cannot read clone manifest")
        split = lines.index("---DISTS---")
        return lines[:split], [n for n in lines[split + 1 :] if n != "omnimarket"]

    def acquire(self) -> None:
        try:
            self.lock.mkdir()
        except FileExistsError:
            holder_path = self.lock / "holder"
            fields = read_holder(self.lock)
            try:
                age = (
                    time.time()
                    - (holder_path if holder_path.is_file() else self.lock)
                    .stat()
                    .st_mtime
                )
            except OSError:
                age = None
            pid, host = fields.get("pid", ""), fields.get("host", "")
            reclaim = ""
            if age is not None:
                if not pid or not host:
                    if age > self.request.lock_stale_s:
                        reclaim = (
                            f"no readable holder record and the lock is {int(age)}s old"
                        )
                elif host != self.host:
                    if age > self.request.lock_stale_s:
                        reclaim = (
                            f"holder pid {pid} is on host {host}, not this one, "
                            f"and the lock is {int(age)}s old"
                        )
                elif not _live(pid):
                    reclaim = (
                        f"holder pid {pid} on this host is not running, "
                        f"and the lock is {int(age)}s old"
                    )
            if not reclaim:
                if pid.isdigit() and int(pid) > 0 and host == self.host and _live(pid):
                    try:
                        started = datetime.strptime(
                            fields.get("started_at", ""), "%Y-%m-%dT%H:%M:%SZ"
                        ).replace(tzinfo=UTC)
                        live_age = int(time.time() - started.timestamp())
                    except ValueError:
                        live_age = 0
                    if live_age > self.request.max_holder_age_s:
                        confirmed, evidence = confirm_reconcile_holder(
                            fields, self.host
                        )
                        if confirmed:
                            termination = terminate_holder(fields)
                            # A terminated holder releases the lock itself; a
                            # peer may take it before this run does.
                            if self.lock.exists() and read_holder(self.lock).get(
                                "pid"
                            ) not in (
                                None,
                                pid,
                            ):
                                raise ReconcileStopError(
                                    4,
                                    f"{termination}; another reconcile-host took the lock "
                                    f"after it; nothing to do.",
                                ) from None
                            reclaim = (
                                f"holder pid {pid} on {host} held it for {live_age}s "
                                f"(max {self.request.max_holder_age_s}s); {evidence}; {termination}"
                            )
                        else:
                            self.say(
                                f"Remedy: inspect pid {pid} on {host} and stop it "
                                f"only after confirming why it is stuck; the next "
                                f"tick will reclaim the dead holder."
                            )
                            self.say(f"not terminated: {evidence}")
                            raise ReconcileStopError(
                                6,
                                (
                                    f"STALE-LIVE-HOLDER: pid {pid} on {host} has held "
                                    f"the lock for {live_age}s "
                                    f"(max {self.request.max_holder_age_s}s); "
                                    f"this run does not kill it"
                                ),
                            ) from None
                if not reclaim:
                    raise ReconcileStopError(
                        4,
                        (
                            f"another reconcile-host is running ({self.lock}); "
                            f"nothing to do. this run reconciled NOTHING; held "
                            f"by pid {pid or '<unrecorded>'} on {host or '<unrecorded>'} "
                            f"since {fields.get('started_at', '<unrecorded>')}"
                        ),
                    ) from None
            self.say(f"RECLAIMING a stale reconcile-host lock: {reclaim}")
            with suppress(FileNotFoundError):
                shutil.rmtree(self.lock)
            try:
                self.lock.mkdir()
            except FileExistsError as exc:
                raise ReconcileStopError(
                    4,
                    "another reconcile-host took the lock during the reclaim; nothing to do.",
                ) from exc
        self.acquired = True
        self.holder_record = (
            f"pid={os.getpid()}\nhost={self.host}\nstarted_at={datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}\n"
            "holder=host-reconcile\n"
        )
        self._store_holder(self.holder_record)

    def plan_owner(self, repos: list[str]) -> None:
        # Reuse the existing privilege library, including root/runuser behavior.
        probe = self.command(
            [
                "bash",
                "-c",
                'source "$1"; rp_plan_privileges "$2"; rc=$?; '
                'printf "%s\\n" "$RP_OWNER" "$CURRENT_USER"; '
                'printf "%s\\n" "${RUN_AS[@]}"; exit "$rc"',
                "privileges",
                str(self.scripts / "reconcile_privilege_lib.sh"),
                str(self.root),
            ]
        )
        lines = probe.stdout.splitlines()
        owner = lines[0] if lines else "<unknown>"
        current = lines[1] if len(lines) > 1 else "<unknown>"
        if probe.returncode:
            raise ReconcileStopError(
                3,
                (
                    f"INDETERMINATE: {self.root} is owned by {owner}, "
                    f"but this process runs as {current} and cannot become that user "
                    f"(privilege plan exit {probe.returncode}); run "
                    f"as its owner, or root with runuser."
                ),
            )
        self.owner_prefix = [part for part in lines[2:] if part]
        if self.owner_prefix:
            self.say(
                f"writing as {owner} (owner of {self.root}); this process is {current}"
            )
        for repo in repos:
            probe = self.command(
                [
                    "bash",
                    "-c",
                    'source "$1"; rp_surface_owner "$2"',
                    "owner",
                    str(self.scripts / "reconcile_privilege_lib.sh"),
                    str(self.root / repo),
                ]
            )
            actual = probe.stdout.strip()
            if actual and actual != owner:
                raise ReconcileStopError(
                    3,
                    (
                        f"INDETERMINATE: the clones under {self.root} do "
                        f"not share one owner; {repo} is owned by {actual}, "
                        f"registry by {owner}."
                    ),
                )

    def step(self, label: str, args: list[str], *, owner: bool = False) -> None:
        result = self._spawn(label, args, owner=owner)
        for line in (result.stdout + result.stderr).splitlines():
            self.say(line)
        if result.returncode:
            self.say(f"{label} exited non-zero; the readback below is what decides.")

    def stop_child(self) -> None:
        if self.child is None:
            return
        try:
            _signal_holder(self.child.pid, signal.SIGTERM, group=True)
            self.child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _signal_holder(self.child.pid, signal.SIGKILL, group=True)
            with suppress(subprocess.TimeoutExpired):
                self.child.communicate(timeout=5)

    def evaluate(
        self, facts: tuple[ModelSurfaceFact, ...]
    ) -> ModelHostReconcileDecisions:
        return self.evaluator.handle(
            ModelHostReconcileEvaluateRequest(
                mode=self.request.mode,
                workspace_root=str(self.root),
                scripts_dir=str(self.scripts),
                rerun_command=f"python -m omnimarket.nodes.node_host_reconcile_effect --omni-home {self.root}",
                receipt_path=self.request.receipt
                or str(self.root / ".onex-workspace-reconcile.json"),
                facts=facts,
            )
        )

    def movement(self, surface: str, before: str, after: str, target: str) -> None:
        self.facts.append(
            ModelSurfaceMovementFact(
                surface=surface, before=before, after=after, target=target
            )
        )

    def clones(self, repos: list[str]) -> None:
        self.plan_owner(repos)

        def fetch() -> None:
            for repo in repos:
                self.git(
                    self.root / repo,
                    "fetch",
                    "--quiet",
                    "--prune",
                    "origin",
                    self.request.branch,
                    owner=True,
                )

        fetch()
        before = {
            repo: self.git(self.root / repo, "rev-parse", "HEAD") for repo in repos
        }
        delegate = Path(
            self.request.clone_delegate
            or self.scripts / "runtime_build/reconcile_deploy_clones.sh"
        )
        if self.request.mode == "repair":
            if not delegate.is_file():
                self.facts.append(
                    ModelSurfaceUncoveredFact(
                        surface="clone-surface", delegate=str(delegate), layer="clone"
                    )
                )
            else:
                self.step(
                    "clone delegate",
                    [
                        "env",
                        f"OMNI_HOME={self.root}",
                        f"RECONCILE_BRANCH={self.request.branch}",
                        "bash",
                        str(delegate),
                    ],
                    owner=True,
                )
        fetch()
        for repo in repos:
            clone = self.root / repo
            health = self.verify("clone-health", "--clone", str(clone))
            if health.returncode == 0:
                self.movement(
                    f"clone:{repo}",
                    before[repo],
                    self.git(clone, "rev-parse", "HEAD"),
                    self.git(clone, "rev-parse", f"origin/{self.request.branch}"),
                )
            else:
                cells = health.stdout.strip().split("\t", 2)
                self.facts.append(
                    ModelSurfaceUnhealthyFact(
                        surface=f"clone:{repo}",
                        reason=cells[2]
                        if len(cells) > 2
                        else "clone is not checkout-capable",
                    )
                )
            if self.evaluate((self.facts[-1],)).failures:
                args = [
                    "clone-refusal",
                    "--clone",
                    str(clone),
                    "--repo",
                    repo,
                    "--state-dir",
                    str(self.root / ".onex_state"),
                    "--ledger",
                    self.request.ledger,
                    "--lock-age-s",
                    str(self.request.step_timeout_s),
                ]
                if self.request.mode == "repair":
                    args.append("--record")
                explanation = self.verify(*args, owner=True)
                for line in explanation.stdout.splitlines():
                    tag, _, text = line.partition("\t")
                    self.say(f"    {text}")
                    if tag == "owner" and not self.facts[-1].owner:
                        self.facts[-1] = self.facts[-1].model_copy(
                            update={"owner": text.removeprefix("owner: ")}
                        )

    @staticmethod
    def site_packages(venv: Path) -> Path | None:
        return next(
            (p for p in sorted(venv.glob("lib/python*/site-packages")) if p.is_dir()),
            None,
        )

    @staticmethod
    def version(sp: Path | None, name: str) -> str:
        if sp is None:
            return ""
        dist = next(
            (
                p
                for p in sorted(sp.glob(f"{name.replace('-', '_')}-*.dist-info"))
                if p.is_dir()
            ),
            None,
        )
        return dist.name.removesuffix(".dist-info").rsplit("-", 1)[-1] if dist else ""

    def commit(self, sp: Path | None) -> str:
        if sp is None:
            return ""
        result = self.verify(
            "observe", "--site-packages", str(sp), "--commit-dist", "omnimarket"
        )
        try:
            observed = json.loads(result.stdout)
            return str(observed.get("commits", {}).get("omnimarket") or "")
        except (ValueError, AttributeError):
            return ""

    def passed_through(self, clone: Path, observed: str, start: str, now: str) -> bool:
        """Whether ``observed`` is a HEAD the clone held during this run.

        True when it lies on the fast-forward from ``start`` to ``now``: ``start``
        is an ancestor of it (or it), and it is an ancestor of ``now`` (or it).
        A commit the clone never held, such as one left by a delegate that
        installed nothing, is not on that path.
        """
        if not (observed and start and now):
            return False

        def ancestor(older: str, newer: str) -> bool:
            if older == newer:
                return True
            return (
                self.command(
                    [
                        "git",
                        "-C",
                        str(clone),
                        "merge-base",
                        "--is-ancestor",
                        older,
                        newer,
                    ]
                ).returncode
                == 0
            )

        return ancestor(start, observed) and ancestor(observed, now)

    def venvs(self, governed: list[str]) -> Path | None:
        venv = Path(self.request.dispatch_venv or self.root / ".onex-dispatch-venv")
        sp = self.site_packages(venv)
        if sp is None:
            self.facts.append(
                ModelSurfaceIndeterminateFact(
                    surface="venv:dispatch", detail=f"no site-packages under {venv}"
                )
            )
        args = ["lock-targets", "--lock", str(self.root / "omnibase_infra/uv.lock")]
        for name in governed:
            args += ["--dist", name]
        targets = self.verify(*args)
        try:
            pins: dict[str, str] = json.loads(targets.stdout)
        except ValueError:
            pins = {}
        before = {name: self.version(sp, name) for name in governed}
        before_commit = self.commit(sp)
        market = self.root / "omnimarket"
        # The clone-sync timer fast-forwards the clone while the delegate runs, so
        # the head the delegate installed from can be any commit the clone passed
        # through during the run, not the head read afterwards.
        head_at_start = (
            self.git(market, "rev-parse", "HEAD") if (market / ".git").is_dir() else ""
        )
        delegate = Path(
            self.request.venv_delegate or self.scripts / "reconcile-workspace-venvs.sh"
        )
        if self.request.mode == "repair":
            if not delegate.is_file():
                self.facts.append(
                    ModelSurfaceUncoveredFact(
                        surface="venv-surface", delegate=str(delegate), layer="venv"
                    )
                )
            else:
                self.step(
                    "venv delegate",
                    [
                        "bash",
                        str(delegate),
                        "--omni-home",
                        str(self.root),
                        "--branch",
                        self.request.branch,
                    ],
                )
                sp = self.site_packages(venv)
        if sp:
            for name in governed:
                if pins.get(name):
                    self.movement(
                        f"venv:{name}", before[name], self.version(sp, name), pins[name]
                    )
            if (market / ".git").is_dir():
                observed = self.commit(sp)
                head_now = self.git(market, "rev-parse", "HEAD")
                target = (
                    observed
                    if self.passed_through(market, observed, head_at_start, head_now)
                    else head_now
                )
                self.movement("venv:omnimarket", before_commit, observed, target)
        gate = self.root / "omnibase_infra/.venv"
        gate_sp = self.site_packages(gate)
        if gate_sp:
            provider = next(iter(sorted(gate_sp.glob("omnimarket-*.dist-info"))), None)
            self.facts.append(
                ModelGatePurityFact(
                    gate_venv=str(gate),
                    dispatch_venv=str(venv),
                    provider=provider.name if provider and provider.is_dir() else "",
                )
            )
        return sp

    def guards(self) -> None:
        home = self.env.get("HOME")
        if home:
            shadow, wrapper = Path(home) / ".local/bin/onex", self.scripts / "onex"
            if shadow.exists() or shadow.is_symlink():
                is_symlink = shadow.is_symlink()
                # The old guard resolves paths only for a symlink. A regular
                # shadow must still be reported even if the wrapper cannot resolve.
                resolved = shadow.resolve() if is_symlink else shadow
                wrapper_resolved = wrapper.resolve() if is_symlink else wrapper
                self.facts.append(
                    ModelPathShadowFact(
                        shadow=str(shadow),
                        wrapper=str(wrapper),
                        wrapper_resolved=str(wrapper_resolved),
                        is_symlink=is_symlink,
                        resolved=str(resolved),
                        points_to_wrapper=is_symlink and resolved == wrapper_resolved,
                    )
                )
        src, live = self.scripts / "git-hooks", self.root / "scripts/git-hooks"
        if not src.is_dir() or not live.is_dir():
            return
        checked, drifted = 0, []
        for name in (
            "canonical_clone_guard.sh",
            "canonical_clone_paths.sh",
            "canonical_clone_ref_guard.sh",
        ):
            source, target = src / name, live / name
            if not source.is_file():
                continue
            checked += 1
            if not target.is_file() or target.read_bytes() != source.read_bytes():
                drifted.append(name)
        if checked:
            self.facts.append(
                ModelGuardFact(
                    live_dir=str(live),
                    source_dir=str(src),
                    checked=checked,
                    drifted=tuple(drifted),
                )
            )

    def finish(self, governed: list[str], sp: Path | None) -> Literal[0, 2]:
        decisions = self.evaluate(tuple(self.facts))
        self.surfaces = list(decisions.surfaces)
        # Failed decisions carry the evaluator's remedy; select them for alert text only.
        failures = [s for s in self.surfaces if s.remedy]
        receipt = {
            "schema": self.receipt_schema,
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mode": self.request.mode,
            "workspace_root": str(self.root),
            "branch": self.request.branch,
            "surfaces": [s.model_dump() for s in self.surfaces],
            "failures": decisions.failures,
            "dispatch_premise_failures": decisions.dispatch_premise_failures,
        }
        # Keep each surface on one line and key order intact for the shell floor reader.
        header = json.dumps(
            {k: v for k, v in receipt.items() if k != "surfaces"}, indent=2
        )
        text = (
            header[:-2]
            + ',\n  "surfaces": [\n'
            + ",\n".join("    " + json.dumps(s.model_dump()) for s in self.surfaces)
            + "\n  ]\n}\n"
        )

        path = self.request.receipt or str(self.root / ".onex-workspace-reconcile.json")
        if self.command(
            ["tee", path], owner=True, input_text=text, discard_stdout=True
        ).returncode:
            self.say(f"WARNING: could not write receipt to {path}")
        if decisions.stamp_floor:
            args = [
                "floor",
                "--output",
                str(self.root / ".onex-workspace-floor.json"),
                "--omni-home",
                str(self.root),
            ]
            for name in governed:
                version = self.version(sp, name)
                if version:
                    args += ["--distribution", f"{name.replace('-', '_')}={version}"]
            commit = self.commit(sp)
            if commit:
                args += ["--omnimarket-commit", commit]
            if "--distribution" in args or commit:
                self.floor_stamped = self.verify(*args, owner=True).returncode == 0
            else:
                self.say("WARNING: nothing observable to stamp; floor left untouched.")
        for surface in self.surfaces:
            self.say(f"  {surface.surface}: {surface.verdict} ({surface.detail})")
        for line in decisions.verdict_lines:
            self.say(line)
        if decisions.failures:
            try:
                if self.request.alert_command:
                    self.command(
                        [
                            *shlex.split(self.request.alert_command),
                            "\n".join(
                                f"{s.surface}: {s.verdict} — {s.detail}"
                                for s in failures
                            )
                            + f"\nreceipt: {path}\nhost root: {self.root}",
                        ]
                    )
                elif self.env.get("SLACK_CHANNEL_ID"):
                    message = "\n".join(
                        f"{s.surface}: {s.verdict} — {s.detail}" for s in failures
                    )
                    try:
                        self.publisher.publish(
                            self.slack_topic,
                            ModelHostReconcileSlackCommand(
                                channel=self.env["SLACK_CHANNEL_ID"],
                                text=(
                                    f"*OmniNode workspace reconcile FAILED* ({self.host})\n"
                                    f"{message}\nreceipt: {path}\nhost root: {self.root}"
                                ),
                                idempotency_key=(
                                    f"host-reconcile|{self.host}|{self.request.correlation_id}"
                                ),
                                correlation_id=self.request.correlation_id,
                            ),
                        )
                    except Exception as exc:
                        self.say(f"WARNING: could not publish Slack command: {exc}")
            except (OSError, ValueError):
                # Alert delivery is best effort, as in the historical scheduler.
                pass
        return decisions.exit_code

    def run(self) -> ModelHostReconcileRunResult:
        try:
            repos, governed = self.setup()
            self.acquire()
            present = [repo for repo in repos if (self.root / repo / ".git").exists()]
            self.say(
                f"surfaces under {self.root} (branch {self.request.branch}): clones={len(present)}"
            )
            self.clones(present)
            sp = self.venvs(governed)
            self.guards()
            code: Literal[0, 2, 3, 4, 5, 6] = self.finish(governed, sp)
        except ReconcileStopError as exc:
            if self.facts and not self.surfaces:
                self.surfaces = list(self.evaluate(tuple(self.facts)).surfaces)
                for surface in self.surfaces:
                    self.say(
                        f"  {surface.surface}: {surface.verdict} ({surface.detail})"
                    )
            self.say(exc.detail)
            code = exc.code
        finally:
            self.stop_child()
            if self.acquired:
                shutil.rmtree(self.lock)
        return ModelHostReconcileRunResult(
            exit_code=code,
            host=self.host,
            correlation_id=self.request.correlation_id,
            surfaces=tuple(self.surfaces),
            diagnostics=tuple(self.diagnostics),
            floor_stamped=self.floor_stamped,
        )
