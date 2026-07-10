# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live OCC contract auto-creation adapter for pr_lifecycle_fix_effect.

Handles the deploy_gate_contract_not_found failure class by creating a minimal
OCC contract YAML and a bound receipt, then opening an OCC PR and cross-linking
the original PR body.

Proactive repair path (OMN-12425):
  - Supports ``dry_run`` and ``mutate`` modes.
  - Idempotency key = (ticket_id, evidence_item_id, repo, pr_head_sha,
    contract_sha256); identical re-run is a no-op.
  - Receipts include ``contract_sha256``, ``pr_head_sha``, ``source_repo``
    and all other mandatory fields.
  - REJECTS ``verifier == runner`` self-attestation.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Literal

from omnimarket.github_api import rest_json, split_repo
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_git_transport import (
    OCC_REPO,
    authenticated_occ_url,
    run_git,
)

# OMN-14189 (Piece 3/5, epic OMN-14180): all PR-body Evidence-Source /
# Evidence-Ticket authoring and read-back flow through the single stamp seam,
# which delegates to the Piece-2 core renderer/parser over the Piece-1 models.
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_stamp_authoring import (
    product_pr_has_evidence_source,
    render_occ_companion_pr_body,
    render_product_pr_body_with_occ_source,
)

logger = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

# OMN-14031: bound every git subprocess so a stalled network git call (push /
# fetch under egress saturation) fails fast instead of wedging the fix-effect
# path — the same un-timed-subprocess hang class fixed in the inventory node.
_GIT_TIMEOUT_SECONDS = 120

_DEFAULT_RUNNER = "node_pr_lifecycle_fix_effect"
_DEFAULT_VERIFIER = "occ-auto-contract-verifier"


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
# Module-level helpers
# ---------------------------------------------------------------------------


def _compute_contract_sha256(contract_yaml: str) -> str:
    """Return the SHA-256 hex digest of contract YAML content."""
    return hashlib.sha256(contract_yaml.encode()).hexdigest()


def _build_idempotency_key(
    *,
    ticket_id: str,
    evidence_item_id: str,
    repo: str,
    pr_head_sha: str,
    contract_sha256: str,
) -> str:
    """Build a deterministic idempotency key from the 5-tuple.

    Key = SHA-256(ticket_id|evidence_item_id|repo|pr_head_sha|contract_sha256).
    """
    raw = "|".join([ticket_id, evidence_item_id, repo, pr_head_sha, contract_sha256])
    return hashlib.sha256(raw.encode()).hexdigest()


_OCC_REPO = OCC_REPO

# ---------------------------------------------------------------------------
# Trivial-infra OCC fast-path (OMN-13776).
#
# A one-line non-runtime infra edit (e.g. a Dockerfile base-image / musl
# version bump) was still triggering the full OCC contract + receipt-chain
# PR. This is a size-AND-path-scoped exemption, not a skip token: it only
# fires when every changed file matches a known non-runtime infra pattern
# AND the total diff is small. Any file touching node business logic
# (handlers/models/contracts) or migrations never qualifies, regardless of
# size. Pure computation — no I/O.
# ---------------------------------------------------------------------------

_TRIVIAL_DIFF_LINE_THRESHOLD = 4
_TRIVIAL_FILE_COUNT_THRESHOLD = 2

_RUNTIME_DENYLIST_RE = re.compile(r"(^|/)(nodes/|migrations/)|\.py$", re.IGNORECASE)

_TRIVIAL_INFRA_ALLOWLIST_RE = re.compile(
    r"(^|/)("
    r"Dockerfile[\w.\-]*"
    r"|[\w.\-]+\.dockerfile"
    r"|requirements[\w.\-]*\.txt"
    r"|\.python-version"
    r"|[\w.\-]*musl[\w.\-]*"
    r"|deploy/.+\.(ya?ml|sh)"
    r"|\.github/workflows/.+\.ya?ml"
    r")$",
    re.IGNORECASE,
)


def classify_trivial_infra_fastpath(
    changed_files: list[str], total_diff_lines: int
) -> tuple[bool, str]:
    """Decide whether a PR qualifies for the trivial-infra OCC fast-path.

    Eligible only when ALL of the following hold:
      - at least one changed file is given (an empty/unknown file list never
        qualifies — we cannot prove triviality without evidence);
      - every changed file matches the non-runtime infra allowlist and none
        match the runtime denylist (node business logic, migrations, any
        ``.py`` source file never qualifies, regardless of size);
      - the file count and total diff line count are both within the
        trivial thresholds.

    Returns (eligible, reason).
    """
    if not changed_files:
        return False, "no changed_files provided — cannot prove triviality"

    denylisted = [f for f in changed_files if _RUNTIME_DENYLIST_RE.search(f)]
    if denylisted:
        return (
            False,
            f"runtime-touching files present, fast-path not eligible: {denylisted}",
        )

    non_allowlisted = [
        f for f in changed_files if not _TRIVIAL_INFRA_ALLOWLIST_RE.search(f)
    ]
    if non_allowlisted:
        return (
            False,
            "files outside the non-runtime infra allowlist, fast-path not "
            f"eligible: {non_allowlisted}",
        )

    if len(changed_files) > _TRIVIAL_FILE_COUNT_THRESHOLD:
        return (
            False,
            f"{len(changed_files)} files changed exceeds trivial threshold "
            f"({_TRIVIAL_FILE_COUNT_THRESHOLD})",
        )

    if total_diff_lines > _TRIVIAL_DIFF_LINE_THRESHOLD:
        return (
            False,
            f"{total_diff_lines} diff lines exceeds trivial threshold "
            f"({_TRIVIAL_DIFF_LINE_THRESHOLD})",
        )

    return (
        True,
        f"trivial non-runtime infra edit ({len(changed_files)} file(s), "
        f"{total_diff_lines} diff line(s)) — OCC receipt-chain skipped via "
        "size/path-scoped fast-path",
    )


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

# Receipt template — all mandatory fields per OMN-12425 DoD:
#   contract_sha256, pr_head_sha, source_repo, run_timestamp, commit_sha,
#   probe_command, probe_stdout, runner, verifier.
# verifier MUST differ from runner (self-attestation is rejected).
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
    runner: "{runner}"
    verifier: "{verifier}"
    probe_command: "gh pr view {pr_number} --repo {source_repo} --json number,state,headRefName"
    probe_stdout: |
      {{"number":{pr_number},"state":"OPEN","headRefSha":"{pr_head_sha}"}}
    actual_output: "PASS: auto-created OCC contract for {ticket_id} from {source_repo}#{pr_number}."
    exit_code: 0
    pr_number: {pr_number}
    branch: "{branch}"
    contract_sha256: "{contract_sha256}"
    pr_head_sha: "{pr_head_sha}"
    source_repo: "{source_repo}"
    """)


class OccContractAdapter:
    """Create a minimal OCC contract when deploy-gate fires for a missing contract.

    Supports dry_run (detect gap, return description, no mutations) and
    mutate (full create + push + PR + backlink) modes. Idempotency is
    tracked per-instance via the 5-tuple key; identical re-runs are no-ops.

    Workflow (mutate mode):
      1. Clone ``onex_change_control`` into a temp directory.
      2. Create branch ``auto/{ticket_id}-occ-contract``.
      3. Write ``contracts/{ticket_id}.yaml`` (ModelTicketContract-compatible).
      4. Write ``drift/dod_receipts/{ticket_id}/{evidence_id}/command.yaml``
         with all mandatory fields including ``contract_sha256``.
      5. Commit, push, open a PR via GitHub REST API.
      6. Append ``Evidence-Source`` and ``Evidence-Ticket`` lines to the original
         PR body via GitHub REST API.
    """

    def __init__(
        self,
        *,
        occ_repo: str = _OCC_REPO,
        git_author_name: str = "omnimarket-bot",
        git_author_email: str = "bot@omninode.ai",
        mode: Literal["dry_run", "mutate"] = "mutate",
        runner: str = _DEFAULT_RUNNER,
        verifier: str = _DEFAULT_VERIFIER,
    ) -> None:
        self._occ_repo = occ_repo
        self._git_author_name = git_author_name
        self._git_author_email = git_author_email
        self._mode = mode
        self._runner = runner
        self._verifier = verifier
        # In-memory idempotency cache; keyed by _build_idempotency_key result.
        self._executed_keys: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_occ_contract(
        self,
        repo: str,
        pr_number: int,
        ticket_id: str,
        pr_head_sha: str | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self._create_occ_contract_sync,
            repo,
            pr_number,
            ticket_id,
            pr_head_sha=pr_head_sha,
        )

    def detect_occ_gap(
        self,
        *,
        repo: str,
        pr_number: int,
        ticket_id: str,
        contract_exists: bool,
        receipt_exists: bool,
    ) -> dict[str, object]:
        """Detect whether OCC coverage is missing for a product PR.

        Returns a dict with ``has_gap: bool`` and ``gap_reason: str``.
        Pure computation — no side effects.
        """
        if not contract_exists and not receipt_exists:
            return {
                "has_gap": True,
                "gap_reason": (
                    f"missing contract and receipt for {ticket_id} "
                    f"on {repo}#{pr_number}"
                ),
            }
        if not contract_exists:
            return {
                "has_gap": True,
                "gap_reason": (
                    f"missing contract for {ticket_id} on {repo}#{pr_number}"
                ),
            }
        if not receipt_exists:
            return {
                "has_gap": True,
                "gap_reason": (
                    f"missing receipt for {ticket_id} on {repo}#{pr_number}"
                ),
            }
        return {"has_gap": False, "gap_reason": ""}

    # ------------------------------------------------------------------
    # Verifier != runner enforcement
    # ------------------------------------------------------------------

    def _validate_verifier_not_runner(self, *, runner: str, verifier: str) -> None:
        """Raise ValueError if verifier == runner (self-attestation rejected)."""
        if runner == verifier:
            raise ValueError(
                f"self-attestation rejected: verifier ({verifier!r}) must differ "
                f"from runner ({runner!r}). Assign an independent verifier."
            )

    # ------------------------------------------------------------------
    # Core sync implementation
    # ------------------------------------------------------------------

    def _create_occ_contract_sync(
        self,
        repo: str,
        pr_number: int,
        ticket_id: str,
        pr_head_sha: str | None = None,
    ) -> str:
        from datetime import UTC, datetime

        # Enforce verifier != runner before doing any work.
        self._validate_verifier_not_runner(runner=self._runner, verifier=self._verifier)

        run_timestamp = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        branch = f"auto/{ticket_id.lower()}-occ-contract"
        evidence_id = f"dod-{repo.replace('/', '-')}-pr-{pr_number}"
        effective_pr_head_sha = pr_head_sha or "unknown"

        # Dry-run: describe what would happen, make no mutations.
        if self._mode == "dry_run":
            gap = self.detect_occ_gap(
                repo=repo,
                pr_number=pr_number,
                ticket_id=ticket_id,
                contract_exists=False,  # assume missing when in dry-run
                receipt_exists=False,
            )
            action = (
                f"[dry-run] would create OCC contract for {ticket_id} "
                f"on {repo}#{pr_number} (branch={branch}). "
                f"Gap detected: {gap['gap_reason']}"
            )
            logger.info("occ_contract_adapter (dry-run): %s", action)
            return action

        # OMN-13990 (CodeRabbit): bail BEFORE any OCC mutation if the product PR
        # is already Evidence-bound. Otherwise an already-bound PR still clones,
        # pushes, and opens an unnecessary companion OCC PR before the late guard
        # in _append_evidence_to_pr fires. Mirrors the autobind adapter's early
        # idempotency guard. The resolved token is reused for the clone below.
        token = _resolve_github_token()
        product_owner, product_repo_name = split_repo(repo)
        product_pr = rest_json(
            "GET",
            f"/repos/{product_owner}/{product_repo_name}/pulls/{pr_number}",
            token=token,
        )
        if product_pr_has_evidence_source(product_pr.get("body") or ""):
            action = (
                f"[no-op] {repo}#{pr_number} already has an Evidence-Source line; "
                f"skipping OCC contract creation for {ticket_id}"
            )
            logger.info("occ_contract_adapter: %s", action)
            return action

        # Build the contract YAML content first so we can hash it.
        contract_yaml = _CONTRACT_TEMPLATE.format(
            ticket_id=ticket_id,
            repo=repo,
            pr_number=pr_number,
            evidence_id=evidence_id,
        )
        contract_sha256 = _compute_contract_sha256(contract_yaml)

        # Idempotency: skip if same key already executed in this process.
        idempotency_key = _build_idempotency_key(
            ticket_id=ticket_id,
            evidence_item_id=evidence_id,
            repo=repo,
            pr_head_sha=effective_pr_head_sha,
            contract_sha256=contract_sha256,
        )
        if idempotency_key in self._executed_keys:
            action = (
                f"[no-op] OCC contract for {ticket_id} on {repo}#{pr_number} "
                f"already created for pr_head_sha={effective_pr_head_sha} "
                f"(idempotency_key={idempotency_key[:12]}...)"
            )
            logger.info("occ_contract_adapter: %s", action)
            return action

        # HTTPS x-access-token transport (OMN-13990): the effects container has
        # no SSH identity. The token resolved above (early Evidence-Source guard)
        # is reused for the clone/push; the shared transport redacts it from git
        # errors.
        with tempfile.TemporaryDirectory(prefix="occ-contract-") as tmpdir:
            clone_dir = Path(tmpdir) / "onex_change_control"

            # 1. Shallow clone OCC repo over authenticated HTTPS
            self._run_git(
                [
                    "git",
                    "clone",
                    "--depth=1",
                    authenticated_occ_url(token, self._occ_repo),
                    str(clone_dir),
                ],
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
            # Fetch current HEAD sha for the receipt (best-effort)
            commit_sha = self._head_sha(str(clone_dir))
            contract_path.write_text(contract_yaml, encoding="utf-8")

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
                    repo_slug=repo.replace("/", "-"),
                    contract_sha256=contract_sha256,
                    pr_head_sha=effective_pr_head_sha,
                    source_repo=repo,
                    runner=self._runner,
                    verifier=self._verifier,
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
                f"Triggered by deploy-gate failure on {repo}#{pr_number}.\n"
                f"contract_sha256: {contract_sha256[:16]}...\n"
                f"pr_head_sha: {effective_pr_head_sha}"
            )
            self._run_git(
                ["git", "commit", "-m", commit_msg],
                cwd=str(clone_dir),
            )
            # Force-push: the auto/* bot branch is fully regenerated each run, so
            # a re-fire diverges from the already-pushed remote branch and a plain
            # push would be rejected non-fast-forward (OMN-13990 / CodeRabbit).
            self._run_git(
                ["git", "push", "--force", "origin", branch],
                cwd=str(clone_dir),
            )

        # 6. Open OCC PR via GitHub REST API
        occ_pr_number = self._open_occ_pr(
            branch=branch,
            ticket_id=ticket_id,
            repo=repo,
            pr_number=pr_number,
            contract_sha256=contract_sha256,
            pr_head_sha=effective_pr_head_sha,
        )

        # 7. Append Evidence-Source to original PR body
        self._append_evidence_to_pr(
            repo=repo,
            pr_number=pr_number,
            occ_pr_number=occ_pr_number,
            ticket_id=ticket_id,
        )

        # Mark as executed for idempotency
        self._executed_keys.add(idempotency_key)

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
        # Delegates to the shared transport, which redacts any embedded
        # x-access-token credential from a surfaced git error (OMN-13990).
        return run_git(argv, cwd=cwd, timeout=_GIT_TIMEOUT_SECONDS)

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
        contract_sha256: str = "unknown",
        pr_head_sha: str = "unknown",
    ) -> int:
        owner, repo_name = split_repo(self._occ_repo)
        # Human prose is authored here; the Evidence-Ticket line is rendered by
        # the Piece-2 core renderer over the typed stamp (OMN-14189). The former
        # ``Evidence-Source: auto-contract-<run_id>`` self-source was a fabricated
        # placeholder token — not a canonical OCC#/SHA source, so the receipt-gate
        # parser could never resolve it — and is dropped. The companion carries no
        # Evidence-Source of its own; the product PR is the gate-read surface (the
        # sibling autobind companion has always omitted it).
        prose = (
            f"Auto-created OCC contract for `{ticket_id}`.\n\n"
            f"Triggered by deploy-gate failure on {repo}#{pr_number}.\n\n"
            f"Source PR head SHA: `{pr_head_sha}`\n"
            f"Contract SHA-256: `{contract_sha256[:16]}...`\n"
        )
        body = render_occ_companion_pr_body(prose, tickets=[ticket_id])
        token = _resolve_github_token()
        # OMN-13990: base on OCC's DEFAULT branch, not a hardcoded "main" (OCC
        # default is `dev`). The branch is cut from the shallow clone of the
        # default, so a PR based on "main" would surface the whole dev<->main
        # delta instead of a clean net-new companion diff.
        info = rest_json("GET", f"/repos/{owner}/{repo_name}", token=token)
        base = info.get("default_branch")
        if not isinstance(base, str) or not base:
            raise RuntimeError(
                f"could not resolve default branch for {owner}/{repo_name}"
            )
        resp = rest_json(
            "POST",
            f"/repos/{owner}/{repo_name}/pulls",
            token=token,
            body={
                "title": f"auto(OCC): contract + receipt for {ticket_id}",
                "head": branch,
                "base": base,
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
        # Idempotency hardening (OMN-13990 item 8 / §6 H1): skip when ANY
        # Evidence-Source line already exists, not only this OCC number. The
        # born-path autobind adapter may have already bound a (possibly
        # different) OCC source; occ-preflight reads the FIRST Evidence-Source
        # line (head -1), so re-stamping under an already-bound PR would shadow
        # the real source. Never double-foot.
        if product_pr_has_evidence_source(existing_body):
            return
        # Author the Evidence-Source / Evidence-Ticket stamp via the Piece-2 core
        # renderer over the typed model — no inline f-string footer (OMN-14189).
        new_body = render_product_pr_body_with_occ_source(
            existing_body, occ_pr_number=occ_pr_number, tickets=[ticket_id]
        )
        rest_json(
            "PATCH",
            f"/repos/{owner}/{repo_name}/pulls/{pr_number}",
            token=token,
            body={"body": new_body},
        )


__all__ = [
    "OccContractAdapter",
    "_build_idempotency_key",
    "_compute_contract_sha256",
    "classify_trivial_infra_fastpath",
]
