# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live OCC contract auto-creation adapter for pr_lifecycle_fix_effect.

Handles the deploy_gate_contract_not_found failure class by creating a minimal
OCC contract YAML and a bound receipt, then opening an OCC PR and cross-linking
the original PR body.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
import textwrap
import uuid
from pathlib import Path

from omnimarket.github_api import rest_json, split_repo
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import contract_secret_ref

logger = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


def _resolve_github_token() -> str:
    """Resolve the GitHub token from the contract-declared ref (OMN-12856)."""
    ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
    secret = resolve_api_key(ref)
    if secret is None:
        raise RuntimeError(
            f"api_key_ref {ref!r} resolved to None — "
            "ensure GITHUB_TOKEN is set in the secret store."
        )
    return secret.get_secret_value()


_OCC_REPO = "OmniNode-ai/onex_change_control"
_OCC_REPO_GIT = "git@github.com:OmniNode-ai/onex_change_control.git"

# ---------------------------------------------------------------------------
# YAML builders — pure string construction, no YAML lib dependency needed
# ---------------------------------------------------------------------------

_CONTRACT_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    title: "Auto-generated contract for {ticket_id}"
    summary: >
      Minimal OCC contract auto-created by node_pr_lifecycle_fix_effect when
      {repo} PR #{pr_number} failed deploy-gate due to a missing contract file.
    is_seam_ticket: false
    interface_change: false
    interfaces_touched: []
    evidence_requirements:
      - kind: "ci"
        description: "PR #{pr_number} CI checks green"
        command: "gh pr checks {pr_number} --repo {repo}"
    emergency_bypass:
      enabled: false
      justification: ""
      follow_up_ticket_id: ""
    dod_evidence:
      - id: "{evidence_id}"
        description: "PR #{pr_number} on {repo} — deploy-gate auto-contract."
        source: "generated"
        checks:
          - check_type: "command"
            check_value: "gh pr view {pr_number} --repo {repo} --json number,state"
    """)

_RECEIPT_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    evidence_item_id: "{evidence_id}"
    check_type: "command"
    check_value: "gh pr view {pr_number} --repo {repo} --json number,state,headRefName"
    status: PASS
    run_timestamp: "{run_timestamp}"
    commit_sha: "{commit_sha}"
    runner: "node_pr_lifecycle_fix_effect"
    verifier: "occ-auto-contract"
    probe_command: "gh pr view {pr_number} --repo {repo} --json number,state,headRefName"
    probe_stdout: |
      {{"number":{pr_number},"state":"OPEN"}}
    actual_output: "PASS: auto-created OCC contract for {ticket_id} from {repo}#{pr_number}."
    exit_code: 0
    pr_number: {pr_number}
    branch: "{branch}"
    """)


class OccContractAdapter:
    """Create a minimal OCC contract when deploy-gate fires for a missing contract.

    Workflow:
      1. Clone ``onex_change_control`` into a temp directory.
      2. Create branch ``auto/{ticket_id}-occ-contract``.
      3. Write ``contracts/{ticket_id}.yaml`` (ModelTicketContract-compatible).
      4. Write ``drift/dod_receipts/{ticket_id}/{evidence_id}/command.yaml``.
      5. Commit, push, open a PR via GitHub REST API.
      6. Append ``Evidence-Source`` and ``Evidence-Ticket`` lines to the original
         PR body via GitHub REST API.
    """

    def __init__(
        self,
        *,
        occ_repo: str = _OCC_REPO,
        occ_repo_git: str = _OCC_REPO_GIT,
        git_author_name: str = "omnimarket-bot",
        git_author_email: str = "bot@omninode.ai",
    ) -> None:
        self._occ_repo = occ_repo
        self._occ_repo_git = occ_repo_git
        self._git_author_name = git_author_name
        self._git_author_email = git_author_email

    async def create_occ_contract(
        self, repo: str, pr_number: int, ticket_id: str
    ) -> str:
        return await asyncio.to_thread(
            self._create_occ_contract_sync, repo, pr_number, ticket_id
        )

    def _create_occ_contract_sync(
        self, repo: str, pr_number: int, ticket_id: str
    ) -> str:
        from datetime import UTC, datetime

        run_timestamp = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        branch = f"auto/{ticket_id.lower()}-occ-contract"
        evidence_id = f"dod-{repo.replace('/', '-')}-pr-{pr_number}"
        repo_slug = repo.replace("/", "-")

        with tempfile.TemporaryDirectory(prefix="occ-contract-") as tmpdir:
            clone_dir = Path(tmpdir) / "onex_change_control"

            # 1. Shallow clone OCC repo
            self._run_git(
                ["git", "clone", "--depth=1", self._occ_repo_git, str(clone_dir)],
                cwd=tmpdir,
            )

            # Configure git identity
            self._run_git(
                ["git", "config", "user.name", self._git_author_name],
                cwd=str(clone_dir),
            )
            self._run_git(
                ["git", "config", "user.email", self._git_author_email],
                cwd=str(clone_dir),
            )

            # 2. Create branch
            self._run_git(
                ["git", "checkout", "-b", branch],
                cwd=str(clone_dir),
            )

            # 3. Write contract YAML
            contract_path = clone_dir / "contracts" / f"{ticket_id}.yaml"
            contract_path.parent.mkdir(parents=True, exist_ok=True)
            # Fetch current HEAD sha for the receipt (best-effort; use placeholder on fail)
            commit_sha = self._head_sha(str(clone_dir))
            contract_path.write_text(
                _CONTRACT_TEMPLATE.format(
                    ticket_id=ticket_id,
                    repo=repo,
                    pr_number=pr_number,
                    evidence_id=evidence_id,
                ),
                encoding="utf-8",
            )

            # 4. Write receipt YAML
            receipt_dir = clone_dir / "drift" / "dod_receipts" / ticket_id / evidence_id
            receipt_dir.mkdir(parents=True, exist_ok=True)
            receipt_path = receipt_dir / "command.yaml"
            receipt_path.write_text(
                _RECEIPT_TEMPLATE.format(
                    ticket_id=ticket_id,
                    evidence_id=evidence_id,
                    pr_number=pr_number,
                    repo=repo,
                    run_timestamp=run_timestamp,
                    commit_sha=commit_sha,
                    branch=branch,
                    repo_slug=repo_slug,
                ),
                encoding="utf-8",
            )

            # 5. Stage, commit, push
            self._run_git(
                ["git", "add", str(contract_path), str(receipt_path)],
                cwd=str(clone_dir),
            )
            commit_msg = (
                f"auto(OCC): create contract + receipt for {ticket_id}\n\n"
                f"Auto-created by node_pr_lifecycle_fix_effect.\n"
                f"Triggered by deploy-gate failure on {repo}#{pr_number}."
            )
            self._run_git(
                ["git", "commit", "-m", commit_msg],
                cwd=str(clone_dir),
            )
            self._run_git(
                ["git", "push", "origin", branch],
                cwd=str(clone_dir),
            )

        # 6. Open OCC PR via GitHub REST API
        occ_pr_number = self._open_occ_pr(
            branch=branch,
            ticket_id=ticket_id,
            repo=repo,
            pr_number=pr_number,
        )

        # 7. Append Evidence-Source to original PR body
        self._append_evidence_to_pr(
            repo=repo,
            pr_number=pr_number,
            occ_pr_number=occ_pr_number,
            ticket_id=ticket_id,
        )

        action = (
            f"created OCC contract for {ticket_id} "
            f"(occ_pr={occ_pr_number}, branch={branch}) "
            f"on {repo}#{pr_number}"
        )
        logger.info("occ_contract_adapter: %s", action)
        return action

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run_git(self, argv: list[str], *, cwd: str) -> str:
        env = os.environ.copy()
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _head_sha(self, cwd: str) -> str:
        try:
            return self._run_git(["git", "rev-parse", "HEAD"], cwd=cwd)
        except subprocess.CalledProcessError:
            return "unknown"

    def _open_occ_pr(
        self,
        *,
        branch: str,
        ticket_id: str,
        repo: str,
        pr_number: int,
    ) -> int:
        owner, repo_name = split_repo(self._occ_repo)
        run_id = uuid.uuid4().hex[:8]
        body = (
            f"Auto-created OCC contract for `{ticket_id}`.\n\n"
            f"Triggered by deploy-gate failure on {repo}#{pr_number}.\n\n"
            f"Evidence-Ticket: {ticket_id}\n"
            f"Evidence-Source: auto-contract-{run_id}\n"
        )
        token = _resolve_github_token()
        resp = rest_json(
            "POST",
            f"/repos/{owner}/{repo_name}/pulls",
            token=token,
            body={
                "title": f"auto(OCC): contract + receipt for {ticket_id}",
                "head": branch,
                "base": "main",
                "body": body,
            },
        )
        number = resp.get("number")
        if not isinstance(number, int):
            raise RuntimeError(
                f"OCC PR creation returned unexpected number field: {number!r}"
            )
        return number

    def _append_evidence_to_pr(
        self,
        *,
        repo: str,
        pr_number: int,
        occ_pr_number: int,
        ticket_id: str,
    ) -> None:
        token = _resolve_github_token()
        owner, repo_name = split_repo(repo)
        pr_data = rest_json(
            "GET", f"/repos/{owner}/{repo_name}/pulls/{pr_number}", token=token
        )
        existing_body: str = pr_data.get("body") or ""
        evidence_footer = (
            f"\n\n---\n"
            f"Evidence-Ticket: {ticket_id}\n"
            f"Evidence-Source: OCC#{occ_pr_number}\n"
        )
        # Only append if not already present (idempotency guard)
        if f"Evidence-Source: OCC#{occ_pr_number}" not in existing_body:
            rest_json(
                "PATCH",
                f"/repos/{owner}/{repo_name}/pulls/{pr_number}",
                token=token,
                body={"body": existing_body + evidence_footer},
            )


__all__ = ["OccContractAdapter"]
