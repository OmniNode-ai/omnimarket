# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerDurableEvidenceGateEffect — runs the DurableEvidenceGate on the node's path.

OMN-20886: ``DurableEvidenceGate`` was constructed only in tests, so the
receipt_tracked, contract_cites_merge_commit and contract_on_occ_main checks the
dod_verify skill advertises never ran when an operator invoked it. This EFFECT
handler owns the gate's production probes and its two non-GitHub inputs (the
ticket's Linear description and labels) and evaluates the gate for one ticket.

Every probe reads the canonical clones and the PR watcher's state first,
through the node's existing reader (:class:`HandlerDodEvidenceGithubEffect` and
its local source); ``gh`` answers only what they do not hold. The probes keep
the gate's fail-closed contract: an unreadable fact is ``None``, an empty tuple
or ``False``, never an affirmative answer.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Final

import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.config.service_endpoints import LINEAR_GRAPHQL_URL
from omnimarket.nodes.node_dod_verify.handlers.dod_evidence_local_source import (
    canonical_clone,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_evidence_github_effect import (
    HandlerDodEvidenceGithubEffect,
    pypi_release_files,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_evidence_github_lookup import (
    EnumDodEvidenceGithubOperation,
    ModelDodEvidenceGithubLookupCommand,
    ModelDodEvidenceGithubLookupResultEvent,
)
from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    ModelDurableEvidenceGateRun,
    ModelDurableEvidenceGateRunCommand,
    ModelRepoContractRead,
    ModelRepoEvidenceCheckRun,
)
from omnimarket.nodes.node_dod_verify.services.durable_evidence_gate import (
    DurableEvidenceGate,
)

logger = logging.getLogger(__name__)

LINEAR_API_KEY_ENV: Final[str] = "LINEAR_API_KEY"
_GIT_TIMEOUT_S: Final[int] = 60
_GH_TIMEOUT_S: Final[int] = 30
_LINEAR_TIMEOUT_S: Final[int] = 30
_TICKET_QUERY: Final[str] = (
    "query($id: String!) { issue(id: $id) { description labels { nodes { name } } } }"
)


def _git(repo_path: str, *args: str) -> tuple[int, str]:
    """One read-only git command in ``repo_path``; ``(rc, stdout)``. Never raises."""
    if not repo_path:
        return 1, ""
    try:
        proc = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
            env=scrub_git_location_env(os.environ),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("git %s in %s failed: %s", args[:2], repo_path, exc)
        return 1, ""
    return proc.returncode, proc.stdout


def _yaml_mapping(text: str) -> dict[str, object] | None:
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(key): value for key, value in parsed.items()}


class HandlerDurableEvidenceGateEffect:
    """EFFECT: evaluate the DurableEvidenceGate for one ticket with live probes."""

    def __init__(self, github: HandlerDodEvidenceGithubEffect | None = None) -> None:
        self._github = (
            github if github is not None else HandlerDodEvidenceGithubEffect()
        )
        self._local = self._github.local_source

    def handle(
        self, command: ModelDurableEvidenceGateRunCommand
    ) -> ModelDurableEvidenceGateRun:
        description, labels, ticket_detail = self.read_ticket(command.ticket_id)
        gate = DurableEvidenceGate(
            is_receipt_tracked=self.is_receipt_tracked,
            gh_pr_view=self.gh_pr_view,
            pr_commits=self.pr_commits,
            load_contract_on_ref=self.load_contract_on_ref,
            load_receipts_on_ref=self.load_receipts_on_ref,
            release_tags_containing=self.release_tags_containing,
            index_release_files=pypi_release_files,
            occ_repo_path=command.occ_repo_path,
            occ_governance_ref=command.occ_governance_ref,
            read_repo_contract=self.read_repo_contract,
            read_repo_check_runs=self.read_repo_check_runs,
        )
        result = gate.evaluate_default(
            ticket_id=command.ticket_id,
            contract=command.contract,
            ticket_labels=labels,
            merged_prs=command.merged_prs,
            ticket_description=description,
        )
        return ModelDurableEvidenceGateRun(
            result=result, ticket_read_detail=ticket_detail
        )

    # ------------------------------------------------------------ Linear

    @staticmethod
    def read_ticket(ticket_id: str) -> tuple[str, frozenset[str], str]:
        """The ticket's description and label names, and why when unreadable.

        Read with ``$LINEAR_API_KEY`` (the lab hosts' Linear credential). An
        unreadable ticket yields an empty description and no labels, which the
        gate refuses on every path that needs them.
        """
        api_key = os.environ.get(LINEAR_API_KEY_ENV, "")
        if not api_key:
            return "", frozenset(), f"{LINEAR_API_KEY_ENV} is not set"
        request = urllib.request.Request(
            LINEAR_GRAPHQL_URL,
            data=json.dumps(
                {"query": _TICKET_QUERY, "variables": {"id": ticket_id}}
            ).encode("utf-8"),
            headers={"Authorization": api_key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=_LINEAR_TIMEOUT_S) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return "", frozenset(), f"Linear read failed: {type(exc).__name__}"
        data = payload.get("data") if isinstance(payload, dict) else None
        issue = data.get("issue") if isinstance(data, dict) else None
        if not isinstance(issue, dict):
            return "", frozenset(), f"Linear returned no issue {ticket_id}"
        labels_node = issue.get("labels")
        nodes = labels_node.get("nodes") if isinstance(labels_node, dict) else None
        labels = frozenset(
            str(node["name"])
            for node in nodes or ()
            if isinstance(node, dict) and isinstance(node.get("name"), str)
        )
        return str(issue.get("description") or ""), labels, ""

    # ------------------------------------------------------------ OCC probes

    @staticmethod
    def is_receipt_tracked(repo_path: str, ref: str, receipt_dir: str) -> bool:
        rc, out = _git(
            repo_path, "ls-tree", "-r", "--name-only", ref, "--", receipt_dir
        )
        return rc == 0 and bool(out.strip())

    @staticmethod
    def load_contract_on_ref(
        repo_path: str, ref: str, rel_path: str
    ) -> dict[str, object] | None:
        rc, out = _git(repo_path, "show", f"{ref}:{rel_path}")
        return _yaml_mapping(out) if rc == 0 else None

    @staticmethod
    def load_receipts_on_ref(
        repo_path: str, ref: str, receipt_dir: str
    ) -> list[dict[str, object]]:
        rc, out = _git(
            repo_path, "ls-tree", "-r", "--name-only", ref, "--", receipt_dir
        )
        if rc != 0:
            return []
        payloads: list[dict[str, object]] = []
        for name in sorted(out.splitlines()):
            if not name.endswith(".yaml"):
                continue
            rc, text = _git(repo_path, "show", f"{ref}:{name}")
            payload = _yaml_mapping(text) if rc == 0 else None
            if payload is None:
                continue
            payload.setdefault("__source_name__", Path(name).name)
            payloads.append(payload)
        return payloads

    # ------------------------------------------------------------ PR probes

    def _lookup(
        self, command: ModelDodEvidenceGithubLookupCommand
    ) -> ModelDodEvidenceGithubLookupResultEvent:
        output = self._github.handle(command)
        event = output.events[0]
        assert isinstance(event, ModelDodEvidenceGithubLookupResultEvent)
        return event

    def gh_pr_view(self, repo: str, pr_number: int) -> tuple[str, str | None]:
        view: object = self._local.pr_view(repo, pr_number)
        merge = view.get("mergeCommit") if isinstance(view, dict) else None
        if not isinstance(view, dict) or (
            str(view.get("state") or "").upper() == "MERGED"
            and not (isinstance(merge, dict) and merge.get("oid"))
        ):
            try:
                proc = subprocess.run(
                    [
                        "gh",
                        "pr",
                        "view",
                        str(pr_number),
                        "--repo",
                        repo,
                        "--json",
                        "state,mergeCommit",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=_GH_TIMEOUT_S,
                    check=False,
                )
                view = json.loads(proc.stdout) if proc.returncode == 0 else None
            except (subprocess.TimeoutExpired, OSError, ValueError):
                view = None
        if not isinstance(view, dict):
            return "UNKNOWN", None
        merge = view.get("mergeCommit")
        oid = str(merge.get("oid") or "") if isinstance(merge, dict) else ""
        return str(view.get("state") or "UNKNOWN").upper(), oid or None

    @staticmethod
    def pr_commits(repo: str, pr_number: int) -> tuple[str, ...]:
        try:
            proc = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    repo,
                    "--json",
                    "commits",
                    "--jq",
                    ".commits[].oid",
                ],
                capture_output=True,
                text=True,
                timeout=_GH_TIMEOUT_S,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return ()
        if proc.returncode != 0:
            return ()
        return tuple(line.strip() for line in proc.stdout.splitlines() if line.strip())

    @staticmethod
    def release_tags_containing(repo: str, commit_sha: str) -> tuple[str, ...] | None:
        clone = canonical_clone(repo)
        if clone is None or not commit_sha:
            return None
        rc, _ = _git(str(clone), "cat-file", "-e", f"{commit_sha}^{{commit}}")
        if rc != 0:
            return None
        rc, out = _git(str(clone), "tag", "--list", "v*", "--contains", commit_sha)
        if rc != 0:
            return None
        return tuple(sorted(line.strip() for line in out.splitlines() if line.strip()))

    # ------------------------------------------------------------ repo readers

    def read_repo_contract(
        self, repo: str, ref: str, ticket_id: str
    ) -> ModelRepoContractRead:
        event = self._lookup(
            ModelDodEvidenceGithubLookupCommand(
                operation=EnumDodEvidenceGithubOperation.READ_REPO_CONTRACT,
                repo=repo,
                commit_sha=ref,
                ticket_id=ticket_id,
            )
        )
        assert event.repo_contract is not None
        return event.repo_contract

    def read_repo_check_runs(
        self, repo: str, sha: str
    ) -> tuple[ModelRepoEvidenceCheckRun, ...] | None:
        event = self._lookup(
            ModelDodEvidenceGithubLookupCommand(
                operation=EnumDodEvidenceGithubOperation.READ_REPO_EVIDENCE_CHECK_RUNS,
                repo=repo,
                commit_sha=sha,
            )
        )
        return event.check_runs if event.resolved else None


__all__ = ["LINEAR_API_KEY_ENV", "HandlerDurableEvidenceGateEffect"]
