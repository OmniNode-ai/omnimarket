# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install smoke, rollback, failed-version holds and lane revision selection (OMN-17427).

The legacy entry points supply their existing filesystem and Git operations. Keeping
those dependencies explicit preserves their isolated runtime and test configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from omnimarket.handlers.overlay_keys import resolve_overlay_key

CANARY_RECORD = "$ONEX_REGISTRY_ROOT/.claude_scratch/adoption/canary.json"
CANARY_RECORD_REL = ".claude_scratch/adoption/canary.json"
CANARY_STEP_TIMEOUT_S = 300
CANARY_OUTPUT_MAX = 4000
CANARY_FIX_PREFIX = "canary-fix-"


def canary_unit_repos() -> tuple[str, ...]:
    """Resolve the private clone roles only when the canary needs its defaults."""
    return (
        resolve_overlay_key("repos.private_data"),
        resolve_overlay_key("repos.plugin_source"),
        "omniclaude",
    )


@dataclass(frozen=True)
class ModelInstallCanaryRequest:
    """One of the contract's install operations, with caller-owned runtime inputs."""

    operation: str
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    runtime: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ModelInstallCanaryResult:
    """The selected operation's existing result."""

    value: Any


LANE_SHIP_WINDOW_H = 6  # the tick judges lanes this recent: older ones ran before the commit they were judged against
PLUGIN_SHIP_REF = "refs/plugin-ship/{sha12}"
LANE_SHIP_FIX = "read lane_ship_drift in omnimarket/nodes/node_install_canary_effect, beside the remote-lane runner's resolve_plugins"
LANE_SHIP_REPORT_MAX = 5  # findings per tick; the rest is a count on the last one
LINK_MODE = "120000"  # git's mode for a symlink; its blob is the link's target text
LINK_BITS = -1  # keep_held's mode for a kept symlink (no permission bits)


class InstallSetError(OSError):
    """A set of files was not installed whole: ``nothing installed: ...`` when the copy is as it was,
    ``PARTIAL install ...`` when a swapped file could not be put back."""


class HandlerInstallCanary:
    """Effects behind the install-canary contract, using the caller's runtime."""

    def __init__(self, runtime: Mapping[str, Any] | None = None) -> None:
        self.runtime = {} if runtime is None else runtime

    def handle(self, request: ModelInstallCanaryRequest) -> ModelInstallCanaryResult:
        """Dispatch only operations declared by the install contract."""
        operations = frozenset(
            (
                "pool_verdict",
                "cmd_canary_pool",
                "canary_hold",
                "adopt_marketplace",
                "canary_cap",
                "merged_plugin_sha",
                "installed_commit",
                "shipped_plugin_sha",
                "lane_ship_drift",
                "receipt_plugins",
                "publish_plugin_commit",
                "ensure_plugin_trees",
                "tick_findings",
                "scan_copy",
                "tree_sha256",
                "pin",
                "read_bytes",
                "copy_bytes",
                "source_path",
                "stage_file",
                "write_atomic",
                "install_set",
                "install_stale",
                "keep_held",
                "restore_held",
            )
        )
        if request.operation not in operations:
            raise ValueError(f"unknown install operation: {request.operation}")
        handler = self if request.runtime is None else type(self)(request.runtime)
        value = getattr(handler, request.operation)(*request.args, **request.kwargs)
        return ModelInstallCanaryResult(value)

    def canary_record_path(
        self, spec: Mapping[str, Any], env: Mapping[str, str]
    ) -> Path:
        return cast(
            "Path",
            self.runtime["expand"](str(spec.get("record") or CANARY_RECORD), env),
        )

    def pool_verdict(
        self, readings: Sequence[Any], min_admitted: int = 1
    ) -> tuple[bool, list[str]]:
        """The canary's pool read: does the pool still admit its lab hosts' usual units under the installed version?

        A lab host admits them when it was read and carries no refusal a version can cause (the parity record: the
        installed plugin, the clones, their hooks; or a hook script that does not parse). A load, memory or link
        refusal is the host's moment, not the install's, and still counts. An unreadable host proves nothing and does
        not count. The launching host is not counted: the canary is about the hosts the version rolls to."""
        lines: list[str] = []
        admitted = lab = 0
        for r in readings:
            if r.host.local:
                continue
            lab += 1
            refusal = r.parity_refusal or getattr(r, "hook_parse_refusal", None)
            if r.error is not None:
                lines.append(f"CANARY-POOL {r.host.name} unreadable {r.error}")
            elif refusal:
                lines.append(f"CANARY-POOL {r.host.name} refused {refusal}")
            else:
                admitted += 1
                lines.append(f"CANARY-POOL {r.host.name} admits")
        lines.append(f"CANARY-POOL admitted={admitted} lab={lab} min={min_admitted}")
        return (admitted >= min_admitted, lines)

    def _placement(self) -> Any:
        """The pool reader and lane queue this plugin version ships."""
        return self.runtime["placement"]

    def cmd_canary_pool(self, a: argparse.Namespace, env: Mapping[str, str]) -> int:
        """Read the pool with this plugin version's placement code (the canary runs it from the version just installed),
        judged with a remote lane's parity needs and the clones the usual lanes are built from: on 2026-10-17 the
        private clones beside the registry root were what every host was refused for."""
        lp = self._placement()
        repos = tuple(a.repo or canary_unit_repos())
        needs = lp.ParityNeeds(
            gh=True, venv=True, bus=True, gh_path=True, hooks=True, clones=repos
        )
        readings = lp.read_pool(
            lp.load_pool(env), lane="adopt-merged-canary", need_clones=repos
        )
        ok, lines = self.pool_verdict(
            [replace(r, parity_needs=needs) for r in readings], a.min_admitted
        )
        sys.stdout.write("\n".join(lines) + "\n")
        return 0 if ok else 1

    def run_canary(
        self, spec: Mapping[str, Any], plugin_root: str
    ) -> list[dict[str, Any]]:
        """Run the canary steps in order against ``plugin_root``; stop at the first that fails. Each step's argv may
        name ``{plugin_root}`` (the installed version's directory) and ``{python}``."""
        out: list[dict[str, Any]] = []
        for step in spec.get("steps") or []:
            argv = [
                str(x)
                .replace("{plugin_root}", plugin_root)
                .replace("{python}", sys.executable)
                for x in step["argv"]
            ]
            started = time.monotonic()
            try:
                proc = subprocess.run(
                    argv,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=float(step.get("timeout_s") or CANARY_STEP_TIMEOUT_S),
                )
                rc, text = (proc.returncode, (proc.stdout or "") + (proc.stderr or ""))
            except subprocess.TimeoutExpired as exc:
                rc, text = (124, f"timed out after {exc.timeout:.0f}s")
            except OSError as exc:
                rc, text = (127, str(exc))
            out.append(
                {
                    "name": str(step.get("name") or argv[0]),
                    "rc": rc,
                    "elapsed_s": round(time.monotonic() - started, 1),
                    "output": text[-CANARY_OUTPUT_MAX:],
                }
            )
            if rc != 0:
                break
        return out

    def canary_fix_brief(self, failed: Mapping[str, Any], target: str) -> str:
        steps = "\n\n".join(
            f"### {s['name']} (exit {s['rc']}, {s['elapsed_s']}s)\n\n```text\n{s['output'].rstrip()}\n```"
            for s in failed["steps"]
        )
        back = failed["rolled_back_to"]
        return (
            f"# Lane {CANARY_FIX_PREFIX}{failed['sha'][:10]}: the install canary failed for plugin {failed['version']}\n\nadopt-merged ({target}) installed plugin {failed['version']} at {failed['sha']} on {failed['host']} at {failed['at']}; its canary smoke failed, and the host was rolled back to {back['version']} at {back['sha']}. The failed commit is held: no host installs it and no lab lane is shipped it until a newer merge passes.\n\n## Task\n\nFind why the step below fails at that commit and fix it on main by pull request. TDD: the smoke output below is the first failing case. Hand the pull request off needs=land. Ticket: the one this brief is dispatched with.\n\n## Smoke output\n\n"
            + steps
            + "\n"
        )

    def queue_canary_fix(
        self,
        spec: Mapping[str, Any],
        failed: Mapping[str, Any],
        record: Path,
        target: Any,
        env: Mapping[str, str],
    ) -> str:
        """Queue one fix lane for the failed commit in the remote-lane queue the throughput tick starts from (OMN-19998).
        Returns the lane name, or an ``unqueued:`` reason."""
        fix = spec.get("fix_lane") or {}
        lane = f"{CANARY_FIX_PREFIX}{failed['sha'][:10]}"
        brief = record.parent / f"{lane}.md"
        try:
            self.runtime["write_atomic"](
                brief, self.canary_fix_brief(failed, target).encode(), "100644"
            )
            argv = [
                "run",
                "--detach",
                "--no-placement-wait",
                "--brief",
                str(brief),
                "--lane",
                lane,
                "--model",
                str(fix.get("model") or "opus"),
                "--priority-class",
                str(fix.get("priority_class") or "process-fix"),
            ]
            for flag, key in (
                ("--ticket", "ticket"),
                ("--repo", "repo"),
                ("--parent", "parent"),
                ("--effort", "effort"),
            ):
                if fix.get(key):
                    argv += [flag, str(fix[key])]
            lp = self._placement()
            lp.lane_queue_enqueue(
                lp.placement_dir(env),
                lane,
                argv=argv,
                model=str(fix.get("model") or "opus"),
                priority_class=str(fix.get("priority_class") or "process-fix"),
                pin=None,
                run_id=f"adopt-canary-{failed['sha'][:10]}",
                reason=f"install canary failed for {failed['version']} at {failed['sha'][:10]}",
            )
        except (OSError, ValueError, ImportError) as exc:
            return f"unqueued:{exc}"
        return lane

    def canary_install(
        self,
        target: Any,
        res: Any,
        mk: Any,
        installed_file: Path,
        before: bytes,
        prior: tuple[str, str | None],
        want: str,
        want_v: str,
    ) -> None:
        """The canary after ``update_command`` installed ``want_v`` (``want``): pass records it; fail rolls back."""
        spec = target.spec["canary"]
        record_path = self.canary_record_path(spec, target.env)
        plugin = target.spec["plugin"]
        try:
            plugin_root = json.loads(installed_file.read_text(encoding="utf-8"))[
                "plugins"
            ][plugin][0]["installPath"]
            steps = self.run_canary(spec, str(plugin_root))
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            steps = [
                {
                    "name": "installed-version-read",
                    "rc": 1,
                    "elapsed_s": 0.0,
                    "output": str(exc),
                }
            ]
        record = self.runtime["load_state"](record_path)
        now, host = (
            self.runtime["stamp"](self.runtime["utc_now"]()),
            socket.gethostname().split(".")[0],
        )
        passed = all(s["rc"] == 0 for s in steps)
        entry: dict[str, Any] = {
            "sha": want,
            "version": want_v,
            "at": now,
            "host": host,
            "steps": steps,
        }
        event: dict[str, Any] = {
            "ts": now,
            "kind": "canary",
            "host": host,
            "target": target.name,
            "plugin": plugin,
            "sha": want,
            "version": want_v,
            "verdict": "pass" if passed else "fail",
            "steps": [{k: s[k] for k in ("name", "rc", "elapsed_s")} for s in steps],
        }
        if passed:
            record["passed"] = entry
            record.pop("failed", None)
            res.actions.append(
                f"canary-passed {plugin} {want_v} ({len(steps)} step(s))"
            )
        else:
            old_head, old_v = prior
            errors: list[str] = []
            try:
                self.runtime["write_atomic"](installed_file, before, "100644")
            except OSError as exc:
                errors.append(f"installed_plugins.json not restored: {exc}")
            try:
                # Preserve local changes, or let the rollback report a refusal.
                mk.run("reset", "-q", "--keep", old_head)
            except RuntimeError as exc:
                errors.append(f"marketplace clone not reset to {old_head[:10]}: {exc}")
            entry["rolled_back_to"] = {"sha": old_head, "version": old_v}
            record["failed"] = entry
            res.errors.extend(
                f"{target.name}: canary rollback incomplete: {e}" for e in errors
            )
            entry["fix_lane"] = self.queue_canary_fix(
                spec, entry, record_path, target.name, target.env
            )
            event.update(
                rolled_back_to=old_head,
                rolled_back_version=old_v,
                rollback_errors=errors,
                fix_lane=entry["fix_lane"],
            )
            bad = steps[-1]
            res.actions.append(
                f"canary-failed {plugin} {want_v} at {bad['name']} exit {bad['rc']}; rolled back to {old_v} at {old_head[:10]}; fix lane {entry['fix_lane']}"
            )
        self.runtime["save_state"](record_path, record)
        sys.stdout.write(
            f"ADOPTION-CANARY {self.runtime['emit_event'](record_path, event)}\n"
        )

    def canary_hold(self, target: Any, want: str | None) -> Any:
        """The finding that holds ``want`` back when its canary failed on this host, else None. A marketplace target reads
        the record its ``canary`` names; a plugin-cache target the one its ``canary_record`` names, so its copies of the
        failed commit's files wait too and a rolled-back host is not handed them again by the copy step."""
        if target.kind == "plugin-cache":
            spec = (
                {"record": target.spec["canary_record"]}
                if target.spec.get("canary_record")
                else None
            )
        else:
            spec = target.spec.get("canary")
        if not spec or not want:
            return None
        failed = self.runtime["load_state"](
            self.canary_record_path(spec, target.env)
        ).get("failed")
        if not isinstance(failed, dict) or failed.get("sha") != want:
            return None
        bad = (failed.get("steps") or [{}])[-1]
        back = failed.get("rolled_back_to") or {}
        note = f"canary failed for {str(want)[:10]} ({failed.get('version')}) at {failed.get('at')} on {failed.get('host')}: {bad.get('name')} exit {bad.get('rc')}; rolled back to {back.get('version')}; fix lane {failed.get('fix_lane')}"
        finding = self.runtime["Finding"](
            target.name,
            "installed",
            str(target.spec.get("plugin")),
            "canary-failed",
            note,
        )
        finding.refusal_key = str(want)
        return finding

    def adopt_marketplace(self, target: Any, git: Any, dry_run: bool) -> Any:
        """Fast-forward the marketplace clone new sessions install from, and update the installed plugin.

        A locally modified file whose content is a merged version is restored first (its merged
        successor arrives with the fast-forward); a modification that never merged is left and reported,
        and blocks nothing else."""
        res = self.runtime["TargetResult"]()
        clone = self.runtime["expand"](target.spec["path"], target.env)
        mk = self.runtime["Git"](clone)
        if not (clone / ".git").exists():
            res.errors.append(f"{target.name}: no clone at {clone.name}")
            return res
        if not dry_run and target.spec.get("fetch", True):
            try:
                mk.run("fetch", "-q", "origin", target.branch, timeout=90)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                res.errors.append(f"{target.name}: fetch failed: {exc}")
        hist = mk.history(target.ref, target.prefixes or ["."])
        for line in mk.run(
            "status", "--porcelain", "--untracked-files=no"
        ).splitlines():
            path = line[3:]
            data = self.runtime["read_bytes"](clone / path)
            head_blob = mk.run(
                "rev-parse", "-q", "--verify", f"HEAD:{path}", check=False
            ).strip()
            want_blob = mk.run(
                "rev-parse", "-q", "--verify", f"{target.ref}:{path}", check=False
            ).strip()
            covered = data is not None and self.runtime["blob_sha"](data) in hist.get(
                path, set()
            )
            if data is not None and (not covered) and head_blob and want_blob:
                covered = self.runtime["merge_file"](
                    data, mk.cat(head_blob), mk.cat(want_blob)
                ) == mk.cat(want_blob)
            if covered:
                if dry_run:
                    res.actions.append(f"would-restore {target.name}:{path}")
                else:
                    mk.run("checkout", "--", path)
                    res.actions.append(f"restored {target.name}:{path}")
            elif data is not None:
                res.findings.append(
                    self.runtime["Finding"](
                        target.name,
                        "clone",
                        path,
                        "unmerged",
                        f"in={self.runtime['locate'](mk.where(self.runtime['blob_sha'](data), path, target.ref), target.repo, target.env)}",
                    )
                )
        head, want = (mk.rev("HEAD"), mk.rev(target.ref))
        canonical = git.rev(target.ref)
        if dry_run and canonical and (canonical != want):
            if mk.rev(canonical) is None:
                res.findings.append(
                    self.runtime["Finding"](
                        target.name,
                        "clone",
                        "HEAD",
                        "stale",
                        f"marketplace clone lacks merged {canonical[:10]}",
                    )
                )
                want = None
            else:
                want = canonical
        plugin = target.spec.get("plugin")
        held = self.canary_hold(target, want) if plugin else None
        if held:
            res.findings.append(held)
            return res
        manifest_rel = target.spec.get(
            "plugin_manifest", "plugins/omni/.claude-plugin/plugin.json"
        )
        installed_file = self.runtime["expand"](
            target.spec.get(
                "installed_plugins", "~/.claude/plugins/installed_plugins.json"
            ),
            target.env,
        )
        if plugin and want:
            try:
                want_v = json.loads(mk.run("show", f"{want}:{manifest_rel}"))["version"]
                have = json.loads(installed_file.read_text(encoding="utf-8"))[
                    "plugins"
                ].get(plugin, [])
                have_v = have[0]["version"] if have else None
            except (
                OSError,
                ValueError,
                KeyError,
                IndexError,
                TypeError,
                RuntimeError,
            ) as exc:
                res.errors.append(f"{target.name}: version read failed: {exc}")
                return res
            if have_v != want_v:
                refusal = self.runtime["plugin_update_refusal"](
                    clone, want, want_v, have, have_v
                ) or self.runtime["_tree"].candidate_smoke_refusal(
                    clone, want, want_v, have, have_v
                )
                if refusal:
                    finding = self.runtime["Finding"](
                        target.name, "installed", plugin, "refused", refusal[0]
                    )
                    finding.refusal_key = refusal[1]
                    res.findings.append(finding)
                    return res
        if head and want and (head != want):
            if not mk.is_ancestor(head, want):
                res.findings.append(
                    self.runtime["Finding"](
                        target.name,
                        "clone",
                        "HEAD",
                        "diverged",
                        f"{head[:10]} vs {want[:10]}",
                    )
                )
            elif dry_run:
                res.actions.append(f"would-ff {target.name} {head[:10]}..{want[:10]}")
            else:
                try:
                    mk.run("merge", "--ff-only", "-q", target.ref)
                    res.actions.append(f"ff {target.name} {head[:10]}..{want[:10]}")
                except RuntimeError as exc:
                    res.findings.append(
                        self.runtime["Finding"](
                            target.name, "clone", "HEAD", "stale", f"ff refused: {exc}"
                        )
                    )
        manifest = clone / manifest_rel
        if not dry_run and want and (mk.rev("HEAD") != want):
            return res
        try:
            want_v = json.loads(manifest.read_text(encoding="utf-8"))["version"]
            have = json.loads(installed_file.read_text(encoding="utf-8"))[
                "plugins"
            ].get(plugin, [])
            have_v = have[0]["version"] if have else None
        except (OSError, ValueError, KeyError, IndexError) as exc:
            res.errors.append(f"{target.name}: version read failed: {exc}")
            return res
        if plugin and have_v != want_v:
            cmd = target.spec.get("update_command")
            if not cmd:
                res.findings.append(
                    self.runtime["Finding"](
                        target.name,
                        "installed",
                        plugin,
                        "stale",
                        f"{have_v} < {want_v}",
                    )
                )
            elif dry_run:
                res.actions.append(f"would-update {plugin} {have_v}->{want_v}")
            else:
                before = installed_file.read_bytes()
                try:
                    proc = subprocess.run(
                        cmd,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=self.runtime["INSTALL_TIMEOUT"],
                    )
                    ok = proc.returncode == 0
                except (OSError, subprocess.TimeoutExpired) as exc:
                    ok, proc = (False, None)
                    res.errors.append(f"{target.name}: update failed: {exc}")
                if ok:
                    res.actions.append(
                        f"updated {plugin} {have_v}->{want_v} (new sessions)"
                    )
                    if target.spec.get("canary") and head and want:
                        self.canary_install(
                            target,
                            res,
                            mk,
                            installed_file,
                            before,
                            (head, have_v),
                            want,
                            want_v,
                        )
                elif proc is not None:
                    res.findings.append(
                        self.runtime["Finding"](
                            target.name,
                            "installed",
                            plugin,
                            "stale",
                            f"{have_v} < {want_v}; update exit {proc.returncode}",
                        )
                    )
        return res

    def canary_cap(
        self, run: Any, clone: Path, registry_root: Path, sha: str, tip: str
    ) -> str | None:
        """The newest merged commit a lab lane may be shipped (OMN-17427): the version rolls to the lab hosts only after the
        launching host's install canary passed it. With no canary record (no canary declared on this host) the merged tip
        ships, as before. With one, the commit it names as passed ships when it lies between ``sha`` and ``tip``; a record
        with no passed commit, an unreadable one, or a passed commit off that range ships nothing newer than ``sha``."""
        path = registry_root / CANARY_RECORD_REL
        if not path.exists():
            return tip
        try:
            passed = (
                json.loads(path.read_text(encoding="utf-8")).get("passed") or {}
            ).get("sha")
        except (OSError, ValueError, AttributeError):
            return None
        if (
            not isinstance(passed, str)
            or not self.runtime["SHA_RE"].match(passed)
            or passed == sha
        ):
            return None
        if passed == tip:
            return tip
        for older, newer in ((sha, passed), (passed, tip)):
            if (
                run(
                    [
                        "git",
                        "-C",
                        str(clone),
                        "merge-base",
                        "--is-ancestor",
                        older,
                        newer,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                ).returncode
                != 0
            ):
                return None
        return passed

    def installed_commit(self, claude_dir: Path, plugin: str) -> str:
        """The commit the launching host has installed for ``plugin`` (``gitCommitSha`` in its ``installed_plugins.json``),
        or "" when the record is absent, unreadable or names no full commit (OMN-17427). User scope wins over any other."""
        try:
            record = json.loads(
                (claude_dir / "plugins" / "installed_plugins.json").read_text(
                    encoding="utf-8"
                )
            )
            entries = (record.get("plugins") or {}).get(plugin) or []
        except (OSError, json.JSONDecodeError, AttributeError):
            return ""
        entries = (
            [e for e in entries if isinstance(e, dict)]
            if isinstance(entries, list)
            else []
        )
        for entry in sorted(entries, key=lambda e: e.get("scope") != "user"):
            sha = entry.get("gitCommitSha")
            if isinstance(sha, str) and self.runtime["SHA_RE"].match(sha):
                return sha
        return ""

    def shipped_plugin_sha(
        self,
        run: Any,
        claude_dir: Path,
        plugin: str,
        mroot: Path,
        slug: str,
        sha: str,
        subdir: str,
        registry_root: str | None,
    ) -> str | None:
        """The commit a lab lane runs for ``plugin`` in place of the marketplace commit ``sha`` (OMN-17427), else None.

        Lab lanes ran the merged tip of main, never the commit the launching host had installed, so a branch install
        reached no lane until it merged and four lab hosts were patched by hand on 2026-10-22. The installed commit is the
        launching host's own record, and it ships as recorded (no canary cap: this host already runs it). A plugin with
        no recorded commit takes ``merged_plugin_sha``."""
        return self.installed_commit(claude_dir, plugin) or self.merged_plugin_sha(
            run, mroot, slug, sha, subdir, registry_root
        )

    def publish_plugin_commit(
        self,
        source: Any,
        local_registry_root: str | None,
        *,
        run: Any = subprocess.run,
        url: str | None = None,
    ) -> str | None:
        """Put ``source``'s commit on its GitHub repository so a lab host can fetch it, else the reason it could not be (OMN-17427).

        The commit a lane ships is the install record's, and ``install-tree --with-installed`` makes that a merge commit that
        lives in the canonical clone only (``refs/adopt-merged/<sha>``), so the host's fetch of it fails and every lane would be
        refused until someone pushed it by hand. The commit is copied into a fresh bare repository and pushed from there, to
        ``refs/plugin-ship/<sha12>`` (no branch, no CI, nothing a pull request lists): the canonical clone's own hooks are not
        the gate of a commit that is already installed and smoked."""
        clone = Path(local_registry_root or "") / source.cache_name
        held = run(
            ["git", "-C", str(clone), "cat-file", "-t", source.sha],
            capture_output=True,
            text=True,
            check=False,
        )
        if not local_registry_root or held.stdout.strip() != "commit":
            return f"no clone under {local_registry_root or '$ONEX_REGISTRY_ROOT'} holds {source.sha[:12]}"
        ref = PLUGIN_SHIP_REF.format(sha12=source.sha[:12])
        with tempfile.TemporaryDirectory(prefix="plugin-ship.") as tmp:
            stage = str(Path(tmp) / "stage.git")
            for argv in (
                ["git", "init", "--bare", "-q", stage],
                [
                    "git",
                    "-C",
                    stage,
                    "fetch",
                    "--no-tags",
                    "-q",
                    str(clone),
                    source.sha,
                ],
                [
                    "git",
                    "-C",
                    stage,
                    "push",
                    "-q",
                    url or source.url,
                    f"{source.sha}:{ref}",
                ],
            ):
                done = run(
                    argv,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=300,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                )
                if done.returncode != 0:
                    return f"{' '.join(argv[:4])} exit {done.returncode}: {(done.stderr or done.stdout).strip()[-300:]}"
        return None

    def ensure_plugin_trees(
        self,
        ensure: Any,
        sources: Sequence[Any],
        local_registry_root: str | None,
        receipt: dict[str, Any],
        echo: Any,
        lane: str,
        run: Any = subprocess.run,
    ) -> Any:
        """Run ``ensure`` (the host's plugin step); a commit the host cannot fetch (``PLUGIN-FETCH-FAILED``, exit 20) is published
        and the step runs once more (OMN-17427). The receipt's ``plugin_published`` says what was pushed."""
        done = ensure()
        unfetched = set(
            re.findall(r"^PLUGIN-FETCH-FAILED (\S+)$", done.stdout, re.MULTILINE)
        )
        if done.returncode != 20 or not unfetched:
            return done
        for src in (x for x in sources if x.plugin in unfetched):
            why = self.publish_plugin_commit(src, local_registry_root, run=run)
            receipt.setdefault("plugin_published", []).append(
                {"plugin": src.plugin, "sha": src.sha, "error": why}
            )
            echo(
                f"REMOTE-LANE-PLUGIN-PUBLISH lane={lane} {src.plugin} {src.sha[:12]}: {why or 'pushed'}"
            )
        return ensure()

    def tick_findings(
        self, now: datetime, env: Mapping[str, str]
    ) -> list[tuple[str, str, str, str]]:
        """The throughput tick's lane-ship line (OMN-17427, RULING 2026-10-22T12:12:56Z): ``lane_ship_drift`` over the launching
        host's receipts and Claude config, an unreadable one as one UNKNOWN so the tick still prints."""
        home = Path(env.get("HOME") or Path.home())
        try:
            return self.lane_ship_drift(
                Path(
                    env.get("ONEX_REMOTE_LANE_DIR")
                    or home / ".local/state/omni/remote-lanes"
                ),
                Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude"),
                now,
            )
        except Exception as exc:
            return [
                (
                    "unknown",
                    "lane-ship",
                    " ".join(str(exc).split())[:200] or type(exc).__name__,
                    LANE_SHIP_FIX,
                )
            ]

    def receipt_plugins(
        self, sources: Sequence[Any], claude_dir: Path
    ) -> list[dict[str, Any]]:
        """The receipt's plugin entries: each shipped source and ``installed``, the commit the launching host's install record
        named for it when the lane launched (OMN-17427), read apart from the commit chosen to ship so ``lane_ship_drift``
        can tell a lane shipped something else."""
        return [
            {**asdict(s), "installed": self.installed_commit(claude_dir, s.plugin)}
            for s in sources
        ]

    def lane_ship_drift(
        self,
        receipts: Path,
        claude_dir: Path,
        now: datetime,
        window_h: float = LANE_SHIP_WINDOW_H,
        limit: int = LANE_SHIP_REPORT_MAX,
    ) -> list[tuple[str, str, str, str]]:
        """Lab lanes of the last ``window_h`` hours that ran a plugin commit other than the one the launching host had
        installed (OMN-17427), as the tick's findings ``(level, loop, why, fix)``, newest first, at most ``limit`` and a count.

        The runner shipped the merged tip of main for months, so a branch install reached no lab lane until it merged and four
        lab hosts were patched by hand on 2026-10-22 (operator ruling 2026-10-22T12:12:56Z: fix it in automation). A lane's
        receipt names the commit it shipped (``plugins[].sha``) and the commit the install record named when it launched
        (``plugins[].installed``). A receipt from before that field is judged against the record only when it started after the
        record was last written, so an install since then never reads as drift. A lane that placed nowhere shipped nothing."""
        try:
            record_at = (
                (claude_dir / "plugins" / "installed_plugins.json").stat().st_mtime
            )
        except OSError:
            record_at = None
        since = now.timestamp() - window_h * 3600
        found: list[tuple[float, str, str, str]] = []
        try:
            lanes = [
                e
                for e in os.scandir(receipts)
                if e.is_dir() and e.stat().st_mtime >= since
            ]
        except OSError:
            return []
        for lane in lanes:
            for entry in os.scandir(lane.path):
                if (
                    not entry.name.startswith("rlane-")
                    or entry.name.endswith(".job.json")
                    or not entry.name.endswith(".json")
                ):
                    continue
                try:
                    receipt = json.loads(Path(entry.path).read_text(encoding="utf-8"))
                    started = datetime.fromisoformat(
                        str(receipt["started_at"]).replace("Z", "+00:00")
                    ).timestamp()
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                if (
                    started < since
                    or not receipt.get("host")
                    or not isinstance(receipt.get("plugins"), list)
                ):
                    continue
                for ship in receipt["plugins"]:
                    if not isinstance(ship, dict) or not isinstance(
                        ship.get("plugin"), str
                    ):
                        continue
                    want = ship.get("installed") or (
                        self.installed_commit(claude_dir, ship["plugin"])
                        if record_at is not None and started >= record_at
                        else ""
                    )
                    if want and ship.get("sha") != want:
                        found.append(
                            (
                                started,
                                str(receipt.get("lane") or lane.name),
                                f"lane {receipt.get('lane') or lane.name} on {receipt['host']} ran {ship['plugin']} at "
                                f"{str(ship.get('sha'))[:12]}, the commit the launching host had installed was {want[:12]}",
                                "read `resolve_plugins` in omni/skills/remote-lane/scripts/onex_remote_lane.py: a lane is shipped "
                                "the install record's gitCommitSha (OMN-17427); fix what chose another commit, and never "
                                "hand-replace a shipped tree under ~/lab-run/plugins",
                            )
                        )
                        break
        found.sort(reverse=True)
        out = [
            ("missing", f"lane-ship:{lane}", why, fix)
            for _, lane, why, fix in found[:limit]
        ]
        if len(found) > limit:
            level, loop, why, fix = out[-1]
            out[-1] = (
                level,
                loop,
                f"{why} (and {len(found) - limit} more lane(s) in the last {window_h:g} h)",
                fix,
            )
        return out

    def merged_plugin_sha(
        self,
        run: Any,
        mroot: Path,
        slug: str,
        sha: str,
        subdir: str,
        registry_root: str | None,
    ) -> str | None:
        """The merged commit a lane should run in place of the marketplace commit ``sha`` (OMN-17427), else None.

        The marketplace clone moves only when adoption updates it, so lanes ran an older plugin than main while adoption was
        down. The canonical clone ``$ONEX_REGISTRY_ROOT/<repo>`` is synced every few minutes; its ``origin/<default>`` is what is merged.
        The merged commit ships when the marketplace clone tracks that same branch, ``sha`` is on its history, and a merge since
        changed the plugin's directory. A marketplace on another branch (a release), a commit off the merged history (a local
        one) or no canonical clone keeps ``sha``. Each lane gets its own tree per commit, so a running lane keeps its copy."""
        if not registry_root:
            return None
        clone = Path(registry_root) / slug.split("/", 1)[1]
        if not (clone / ".git").exists():
            clone = Path(registry_root).parent / slug.split("/", 1)[1]
        if not (clone / ".git").exists():
            return None
        default = self.runtime["_git"](
            run, clone, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"
        )
        if (
            not default.startswith("origin/")
            or self.runtime["_git"](
                run, mroot, "rev-parse", "--abbrev-ref", "HEAD@{upstream}"
            )
            != default
        ):
            return None
        tip = self.runtime["_git"](
            run, clone, "rev-parse", "--verify", "-q", f"{default}^{{commit}}"
        )
        if not self.runtime["SHA_RE"].match(tip) or tip == sha:
            return None
        ancestor = run(
            ["git", "-C", str(clone), "merge-base", "--is-ancestor", sha, tip],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if ancestor.returncode != 0:
            return None
        tip = self.canary_cap(run, clone, Path(registry_root), sha, tip)
        if tip is None:
            return None
        paths = [subdir]
        link = f"{subdir}/src"
        entry = self.runtime["_git"](run, clone, "ls-tree", tip, "--", link)
        if entry.startswith("120000 "):
            source = self.runtime["_git"](run, clone, "show", f"{tip}:{link}")
            mounted = posixpath.normpath(posixpath.join(subdir, source))
            if not mounted.startswith(("/", "../")) and mounted != "..":
                paths.append(mounted)
        changed = run(
            ["git", "-C", str(clone), "diff", "--quiet", sha, tip, "--", *paths],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        return tip if changed.returncode == 1 else None

    def source_path(self, target: Any, strip: str, rel: str) -> str:
        """The repository path that an installed copy's ``rel`` path is adopted from."""
        mounts = {strip: "", **target.spec.get("source_mounts", {})}
        for source, destination in sorted(
            mounts.items(), key=lambda item: len(item[1]), reverse=True
        ):
            if rel.startswith(destination):
                return str(source + rel[len(destination) :])
        return strip + rel

    def scan_copy(
        self,
        target: Any,
        git: Any,
        label: str,
        root: Path,
        strip: str,
        only: Sequence[str] | None = None,
    ) -> list[tuple[str, Path, str, tuple[str, str] | None]]:
        """``(source path, installed path, class, merged (mode, blob))`` for the copy's files."""
        # The ref resolves to one commit here: the tree, its history and the superseded check below all
        # read it, so a ref that moves mid-scan cannot split a script and its source across two merges.
        ref = git.rev(target.ref) or target.ref
        merged = git.tree(ref, target.prefixes)
        hist = git.history(ref, target.prefixes)
        files = target.spec.get("files")  # installed targets that copy a named subset
        rows = []
        if files:
            pairs = [
                (src, self.runtime["expand"](dst, target.env))
                for src, dst in files.items()
            ]
        else:
            mounts = {strip: "", **target.spec.get("source_mounts", {})}

            destinations = {}
            for source, destination in mounts.items():
                for src in merged:
                    if src.startswith(source):
                        dest = root / destination / src[len(source) :]
                        # Plugin installers materialize source-directory links. The mounted
                        # files, rather than the link blob, are the installed content.
                        if merged[src][0] == "120000" and any(
                            str(dest.relative_to(root)) + "/" == mount
                            for mount in mounts.values()
                        ):
                            continue
                        destinations[src] = dest
            if target.spec.get("compare", "merged") == "both" and root.is_dir():
                for f in root.rglob("*"):
                    if f.is_file():
                        rel = f.relative_to(root).as_posix()
                        if not self.runtime["ignored"](rel):
                            destinations[self.source_path(target, strip, rel)] = f
            pairs = sorted(destinations.items())
        for src, dest in pairs:
            if only is not None and src not in only:
                continue
            if self.runtime["ignored"](
                dest.relative_to(root).as_posix() if not files else src
            ) or src in target.spec.get("exclude", []):
                continue
            data = self.copy_bytes(dest, merged[src][0] if src in merged else None)
            cls = self.runtime["classify"](data, merged.get(src), hist.get(src, set()))
            if (
                cls == "unmerged"
                and data is not None
                and merged.get(src) is not None
                and self.runtime["superseded"](
                    git, ref, src, dest, data, merged[src][1]
                )
            ):
                cls = "superseded"
            rows.append((src, dest, cls, merged.get(src)))
        return rows

    def stage_file(self, dest: Path, data: bytes, mode: str) -> Path:
        """Write ``data`` beside ``dest`` under a temporary name; ``os.replace`` of the result installs it."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if (
            mode == LINK_MODE
        ):  # a merged symlink goes in as the link git stores, never as a file
            link = dest.parent / f".{dest.name}.{os.getpid()}.link"
            link.unlink(missing_ok=True)
            os.symlink(os.fsdecode(data), link)
            return link
        fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", dir=dest.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, 0o700 if mode == "100755" else 0o600)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise
        return Path(tmp)

    def write_atomic(self, dest: Path, data: bytes, mode: str) -> None:
        os.replace(self.stage_file(dest, data, mode), dest)

    def install_set(self, items: Sequence[tuple[Path, bytes, str]]) -> None:
        """Install ``(dest, data, mode)`` files as one set: all of them or none (OMN-20497).

        A script and the source it imports are adopted from one merge, so a copy never holds the
        new script over the old source. Every file is staged beside its destination first, then
        swapped in order; a staging or swap failure removes the staged files and puts back each
        file already swapped. ``InstallSetError`` says whether the copy is whole as it was."""
        staged: list[tuple[Path, Path]] = []
        done: list[tuple[Path, tuple[bytes, str] | None]] = []
        try:
            for dest, data, mode in items:
                staged.append((dest, self.stage_file(dest, data, mode)))
            for dest, tmp in staged:
                done.append((dest, self._previous(dest)))
                os.replace(tmp, dest)
        except OSError as exc:
            lost = []
            for dest, old in reversed(done):
                try:
                    if old is None:
                        dest.unlink(missing_ok=True)
                    else:
                        self.write_atomic(dest, *old)
                except OSError as back:
                    lost.append(f"{dest.name}: {back}")
            for _dest, tmp in staged:
                tmp.unlink(missing_ok=True)
            if lost:
                raise InstallSetError(
                    f"PARTIAL install ({exc}); not put back: {'; '.join(lost)}"
                ) from exc
            raise InstallSetError(f"nothing installed: {exc}") from exc

    def install_stale(
        self,
        git: Any,
        rows: Sequence[tuple[str, Path, str, tuple[str, str] | None]],
    ) -> str | None:
        """Install the merged blob of each scanned row as one set; the failure text, or None when it landed.

        Every blob is read before any file is written, and the source under ``src/`` goes in ahead of
        the scripts that import it (OMN-20497). The installed plugin tree is never installed this way
        (OMN-17427): it goes in whole, through node_plugin_tree_install_effect."""
        ordered = sorted(rows, key=lambda r: not r[0].startswith("src/"))
        try:
            items = [(r[1], git.cat(r[3][1]), r[3][0]) for r in ordered if r[3]]
            self.install_set(items)
        except InstallSetError as exc:
            return str(exc)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"nothing installed: {exc}"
        return None

    def _previous(self, dest: Path) -> tuple[bytes, str] | None:
        """``(data, mode)`` of the installed ``dest`` that ``install_set`` may have to put back."""
        if dest.is_symlink():
            data = self.copy_bytes(dest, LINK_MODE)
            return None if data is None else (data, LINK_MODE)
        data = self.read_bytes(dest)
        if data is None:
            return None
        return data, "100755" if dest.stat().st_mode & 0o111 else "100644"

    def keep_held(self, paths: Sequence[Path]) -> list[tuple[Path, bytes, int]]:
        """``(path, bytes, mode)`` of each unmerged file and its ``.bak*`` backups, read before an
        installer that replaces the copy whole, so ``restore_held`` can put them back (OMN-20863).
        A symlink is kept as its link text with mode ``LINK_BITS``."""
        kept = []
        for dest in paths:
            for p in (dest, *self.runtime["backups_of"](dest)):
                if p.is_symlink():
                    data = self.copy_bytes(p, LINK_MODE)
                    if data is not None:
                        kept.append((p, data, LINK_BITS))
                    continue
                data = self.read_bytes(p)
                if data is not None:
                    kept.append((p, data, p.stat().st_mode & 0o777))
        return kept

    def restore_held(self, kept: Sequence[tuple[Path, bytes, int]]) -> list[str]:
        """Write back every kept file the installer replaced or removed; the paths it could not restore."""
        failed = []
        for p, data, mode in kept:
            link = mode == LINK_BITS
            if (
                p.is_symlink() == link
                and (self.copy_bytes(p, LINK_MODE) if link else self.read_bytes(p))
                == data
            ):
                continue
            try:
                self.write_atomic(
                    p,
                    data,
                    LINK_MODE if link else "100755" if mode & 0o111 else "100644",
                )
            except OSError as exc:
                failed.append(f"{p.name}: {exc}")
        return failed

    def read_bytes(self, p: Path) -> bytes | None:
        try:
            return p.read_bytes() if p.is_file() else None
        except OSError:
            return None

    def copy_bytes(self, p: Path, mode: str | None) -> bytes | None:
        """The bytes git hashes for installed ``p`` when its merged blob has ``mode``: for a merged
        symlink the link's own target text, as git stores it, else the file's content. Read through
        the link, every merged symlink classed unmerged and was held in place on every run (OMN-17427)."""
        if mode == LINK_MODE and p.is_symlink():
            try:
                return os.fsencode(os.readlink(p))
            except OSError:
                return None
        return self.read_bytes(p)

    def tree_sha256(self, root: Path) -> str:
        """Hash sorted paths, file contents, modes and symlinks, excluding pin metadata."""
        digest = hashlib.sha256()

        def visit(directory: Path) -> None:
            for path in sorted(directory.iterdir()):
                rel = path.relative_to(root).as_posix()
                if self.runtime["ignored"](path.name) or rel == ".pin.json":
                    continue
                materialized = rel == "src" and path.is_symlink() and path.is_dir()
                if path.is_symlink() and not materialized:
                    kind, data = "link", os.readlink(path).encode()
                elif path.is_dir():
                    kind, data = "dir", b""
                else:
                    kind, data = "file", path.read_bytes()
                header = json.dumps(
                    [
                        rel,
                        kind,
                        (path.stat() if materialized else path.lstat()).st_mode & 0o777,
                        len(data),
                    ]
                )
                digest.update(header.encode() + b"\0" + data)
                if kind == "dir":
                    visit(path)

        visit(root)
        return digest.hexdigest()

    def pin(self, root: Path, out: Path, entries: list[dict[str, str]]) -> Path:
        manifest = json.loads(
            (root / ".claude-plugin/plugin.json").read_text(encoding="utf-8")
        )
        version = manifest["version"]
        if (
            not isinstance(version, str)
            or not version
            or version in (".", "..")
            or Path(version).name != version
        ):
            raise ValueError("invalid plugin version")
        if out.resolve().is_relative_to(root.resolve()):
            raise ValueError("pin output must be outside the source tree")
        digest = self.tree_sha256(root)
        dest = out / f"{version}-{digest[:8]}"
        if dest.exists():
            self.runtime["verify_pin"](dest)
            if self.tree_sha256(dest) != digest:
                raise ValueError("existing pin has a different digest")
            return dest
        out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".pin-", dir=out) as temporary:
            staging = Path(temporary) / "tree"
            shutil.copytree(
                root,
                staging,
                symlinks=True,
                ignore=shutil.ignore_patterns(*self.runtime["IGNORE"]),
            )
            source = root / "src"
            if source.is_symlink() and source.is_dir():
                # A standalone pin must carry the same canonical source that the
                # plugin installer materializes; other symlinks retain their identity.
                (staging / "src").unlink()
                shutil.copytree(
                    source,
                    staging / "src",
                    symlinks=True,
                    ignore=shutil.ignore_patterns(*self.runtime["IGNORE"]),
                )
            if self.tree_sha256(staging) != digest:
                raise ValueError("source tree changed while pinning")
            metadata = {
                "source": str(root.resolve()),
                "version": version,
                "pinned_at": datetime.now(UTC).isoformat(),
                "markers": entries,
                "tree_sha256": digest,
            }
            (staging / ".pin.json").write_text(
                json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
            )
            staging.rename(dest)
        return dest
