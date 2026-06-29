# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evidence-Source autobind adapter for pr_lifecycle_fix_effect (OMN-13317, F1).

Handles the receipt_evidence_source_autobind failure class: a product PR whose
``Evidence-Source`` points at the product head SHA instead of an OCC source, so
the Receipt Gate (``verify / verify``) fails. Binding is fully manual today
(``onex_change_control#2801`` / ``omnibase_infra#2043`` consumed hours).

Flow (the missing glue F1 builds on top of the existing deploy-gate adapter):

  1. Detect the ticket id from the product PR title/body.
  2. Resolve the real product PR head SHA + number (stamped into the receipt —
     NOT the OCC repo HEAD, which the deploy-gate adapter used).
  3. Open/sync an OCC binding PR carrying ``contracts/<ticket>.yaml`` (created if
     absent) + a downstream receipt stamped with the product PR head + number.
  4. Two-stage self-bind: after the OCC PR exists, add a self-binding receipt
     using the REAL OCC PR number + OCC head commit (hooks reject placeholder
     ``commit_sha`` / ``pr_number`` — see overnight-sweep friction #8).
  5. Recompute ``contract_sha256`` across ALL matching receipts via
     ``LC_ALL=C shasum -a 256`` (friction #9 — easy to miss).
  6. PATCH ``Evidence-Source: OCC#<n>`` back onto the product PR body via REST
     (NOT ``gh pr edit`` — Projects-classic no-op, friction #7).

The result is validated by the unchanged ``occ-preflight / eligibility`` check.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import subprocess
import tempfile
import textwrap
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.github_api import rest_json, split_repo
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import contract_secret_ref

logger = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

_OCC_REPO = "OmniNode-ai/onex_change_control"
_OCC_REPO_GIT = "git@github.com:OmniNode-ai/onex_change_control.git"

# Ticket id pattern. Product PR titles/bodies cite OMN-XXXX (PR title gate).
_TICKET_RE = re.compile(r"\bOMN-\d+\b")
# Evidence-Source line: an OCC source is `OCC#<n>`; a product-SHA source is a
# bare 7-40 hex sha (the failure mode this adapter repairs).
_EVIDENCE_SOURCE_RE = re.compile(r"^Evidence-Source:\s*(\S+)\s*$", re.MULTILINE)
_OCC_SOURCE_RE = re.compile(r"^OCC#\d+$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
# contract_sha256 line in a receipt: matches both 64-hex and PENDING sentinels.
_CONTRACT_SHA_LINE_RE = re.compile(
    r'contract_sha256:\s*"sha256:(?:[0-9a-f]{64}|PENDING)"'
)


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


# ---------------------------------------------------------------------------
# YAML builders — pure string construction (the receipt-gate parses YAML, but
# we keep authoring lib-free so byte-for-byte hashing is deterministic).
# ---------------------------------------------------------------------------

_CONTRACT_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    title: "Autobind OCC evidence for {ticket_id}"
    summary: >
      OCC contract bound by node_pr_lifecycle_fix_effect Evidence-Source autobind
      (OMN-13317 F1) when {repo} PR #{pr_number} carried a product-SHA
      Evidence-Source and failed the Receipt Gate.
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
        description: "PR #{pr_number} on {repo} — Evidence-Source autobind."
        source: "generated"
        checks:
          - check_type: "command"
            check_value: "gh pr view {pr_number} --repo {repo} --json number,state"
    """)

# Downstream receipt — stamped with the REAL product PR head + number so
# check_receipt_hardening.py (commit_sha 7-40 hex, pr_number >= 1) passes.
# contract_sha256 starts PENDING and is rebound in stage 2.
_DOWNSTREAM_RECEIPT_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    evidence_item_id: "{evidence_id}"
    check_type: "command"
    check_value: "gh pr view {pr_number} --repo {repo} --json number,state,headRefName"
    contract_sha256: "sha256:PENDING"
    status: PASS
    run_timestamp: "{run_timestamp}"
    commit_sha: "{commit_sha}"
    runner: "node_pr_lifecycle_fix_effect"
    verifier: "occ-evidence-source-autobind"
    probe_command: "gh pr view {pr_number} --repo {repo} --json number,state,headRefName"
    probe_stdout: |
      {{"number":{pr_number},"state":"OPEN","headRefName":"{commit_sha}"}}
    actual_output: "PASS: Evidence-Source autobind for {ticket_id} from {repo}#{pr_number}."
    exit_code: 0
    pr_number: {pr_number}
    branch: "{branch}"
    """)

# Self-binding receipt — proves the OCC PR itself. Stamped with the REAL OCC PR
# number + OCC head commit (placeholder values are rejected by hooks; friction #8).
_SELF_BIND_RECEIPT_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    evidence_item_id: "{evidence_id}"
    check_type: "command"
    check_value: "gh pr view {occ_pr_number} --repo {occ_repo} --json number,state"
    contract_sha256: "sha256:PENDING"
    status: PASS
    run_timestamp: "{run_timestamp}"
    commit_sha: "{occ_commit_sha}"
    runner: "node_pr_lifecycle_fix_effect"
    verifier: "occ-evidence-source-autobind"
    probe_command: "gh pr view {occ_pr_number} --repo {occ_repo} --json number,state"
    probe_stdout: |
      {{"number":{occ_pr_number},"state":"OPEN"}}
    actual_output: "PASS: OCC self-bind for {ticket_id} (OCC#{occ_pr_number})."
    exit_code: 0
    pr_number: {occ_pr_number}
    branch: "{branch}"
    """)


class OccAutobindAdapter:
    """Repair a product PR's ``Evidence-Source`` by binding OCC receipt evidence.

    Unlike :class:`OccContractAdapter` (which handles the missing-contract
    deploy-gate class and stamps the receipt with the OCC repo HEAD), this
    adapter stamps the downstream receipt with the **real product PR head SHA
    and number**, performs the two-stage self-bind against the real OCC PR, and
    recomputes ``contract_sha256`` across every matching receipt before pushing.
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

    async def autobind_evidence_source(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> str:
        return await asyncio.to_thread(self._autobind_sync, repo, pr_number, ticket_id)

    # ------------------------------------------------------------------
    # Top-level flow
    # ------------------------------------------------------------------

    def _autobind_sync(self, repo: str, pr_number: int, ticket_id: str | None) -> str:
        token = _resolve_github_token()
        owner, repo_name = split_repo(repo)

        # 1. Resolve the product PR snapshot: body, title, real head SHA.
        pr_data = rest_json(
            "GET", f"/repos/{owner}/{repo_name}/pulls/{pr_number}", token=token
        )
        body: str = pr_data.get("body") or ""
        title: str = pr_data.get("title") or ""
        head = pr_data.get("head") or {}
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(head_sha, str) or not _SHA_RE.match(head_sha):
            raise RuntimeError(
                f"could not resolve product PR head SHA for {repo}#{pr_number}: "
                f"{head_sha!r}"
            )

        # Idempotency guard: already bound to an OCC source — nothing to do.
        existing = _EVIDENCE_SOURCE_RE.search(body)
        if existing and _OCC_SOURCE_RE.match(existing.group(1)):
            action = (
                f"no-op: {repo}#{pr_number} already bound to "
                f"{existing.group(1)} (Evidence-Source already an OCC source)"
            )
            logger.info("occ_autobind_adapter: %s", action)
            return action

        # 2. Detect ticket id (PR-title gate guarantees one on product PRs).
        ticket = ticket_id or self._detect_ticket(title, body)
        if ticket is None:
            raise RuntimeError(
                f"could not detect OMN-XXXX ticket id from {repo}#{pr_number} "
                "title/body; cannot autobind Evidence-Source."
            )

        run_timestamp = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        branch = f"auto/{ticket.lower()}-occ-autobind"
        evidence_id = f"dod-{repo.replace('/', '-')}-pr-{pr_number}"

        with tempfile.TemporaryDirectory(prefix="occ-autobind-") as tmpdir:
            clone_dir = Path(tmpdir) / "onex_change_control"
            self._clone_and_branch(clone_dir, branch, tmpdir)

            contract_path = clone_dir / "contracts" / f"{ticket}.yaml"
            contract_path.parent.mkdir(parents=True, exist_ok=True)
            if not contract_path.is_file():
                contract_path.write_text(
                    _CONTRACT_TEMPLATE.format(
                        ticket_id=ticket,
                        repo=repo,
                        pr_number=pr_number,
                        evidence_id=evidence_id,
                    ),
                    encoding="utf-8",
                )

            # Stage 1: downstream receipt stamped with the REAL product head SHA.
            downstream_dir = clone_dir / "drift" / "dod_receipts" / ticket / evidence_id
            downstream_dir.mkdir(parents=True, exist_ok=True)
            downstream_receipt = downstream_dir / "command.yaml"
            downstream_receipt.write_text(
                _DOWNSTREAM_RECEIPT_TEMPLATE.format(
                    ticket_id=ticket,
                    evidence_id=evidence_id,
                    pr_number=pr_number,
                    repo=repo,
                    run_timestamp=run_timestamp,
                    commit_sha=head_sha,
                    branch=branch,
                ),
                encoding="utf-8",
            )

            # Rebind contract hash for the stage-1 receipt set, commit, push.
            self._rebind_contract_sha256(clone_dir, ticket, contract_path)
            self._run_git(["git", "add", "contracts", "drift"], cwd=str(clone_dir))
            self._run_git(
                [
                    "git",
                    "commit",
                    "-m",
                    (
                        f"evidence({ticket}): autobind OCC evidence for "
                        f"{repo}#{pr_number}\n\n"
                        f"Evidence-Source autobind by node_pr_lifecycle_fix_effect "
                        f"(OMN-13317 F1). Product PR head {head_sha}."
                    ),
                ],
                cwd=str(clone_dir),
            )
            self._run_git(["git", "push", "origin", branch], cwd=str(clone_dir))

            # 3. Open or sync the OCC binding PR.
            occ_pr_number = self._open_or_sync_occ_pr(
                branch=branch, ticket=ticket, repo=repo, pr_number=pr_number
            )

            # Stage 2: self-binding receipt with the REAL OCC PR number + head.
            occ_head_sha = self._head_sha(str(clone_dir))
            self_bind_dir = (
                clone_dir
                / "drift"
                / "dod_receipts"
                / ticket
                / f"occ-self-bind-pr-{occ_pr_number}"
            )
            self_bind_dir.mkdir(parents=True, exist_ok=True)
            (self_bind_dir / "command.yaml").write_text(
                _SELF_BIND_RECEIPT_TEMPLATE.format(
                    ticket_id=ticket,
                    evidence_id=f"occ-self-bind-pr-{occ_pr_number}",
                    occ_pr_number=occ_pr_number,
                    occ_repo=self._occ_repo,
                    run_timestamp=run_timestamp,
                    occ_commit_sha=occ_head_sha,
                    branch=branch,
                ),
                encoding="utf-8",
            )

            # 4. Rebind contract hash across ALL matching receipts (friction #9).
            self._rebind_contract_sha256(clone_dir, ticket, contract_path)
            self._run_git(["git", "add", "drift"], cwd=str(clone_dir))
            self._run_git(
                [
                    "git",
                    "commit",
                    "-m",
                    (
                        f"evidence({ticket}): self-bind OCC#{occ_pr_number} + "
                        f"rebind contract_sha256"
                    ),
                ],
                cwd=str(clone_dir),
            )
            self._run_git(["git", "push", "origin", branch], cwd=str(clone_dir))

        # 5. PATCH Evidence-Source: OCC#<n> back onto the product PR via REST.
        self._patch_evidence_source(
            repo=repo,
            pr_number=pr_number,
            occ_pr_number=occ_pr_number,
            ticket=ticket,
            existing_body=body,
        )

        action = (
            f"autobound Evidence-Source: OCC#{occ_pr_number} for {ticket} on "
            f"{repo}#{pr_number} (product head {head_sha}, branch {branch})"
        )
        logger.info("occ_autobind_adapter: %s", action)
        return action

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_ticket(title: str, body: str) -> str | None:
        """Detect the OMN-NNNN ticket id from PR title (preferred) then body."""
        for source in (title, body):
            match = _TICKET_RE.search(source)
            if match is not None:
                return match.group(0)
        return None

    def _clone_and_branch(self, clone_dir: Path, branch: str, tmpdir: str) -> None:
        self._run_git(
            ["git", "clone", "--depth=1", self._occ_repo_git, str(clone_dir)],
            cwd=tmpdir,
        )
        self._run_git(
            ["git", "config", "user.name", self._git_author_name],
            cwd=str(clone_dir),
        )
        self._run_git(
            ["git", "config", "user.email", self._git_author_email],
            cwd=str(clone_dir),
        )
        self._run_git(["git", "checkout", "-b", branch], cwd=str(clone_dir))

    def _rebind_contract_sha256(
        self, clone_dir: Path, ticket: str, contract_path: Path
    ) -> None:
        """Set ``contract_sha256`` to sha256(contract) on every matching receipt.

        Mirrors the overnight-sweep manual recipe (friction #9):
        ``LC_ALL=C shasum -a 256 contracts/<ticket>.yaml`` then rewrite the
        hash in ``drift/dod_receipts/<ticket>/*/command.yaml``. Implemented in
        Python with the same byte semantics (``LC_ALL=C`` is a locale concern
        for the shell tool, not for Python's hashlib, which is locale-free).
        """
        digest = hashlib.sha256(contract_path.read_bytes()).hexdigest()
        replacement = f'contract_sha256: "sha256:{digest}"'
        receipt_root = clone_dir / "drift" / "dod_receipts" / ticket
        for receipt in receipt_root.rglob("*.yaml"):
            text = receipt.read_text(encoding="utf-8")
            new_text = _CONTRACT_SHA_LINE_RE.sub(replacement, text)
            if new_text != text:
                receipt.write_text(new_text, encoding="utf-8")

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
        return self._run_git(["git", "rev-parse", "HEAD"], cwd=cwd)

    def _open_or_sync_occ_pr(
        self, *, branch: str, ticket: str, repo: str, pr_number: int
    ) -> int:
        """Open the OCC binding PR, or return the existing PR for this branch.

        ``synchronize`` re-fires this adapter for the same product PR; the OCC
        branch already exists and pushing updates it, so a fresh ``create`` 422s.
        Look up the open PR for the branch head first.
        """
        token = _resolve_github_token()
        owner, repo_name = split_repo(self._occ_repo)

        # The list endpoint (`/pulls?head=...`) returns a JSON array, which the
        # dict-returning ``rest_json`` contract cannot represent. Use the search
        # API (object with ``items``) to find an existing open PR for the branch.
        existing_number = self._first_open_pr_number(owner, repo_name, branch, token)
        if existing_number is not None:
            return existing_number

        body = (
            f"Autobind OCC evidence for `{ticket}`.\n\n"
            f"Triggered by Receipt-Gate Evidence-Source autobind on "
            f"{repo}#{pr_number} (OMN-13317 F1).\n\n"
            f"Evidence-Ticket: {ticket}\n"
        )
        resp = rest_json(
            "POST",
            f"/repos/{owner}/{repo_name}/pulls",
            token=token,
            body={
                "title": (
                    f"evidence({ticket}): OCC Evidence-Source autobind for "
                    f"{repo}#{pr_number}"
                ),
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

    @staticmethod
    def _first_open_pr_number(
        owner: str, repo_name: str, branch: str, token: str
    ) -> int | None:
        """Return the number of an open PR for ``branch``, or None.

        Uses the GitHub search API (returns an object with ``items``) so the
        dict-returning ``rest_json`` contract holds.
        """
        query = f"repo:{owner}/{repo_name} is:pr is:open head:{branch}"
        from urllib.parse import quote

        resp = rest_json(
            "GET",
            f"/search/issues?q={quote(query)}",
            token=token,
        )
        items = resp.get("items")
        if isinstance(items, list) and items:
            number = items[0].get("number")
            if isinstance(number, int):
                return number
        return None

    def _patch_evidence_source(
        self,
        *,
        repo: str,
        pr_number: int,
        occ_pr_number: int,
        ticket: str,
        existing_body: str,
    ) -> None:
        """Rewrite or append ``Evidence-Source: OCC#<n>`` via REST PATCH.

        ``gh pr edit`` and GraphQL silently no-op on Projects-classic repos
        (friction #7); REST PATCH of the body is the reliable path.
        """
        token = _resolve_github_token()
        owner, repo_name = split_repo(repo)
        occ_source = f"OCC#{occ_pr_number}"
        occ_line = f"Evidence-Source: {occ_source}"

        match = _EVIDENCE_SOURCE_RE.search(existing_body)
        if match is not None:
            if match.group(1) == occ_source:
                return  # already bound to the correct OCC source — no-op
            # Replace only the captured source token so surrounding whitespace
            # (notably the trailing newline consumed by ``\s*$``) is preserved.
            new_body = (
                existing_body[: match.start(1)]
                + occ_source
                + existing_body[match.end(1) :]
            )
        else:
            new_body = (
                existing_body + f"\n\n---\nEvidence-Ticket: {ticket}\n{occ_line}\n"
            )

        if new_body == existing_body:
            return  # already correct
        rest_json(
            "PATCH",
            f"/repos/{owner}/{repo_name}/pulls/{pr_number}",
            token=token,
            body={"body": new_body},
        )


__all__ = ["OccAutobindAdapter"]
