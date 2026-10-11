# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOccReplay: the OCC retirement S7 replay's I/O (OMN-20917).

For each merged PR of a repository, newest first, this handler gathers one
``ModelOccReplayRecord``:

* the merged PRs come from the repository's canonical clone under
  ``$OMNI_HOME`` (the squash commits on the default branch whose subject ends
  ``(#<n>)``), and each PR's head, GitHub login, labels and title from
  ``HandlerDodEvidenceGithubEffect`` (the PR watcher's records first); exemptions
  read that GitHub login, never the squash commit's git author name;
* OCC's recorded verdict is the newest run of ``occ_context`` on that head,
  read through the same effect handler, annotations included;
* the new path's verdict is the receipt gate's own sequence, replayed in a
  scratch clone that shares the canonical clone's objects: the dependency-bot
  and OCC-writer exemptions, the PR's own ``contracts/<ticket>.yaml`` at the
  head (or the contract-home lookup when it is absent), the verifier
  entrypoint the head step calls, then the must-fail control at the merge base
  with the PR's test side and cited contracts laid over it.

Differences from the live gate, each reported on the row's note rather than
guessed: the writer app's producer-outcome probe (a GitHub read) is not run, so
its exemption is the derived diff verdict alone; a ``Binds-Merged-Fix`` control
is not replayed (the row is not compared); the merge base is taken against the
squash commit's first parent, the base branch as the PR merged.

The classification itself is ``services/occ_replay.py``; nothing here decides
an outcome.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_dod_verify.handlers.dod_evidence_local_source import (
    canonical_clone,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_evidence_github_effect import (
    HandlerDodEvidenceGithubEffect,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_evidence_github_lookup import (
    EnumDodEvidenceGithubOperation,
    ModelDodEvidenceGithubLookupCommand,
    ModelDodEvidenceGithubLookupResultEvent,
)
from omnimarket.nodes.node_dod_verify.models.model_occ_replay import (
    ModelOccReplayRecord,
    ModelOccReplayReport,
    ModelOccReplayRequest,
)
from omnimarket.nodes.node_dod_verify.models.model_occ_verdict_difference import (
    ModelNewPathVerdict,
)
from omnimarket.nodes.node_dod_verify.services.occ_replay import (
    CONTRACT_HOME_REPOSITORIES,
    DEPENDENCY_BOT_AUTHORS,
    NEGATIVE_CONTROL_LABEL,
    NOT_COUNTED_OUTCOMES,
    OCC_WRITER_AUTHORS,
    carried_evidence_ids,
    classify_writer_app_exemption,
    is_test_only_diff,
    is_test_side,
    must_fail_control_line,
    replay_records,
    replay_row,
    tickets_from_title,
)
from omnimarket.nodes.node_dod_verify.services.occ_verdict_difference import (
    classify_dependency_repin,
    load_new_verdict,
    parse_occ_verdict,
)

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_S: Final[int] = 300
_SQUASH_SUBJECT: Final[re.Pattern[str]] = re.compile(r"^(?P<title>.*) \(#(?P<n>\d+)\)$")


@dataclass(frozen=True)
class _MergedPr:
    number: int
    merge_sha: str
    parent: str
    merged_at: str
    title: str


def _git(repo_dir: Path, *args: str) -> tuple[int, str]:
    """One git command in ``repo_dir``; ``(rc, stdout)``. Never raises."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
            env=scrub_git_location_env(os.environ),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("git %s in %s failed: %s", args[:2], repo_dir, exc)
        return 1, ""
    return proc.returncode, proc.stdout


def _show(repo_dir: Path, sha: str, path: str) -> str | None:
    rc, out = _git(repo_dir, "show", f"{sha}:{path}")
    return out if rc == 0 else None


def _default_branch(clone: Path) -> str:
    rc, out = _git(clone, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    ref = out.strip()
    return ref[len("origin/") :] if rc == 0 and ref.startswith("origin/") else "main"


class HandlerOccReplay:
    """EFFECT: gather replay records; ``handle`` classifies them."""

    def __init__(self, github: HandlerDodEvidenceGithubEffect | None = None) -> None:
        self._github = (
            github if github is not None else HandlerDodEvidenceGithubEffect()
        )

    def handle(self, request: ModelOccReplayRequest) -> ModelOccReplayReport:
        return replay_records(
            self.gather(request), repository=request.repository, count=request.count
        )

    # ------------------------------------------------------------ gather

    def gather(self, request: ModelOccReplayRequest) -> list[ModelOccReplayRecord]:
        clone = canonical_clone(request.repository)
        if clone is None:
            raise ValueError(
                f"no canonical clone of {request.repository} under $OMNI_HOME"
            )
        merged = self._merged_prs(clone, request.max_examined)
        work = request.work_dir.resolve()
        mirror = work / "clone" / clone.name
        if not (mirror / ".git").exists():
            mirror.parent.mkdir(parents=True, exist_ok=True)
            rc, _ = _git(
                mirror.parent,
                "clone",
                "--quiet",
                "--shared",
                "--no-checkout",
                str(clone),
                str(mirror),
            )
            if rc != 0:
                raise RuntimeError(f"could not create the scratch clone at {mirror}")
        records: list[ModelOccReplayRecord] = []
        compared = 0
        for pr in merged:
            if compared >= request.count:
                break
            record = self._record(request, clone, mirror, work, pr)
            records.append(record)
            if replay_row(record).outcome not in NOT_COUNTED_OUTCOMES:
                compared += 1
            logger.info(
                "replay %s#%d head=%s occ=%s new=%s compared=%d/%d: %s",
                request.repository,
                pr.number,
                record.head_sha[:12],
                (record.occ_check_run or {}).get("conclusion"),
                record.new_verdict.model_dump() if record.new_verdict else None,
                compared,
                request.count,
                record.note,
            )
        return records

    @staticmethod
    def _merged_prs(clone: Path, limit: int) -> list[_MergedPr]:
        branch = _default_branch(clone)
        rc, out = _git(
            clone,
            "log",
            "--first-parent",
            "--format=%H%x09%P%x09%cI%x09%s",
            f"refs/remotes/origin/{branch}",
        )
        if rc != 0:
            raise RuntimeError(f"cannot read origin/{branch} in {clone}")
        merged: list[_MergedPr] = []
        for line in out.splitlines():
            cols = line.split("\t", 3)
            if len(cols) != 4:
                continue
            match = _SQUASH_SUBJECT.match(cols[3].rstrip())
            if match is None:
                continue
            merged.append(
                _MergedPr(
                    number=int(match.group("n")),
                    merge_sha=cols[0],
                    parent=cols[1].split()[0] if cols[1] else "",
                    merged_at=cols[2],
                    title=match.group("title"),
                )
            )
            if len(merged) >= limit:
                break
        return merged

    def _lookup(
        self, command: ModelDodEvidenceGithubLookupCommand
    ) -> ModelDodEvidenceGithubLookupResultEvent:
        output = self._github.handle(command)
        event = output.events[0]
        if not isinstance(event, ModelDodEvidenceGithubLookupResultEvent):
            raise TypeError(f"unexpected lookup event {type(event).__name__}")
        return event

    def _record(
        self,
        request: ModelOccReplayRequest,
        clone: Path,
        mirror: Path,
        work: Path,
        pr: _MergedPr,
    ) -> ModelOccReplayRecord:
        facts_event = self._lookup(
            ModelDodEvidenceGithubLookupCommand(
                operation=EnumDodEvidenceGithubOperation.FETCH_PR_HEAD_FACTS,
                repo=request.repository,
                pr_number=pr.number,
            )
        )
        facts = facts_event.pr_head_facts
        title = (facts.title if facts and facts.title else "") or pr.title
        tickets = tickets_from_title(title)
        if facts is None:
            return ModelOccReplayRecord(
                pr=pr.number,
                merged_at=pr.merged_at,
                tickets=tickets,
                head_sha="",
                occ_unreadable=True,
                note=f"head sha unreadable: {facts_event.detail}",
            )
        occ_event = self._lookup(
            ModelDodEvidenceGithubLookupCommand(
                operation=EnumDodEvidenceGithubOperation.FETCH_HEAD_CHECK_RUN,
                repo=request.repository,
                pr_number=pr.number,
                head_sha=facts.head_sha,
                check_name=request.occ_context,
            )
        )
        occ = occ_event.check_run if occ_event.resolved else None
        negative_control = NEGATIVE_CONTROL_LABEL in facts.labels
        if not occ_event.resolved:
            note = f"OCC verdict unreadable ({occ_event.detail})"
        elif occ is None:
            note = f"no OCC run on the head ({occ_event.detail})"
        elif parse_occ_verdict(occ).admitted is None:
            note = f"OCC conclusion {occ.get('conclusion')} ({occ_event.detail})"
        else:
            note = f"OCC read from {occ_event.detail}"
        new = None
        # OMN-20917: only a head with no OCC run skips the new path;
        # negative controls always replay it.
        if not (occ_event.resolved and occ is None) or negative_control:
            new, how = self._new_path(
                request, clone, mirror, work, pr, facts.head_sha, facts.author, tickets
            )
            note = f"{how}; {note}"
        return ModelOccReplayRecord(
            pr=pr.number,
            merged_at=pr.merged_at,
            tickets=tickets,
            head_sha=facts.head_sha,
            occ_check_run=occ,
            occ_unreadable=not occ_event.resolved,
            new_verdict=new,
            negative_control=negative_control,
            note=note,
        )

    # ------------------------------------------------------------ new path

    def _new_path(
        self,
        request: ModelOccReplayRequest,
        clone: Path,
        mirror: Path,
        work: Path,
        pr: _MergedPr,
        head: str,
        author: str,
        tickets: tuple[str, ...],
    ) -> tuple[ModelNewPathVerdict | None, str]:
        login = author
        if login in DEPENDENCY_BOT_AUTHORS:
            return ModelNewPathVerdict(admitted=True), f"exempt: dependency bot {login}"
        # The canonical clone holds a merged PR's head; the replay never
        # fetches from GitHub (lanes read the canonical clones).
        if _git(mirror, "cat-file", "-e", f"{head}^{{commit}}")[0] != 0:
            return None, "new path not replayed: head commit not in the canonical clone"
        rc, out = _git(mirror, "merge-base", pr.parent, head)
        merge_base = out.strip()
        if rc != 0 or not merge_base:
            return None, "new path not replayed: merge base unresolvable"
        rc, out = _git(
            mirror, "diff", "--name-only", "--no-renames", "-z", merge_base, head
        )
        paths = [path for path in out.split("\0") if path]
        if rc != 0:
            return None, "new path not replayed: changed paths unreadable"
        if login in OCC_WRITER_AUTHORS:
            wanted = sorted({*paths, "CHANGELOG.md", "pyproject.toml"})
            contents = {
                path: (_show(mirror, head, path), _show(mirror, merge_base, path))
                for path in wanted
            }
            exempt, why = classify_writer_app_exemption(paths, contents=contents)
            if exempt:
                return ModelNewPathVerdict(admitted=True), f"exempt: writer app {why}"
        if not tickets:
            return ModelNewPathVerdict(admitted=False), "refused: title cites no ticket"

        dod = work / "dod" / str(pr.number)
        trees = work / "trees" / str(pr.number)
        shutil.rmtree(dod, ignore_errors=True)
        dod.mkdir(parents=True)
        try:
            how = self._verify(
                request, clone, mirror, dod, trees, head, merge_base, paths, tickets
            )
            if how is None:
                return None, "new path not replayed: Binds-Merged-Fix control"
            manifests = [p for p in paths if p.rsplit("/", 1)[-1] == "pyproject.toml"]
            manifest = manifests[0] if len(manifests) == 1 else ""
            repin, repin_why = classify_dependency_repin(
                paths,
                tickets=tickets,
                pyproject_head=_show(mirror, head, manifest) if manifest else None,
                pyproject_base=_show(mirror, merge_base, manifest)
                if manifest
                else None,
            )
            verdict = load_new_verdict(dod, tickets, dependency_repin=repin)
            if repin:
                how = f"{how}; dependency re-pin: {repin_why}"
            return verdict, how
        finally:
            for side in ("head", "base"):
                tree = trees / side / mirror.name
                if tree.exists():
                    _git(mirror, "worktree", "remove", "--force", str(tree))
            shutil.rmtree(trees, ignore_errors=True)
            _git(mirror, "worktree", "prune")

    def _verify(
        self,
        request: ModelOccReplayRequest,
        clone: Path,
        mirror: Path,
        dod: Path,
        trees: Path,
        head: str,
        merge_base: str,
        paths: list[str],
        tickets: tuple[str, ...],
    ) -> str | None:
        """Write head-, base- and contract-home- files the way the gate does.

        Returns how the new path ended, or None when the control would revert
        a merged fix, which the replay does not reproduce.
        """
        head_home = trees / "head"
        head_tree = head_home / mirror.name
        head_home.mkdir(parents=True, exist_ok=True)
        if _git(mirror, "worktree", "add", "--detach", str(head_tree), head)[0] != 0:
            raise RuntimeError(f"cannot check out {head[:12]} in the scratch clone")
        owner = request.repository.split("/", 1)[0]
        head_results: dict[str, object] = {}
        for ticket in tickets:
            contract = head_tree / "contracts" / f"{ticket}.yaml"
            if not contract.is_file():
                home = self._contract_home(owner, mirror.name, ticket)
                if home:
                    (dod / f"contract-home-{ticket}.txt").write_text(
                        f"{home}\n", encoding="utf-8"
                    )
                    return f"head refused: contracts/{ticket}.yaml is in {home}"
                return f"head refused: no contracts/{ticket}.yaml at the head"
            result = self._run_verifier(
                request, head_home, head_tree, ticket, contract, dod / f"head-{ticket}"
            )
            head_results[ticket] = result
            if not (isinstance(result, dict) and result.get("status") == "verified"):
                status = result.get("status") if isinstance(result, dict) else None
                return f"head refused: {ticket} status={status}"

        rc, trailers = _git(
            mirror,
            "log",
            "--format=%(trailers:key=Binds-Merged-Fix,valueonly)",
            f"{merge_base}..{head}",
        )
        if rc == 0 and trailers.strip():
            return None
        base_home = trees / "base"
        base_tree = base_home / mirror.name
        base_home.mkdir(parents=True, exist_ok=True)
        if (
            _git(mirror, "worktree", "add", "--detach", str(base_tree), merge_base)[0]
            != 0
        ):
            raise RuntimeError(
                f"cannot check out {merge_base[:12]} in the scratch clone"
            )
        rc, out = _git(
            mirror,
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            "-z",
            merge_base,
            head,
        )
        cited = {f"contracts/{ticket}.yaml" for ticket in tickets}
        test_paths = 0
        for path in (p for p in out.split("\0") if p):
            if is_test_side(path) or path in cited:
                _git(base_tree, "checkout", head, "--", path)
                test_paths += int(is_test_side(path))
        if test_paths == 0:
            return "control refused: no test-side change"
        test_only = is_test_only_diff(paths, tickets)
        lines: list[str] = []
        for ticket in tickets:
            head_contract = yaml.safe_load(
                (head_tree / "contracts" / f"{ticket}.yaml").read_text(encoding="utf-8")
            )
            base_text = _show(mirror, merge_base, f"contracts/{ticket}.yaml")
            base_contract = yaml.safe_load(base_text) if base_text else None
            base_result = self._run_verifier(
                request,
                base_home,
                base_tree,
                ticket,
                base_tree / "contracts" / f"{ticket}.yaml",
                dod / f"base-{ticket}",
            )
            line = must_fail_control_line(
                base_result,
                head_results.get(ticket),
                carried_ids=carried_evidence_ids(head_contract, base_contract),
                test_only=test_only,
                at_merge_base=True,
            )
            (dod / f"base-{ticket}.control.txt").write_text(
                f"{line}\n", encoding="utf-8"
            )
            lines.append(f"{ticket} control {line}")
        return "head verified; " + "; ".join(lines)

    @staticmethod
    def _contract_home(owner: str, repo_short: str, ticket: str) -> str:
        """The first other canonical clone whose default branch holds the contract."""
        for home in CONTRACT_HOME_REPOSITORIES:
            if home == repo_short:
                continue
            other = canonical_clone(f"{owner}/{home}")
            if other is None:
                continue
            branch = _default_branch(other)
            path = f"refs/remotes/origin/{branch}:contracts/{ticket}.yaml"
            if _git(other, "cat-file", "-e", path)[0] == 0:
                return f"{owner}/{home}"
        return ""

    @staticmethod
    def _run_verifier(
        request: ModelOccReplayRequest,
        home: Path,
        tree: Path,
        ticket: str,
        contract: Path,
        stem: Path,
    ) -> object:
        """The entrypoint the receipt gate's head step and control call."""
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"VIRTUAL_ENV", "ONEX_EVIDENCE_ROOT"}
        }
        env["OMNI_HOME"] = str(home)
        env["DOD_VERIFY_ALLOW_STALE_PRODUCT_CLONE"] = "1"
        result_path = stem.with_suffix(".json")
        try:
            proc = subprocess.run(
                [
                    str(request.verifier_python),
                    "-m",
                    "omnimarket.nodes.node_dod_verify",
                    "--ticket-id",
                    ticket,
                    "--contract-path",
                    str(contract),
                    "--execution-audience",
                    "hosted",
                    "--output-path",
                    str(stem.with_suffix(".receipt.json")),
                ],
                cwd=str(tree),
                env=env,
                capture_output=True,
                text=True,
                timeout=request.verifier_timeout_s,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("verifier for %s at %s failed: %s", ticket, tree, exc)
            return None
        stdout = "\n".join(
            line
            for line in proc.stdout.splitlines()
            if not line.startswith("Receipt written to: ")
        )
        result_path.write_text(stdout + "\n", encoding="utf-8")
        stem.with_suffix(".stderr.txt").write_text(proc.stderr, encoding="utf-8")
        try:
            parsed: object = json.loads(stdout)
        except json.JSONDecodeError:
            return None
        return parsed


__all__ = ["HandlerOccReplay"]
