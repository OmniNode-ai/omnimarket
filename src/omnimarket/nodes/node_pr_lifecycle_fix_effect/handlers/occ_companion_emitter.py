# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The single canonical OCC companion producer (OMN-14285, S1 converge).

This is the **one** OCC-companion writer surface for the node. It replaces the
two ~80%-overlapping ``*Adapter`` classes that coexisted before S1:

  * ``OccAutobindAdapter`` (``receipt_evidence_source_autobind``: a product PR
    whose ``Evidence-Source`` pointed at its own head SHA instead of an OCC
    source), and
  * ``OccContractAdapter`` (``deploy_gate_contract_not_found``: a product PR with
    no OCC contract at all),

plus the bespoke ``omniclaude/scripts/scaffold_occ_receipt.py`` manual writer
(retired under the same ticket). All three authored load-bearing OCC evidence;
two of them carried divergent receipt shapes and the pre-OMN-14255 head-SHA bug.

Both failure classes are now ONE code path (:meth:`_emit_companion_sync`): the
autobind flow already authored the contract-if-absent, so the deploy-gate case is
a caller of the same core. Every committed byte — contract YAML, downstream
receipt, self-bind receipt, ``contract_sha256``/``contract_entry_sha256``
binding — is rendered by the pure :mod:`occ_evidence_stamp` seam (the COMPUTE
half); this class owns only the git/gh side effects (the EFFECT half). PR-body
``Evidence-Source`` / ``Evidence-Ticket`` authoring flows through the
:mod:`occ_stamp_authoring` seam (Piece 3, OMN-14189).

Hardened behaviors preserved from the autobind path: the real ``gh pr view``
probe (machine-observed, not fabricated — OMN-13990 item 4 / OMN-14055),
whole-file ``contract_sha256`` rebind across every matching receipt (friction #9;
now paired with a per-receipt ``contract_entry_sha256`` rebind, OMN-13888 /
OMN-14418 residual 3, so receipts survive a later append to the contract instead
of going stale), the two-stage OCC self-bind, open-**or-sync** idempotency (no
422 on ``synchronize`` re-fire), and force-push all-adds regeneration
(append-only gate stays green). Capabilities folded in from the deploy-gate
path: ``dry_run`` mode, the configurable ``verifier != runner`` self-attestation
guard (OMN-12791), and ``detect_occ_gap``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml

# OMN-13990 (D3, validator-parity ticket extraction): the emitter uses the SAME
# ticket-extraction the occ-preflight eligibility validator uses so the two can
# never cite a different ticket set. This is the gate's own private helper —
# importing it (rather than re-deriving the regex) binds the emitter to the gate
# exactly. omnibase-core is a declared omnimarket dependency.
#
# OMN-14418 residual 3: compute_contract_entry_sha256 is the SAME canonical
# per-entry hasher the consumer-side gates (check_receipt_contract_binding,
# check_receipt_hardening.py) recompute against — imported, never
# re-implemented, so the producer and the gates can never diverge on the hash.
from omnibase_core.validation.validator_receipt_gate import (
    ContractEntryNotFoundError,
    _extract_ticket_ids,
    compute_contract_entry_sha256,
)

from omnimarket.github_api import rest_json, split_repo
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    SHA_RE,
    compute_contract_sha256,
    extract_evidence_item_id,
    rebind_contract_entry_sha256_in_text,
    rebind_contract_sha256_in_text,
    render_companion_contract,
    render_downstream_receipt,
    render_self_bind_receipt,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_git_transport import (
    OCC_REPO,
    authenticated_occ_url,
    run_git,
)

# OMN-14189 (Piece 3/5, epic OMN-14180): all PR-body Evidence-Source /
# Evidence-Ticket authoring and read-back flow through the single stamp seam,
# which delegates to the Piece-2 core renderer/parser over the Piece-1 models.
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_stamp_authoring import (
    product_pr_occ_binding,
    render_occ_companion_pr_body,
    render_product_pr_body_with_occ_source,
)

logger = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

# OMN-14031: bound every git subprocess so a stalled network git call (push /
# fetch under egress saturation) fails fast instead of wedging the fix-effect
# path — the same un-timed-subprocess hang class fixed in the inventory node.
_GIT_TIMEOUT_SECONDS = 120

_OCC_REPO = OCC_REPO

_DEFAULT_RUNNER = "node_pr_lifecycle_fix_effect"
_DEFAULT_VERIFIER = "occ-evidence-source-autobind"


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


class OccCompanionEmitter:
    """Author the OCC companion (contract + receipts) + rebind a product PR.

    A single producer for both OCC failure classes. The public methods
    (:meth:`autobind_evidence_source`, :meth:`create_occ_contract`) are thin
    entry points over the shared :meth:`_emit_companion_sync` core, so there is
    exactly one clone/branch/commit/push/PR/patch flow and one receipt vocabulary.
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
        # verifier == runner is rejected at construction so a mis-wired producer
        # fails fast rather than authoring self-attesting receipts (OMN-12791).
        self._validate_verifier_not_runner(runner=runner, verifier=verifier)

    # ------------------------------------------------------------------
    # Public API — both former adapter surfaces, one core
    # ------------------------------------------------------------------

    async def autobind_evidence_source(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> str:
        """Bind OCC receipt evidence for a PR and rewrite its Evidence-Source.

        The ``receipt_evidence_source_autobind`` failure class: the product PR's
        ``Evidence-Source`` points at its own head SHA (or is absent).
        """
        return await asyncio.to_thread(
            self._emit_companion_sync, repo, pr_number, ticket_id
        )

    async def create_occ_contract(
        self,
        repo: str,
        pr_number: int,
        ticket_id: str,
        pr_head_sha: str | None = None,
    ) -> str:
        """Author the OCC companion when deploy-gate fires for a missing contract.

        The ``deploy_gate_contract_not_found`` failure class. ``pr_head_sha`` is
        accepted for call-compatibility but the authoritative head SHA is always
        re-observed live from GitHub inside the core (never a caller-supplied
        value), so a stale hint can never be stamped into a receipt.
        """
        return await asyncio.to_thread(
            self._emit_companion_sync, repo, pr_number, ticket_id
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
        """Detect whether OCC coverage is missing for a product PR (pure)."""
        if not contract_exists and not receipt_exists:
            reason = (
                f"missing contract and receipt for {ticket_id} on {repo}#{pr_number}"
            )
        elif not contract_exists:
            reason = f"missing contract for {ticket_id} on {repo}#{pr_number}"
        elif not receipt_exists:
            reason = f"missing receipt for {ticket_id} on {repo}#{pr_number}"
        else:
            return {"has_gap": False, "gap_reason": ""}
        return {"has_gap": True, "gap_reason": reason}

    # ------------------------------------------------------------------
    # verifier != runner enforcement (OMN-12791)
    # ------------------------------------------------------------------

    def _validate_verifier_not_runner(self, *, runner: str, verifier: str) -> None:
        """Raise ValueError if verifier == runner (self-attestation rejected)."""
        if runner == verifier:
            raise ValueError(
                f"self-attestation rejected: verifier ({verifier!r}) must differ "
                f"from runner ({runner!r}). Assign an independent verifier."
            )

    # ------------------------------------------------------------------
    # The one core flow
    # ------------------------------------------------------------------

    def _emit_companion_sync(
        self, repo: str, pr_number: int, ticket_id: str | None
    ) -> str:
        # Dry-run: describe intent and make ZERO side effects (no token, no I/O).
        # This is a planning affordance (detect_occ_gap companion), not a
        # production path — the runtime/orchestrator always wire mutate mode.
        if self._mode == "dry_run":
            who = ticket_id or "auto-detected ticket(s)"
            action = (
                f"[dry-run] would author OCC companion for {who} on "
                f"{repo}#{pr_number} (no side effects performed)"
            )
            logger.info("occ_companion_emitter (dry-run): %s", action)
            return action

        token = _resolve_github_token()
        owner, repo_name = split_repo(repo)

        # 1. Resolve the product PR snapshot: body, title, real head SHA + state.
        pr_data = rest_json(
            "GET", f"/repos/{owner}/{repo_name}/pulls/{pr_number}", token=token
        )
        body: str = pr_data.get("body") or ""
        title: str = pr_data.get("title") or ""
        head = pr_data.get("head") or {}
        head_sha = head.get("sha") if isinstance(head, dict) else None
        head_ref = head.get("ref") if isinstance(head, dict) else None
        pr_state = pr_data.get("state") or "open"
        if not isinstance(head_sha, str) or not SHA_RE.match(head_sha):
            raise RuntimeError(
                f"could not resolve product PR head SHA for {repo}#{pr_number}: "
                f"{head_sha!r}"
            )

        # OMN-14255: the receipt must cite the actual squash ``mergeCommit.oid``
        # once the PR has landed — NOT the pre-merge ``headRefOid``. On these
        # squash-merge-only repos the merge commit is a brand-new SHA with no
        # ancestry to the head, so a head-SHA citation cannot satisfy the
        # DurableEvidenceGate CONTRACT_CITES_MERGE_COMMIT identity leg. When this
        # adapter runs pre-merge (its usual Evidence-Source repair timing) the
        # merge commit does not exist yet — GitHub's ``merge_commit_sha`` on an
        # OPEN PR is a throwaway test-merge SHA, so it MUST be ignored unless the
        # PR is actually merged. In that pre-merge case we fall back to the head
        # SHA; the gate's PR-commits membership leg (OMN-14255) still accepts it.
        receipt_commit_sha = self._receipt_commit_sha(pr_data, head_sha)

        # Idempotency guard: already bound to an OCC source — nothing to do.
        # Read the canonical stamp via the Piece-2 parser, not a local regex.
        already_bound = product_pr_occ_binding(body)
        if already_bound is not None:
            action = (
                f"no-op: {repo}#{pr_number} already bound to "
                f"OCC#{already_bound} (Evidence-Source already an OCC source)"
            )
            logger.info("occ_companion_emitter: %s", action)
            return action

        # 2. Validator-parity ticket extraction (OMN-13990 D3): the gate owns
        #    tickets via closing-keyword-exclusive-else-all-title-tokens; author a
        #    companion for EVERY cited ticket. Fall back to the caller-supplied
        #    ticket only when the PR cites none.
        tickets = self._extract_tickets(title, body)
        if not tickets and ticket_id:
            tickets = [ticket_id]
        if not tickets:
            raise RuntimeError(
                f"could not detect any OMN-XXXX ticket id from {repo}#{pr_number} "
                "title/body; cannot author an OCC companion."
            )

        repo_slug = repo.replace("/", "-")
        # One OCC branch/PR per product PR (ticket-count agnostic) so a single
        # Evidence-Source: OCC#<n> covers every cited ticket and re-fires sync.
        branch = f"auto/{repo_slug.lower()}-pr-{pr_number}-occ-autobind"
        evidence_id = f"dod-{repo_slug}-pr-{pr_number}"

        run_timestamp = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Genuine product-PR probe, observed once and shared across tickets.
        downstream_probe_command = (
            f"gh pr view {pr_number} --repo {repo} --json number,state,headRefName"
        )
        downstream_stdout, downstream_exit = self._observe_pr_probe(
            probe_command=downstream_probe_command,
            token=token,
            fallback={
                "number": pr_number,
                "state": pr_state,
                "headRefName": head_ref or head_sha,
            },
        )

        with tempfile.TemporaryDirectory(prefix="occ-companion-") as tmpdir:
            clone_dir = Path(tmpdir) / "onex_change_control"
            self._clone_and_branch(clone_dir, branch, tmpdir, token)

            contract_paths: dict[str, Path] = {}
            for ticket in tickets:
                contract_path = clone_dir / "contracts" / f"{ticket}.yaml"
                contract_path.parent.mkdir(parents=True, exist_ok=True)
                if not contract_path.is_file():
                    contract_path.write_text(
                        render_companion_contract(
                            ticket_id=ticket,
                            repo=repo,
                            pr_number=pr_number,
                            evidence_id=evidence_id,
                        ),
                        encoding="utf-8",
                    )
                contract_paths[ticket] = contract_path

                # Stage 1: downstream receipt stamped with the actual landed
                # commit — the squash mergeCommit.oid post-merge, else the
                # reviewed head SHA pre-merge (OMN-14255).
                downstream_dir = (
                    clone_dir / "drift" / "dod_receipts" / ticket / evidence_id
                )
                downstream_dir.mkdir(parents=True, exist_ok=True)
                (downstream_dir / "command.yaml").write_text(
                    render_downstream_receipt(
                        ticket_id=ticket,
                        evidence_id=evidence_id,
                        pr_number=pr_number,
                        repo=repo,
                        run_timestamp=run_timestamp,
                        commit_sha=receipt_commit_sha,
                        branch=branch,
                        probe_command=downstream_probe_command,
                        probe_stdout=downstream_stdout,
                        exit_code=downstream_exit,
                        runner=self._runner,
                        verifier=self._verifier,
                    ),
                    encoding="utf-8",
                )
                self._rebind_all_receipts(clone_dir, ticket, contract_path)

            self._run_git(["git", "add", "contracts", "drift"], cwd=str(clone_dir))
            self._run_git(
                [
                    "git",
                    "commit",
                    "-m",
                    (
                        f"evidence({', '.join(tickets)}): author OCC companion "
                        f"for {repo}#{pr_number}\n\n"
                        f"OCC companion by node_pr_lifecycle_fix_effect "
                        f"(OMN-13317 F1 / OMN-13990 / OMN-14285). "
                        f"Product PR head {head_sha}."
                    ),
                ],
                cwd=str(clone_dir),
            )
            # Force-push: the auto/* bot branch is fully REGENERATED each run
            # (fresh clone off the default + freshly-timestamped receipts), so a
            # `synchronize` re-fire produces history disjoint from the already
            # pushed remote branch — a plain push would be rejected non-fast-
            # forward (OMN-13990 / CodeRabbit). Force-push is safe here (content
            # is deterministic and the branch always presents the companion as
            # all-adds relative to base, keeping the append-only gate green).
            self._run_git(
                ["git", "push", "--force", "origin", branch], cwd=str(clone_dir)
            )

            # 3. Open or sync the OCC binding PR (one per product PR).
            occ_pr_number = self._open_or_sync_occ_pr(
                branch=branch, ticket=tickets[0], repo=repo, pr_number=pr_number
            )

            # Genuine OCC-PR probe for the self-bind receipts.
            occ_owner, occ_repo_name = split_repo(self._occ_repo)
            occ_pr_data = rest_json(
                "GET",
                f"/repos/{occ_owner}/{occ_repo_name}/pulls/{occ_pr_number}",
                token=token,
            )
            occ_state = occ_pr_data.get("state") or "open"
            occ_head_sha = self._head_sha(str(clone_dir))
            occ_probe_command = (
                f"gh pr view {occ_pr_number} --repo {self._occ_repo} "
                "--json number,state"
            )
            occ_stdout, occ_exit = self._observe_pr_probe(
                probe_command=occ_probe_command,
                token=token,
                fallback={"number": occ_pr_number, "state": occ_state},
            )

            # Stage 2: self-binding receipt per ticket with the REAL OCC PR + head.
            for ticket in tickets:
                self_bind_dir = (
                    clone_dir
                    / "drift"
                    / "dod_receipts"
                    / ticket
                    / f"occ-self-bind-pr-{occ_pr_number}"
                )
                self_bind_dir.mkdir(parents=True, exist_ok=True)
                (self_bind_dir / "command.yaml").write_text(
                    render_self_bind_receipt(
                        ticket_id=ticket,
                        evidence_id=f"occ-self-bind-pr-{occ_pr_number}",
                        occ_pr_number=occ_pr_number,
                        occ_repo=self._occ_repo,
                        run_timestamp=run_timestamp,
                        occ_commit_sha=occ_head_sha,
                        branch=branch,
                        probe_command=occ_probe_command,
                        probe_stdout=occ_stdout,
                        exit_code=occ_exit,
                        runner=self._runner,
                        verifier=self._verifier,
                    ),
                    encoding="utf-8",
                )
                # Rebind contract hash across ALL matching receipts (friction #9).
                self._rebind_all_receipts(clone_dir, ticket, contract_paths[ticket])

            self._run_git(["git", "add", "drift"], cwd=str(clone_dir))
            self._run_git(
                [
                    "git",
                    "commit",
                    "-m",
                    (
                        f"evidence({', '.join(tickets)}): self-bind "
                        f"OCC#{occ_pr_number} + rebind contract_sha256"
                    ),
                ],
                cwd=str(clone_dir),
            )
            # Force-push (see rationale above): deterministic all-adds regeneration.
            self._run_git(
                ["git", "push", "--force", "origin", branch], cwd=str(clone_dir)
            )

        # 5. PATCH Evidence-Source: OCC#<n> back onto the product PR via REST.
        self._patch_evidence_source(
            repo=repo,
            pr_number=pr_number,
            occ_pr_number=occ_pr_number,
            tickets=tickets,
            existing_body=body,
        )

        action = (
            f"authored OCC companion Evidence-Source: OCC#{occ_pr_number} for "
            f"{', '.join(tickets)} on {repo}#{pr_number} "
            f"(product head {head_sha}, branch {branch})"
        )
        logger.info("occ_companion_emitter: %s", action)
        return action

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_tickets(title: str, body: str) -> list[str]:
        """Return the gate-identical cited ticket set (OMN-13990 D3).

        Delegates to the occ-preflight eligibility validator's ``_extract_ticket_ids``
        (body closing-keywords exclusive, else all title tokens) so the emitter
        authors a companion for exactly the tickets the gate will check.
        """
        return _extract_ticket_ids(body, title)

    def _observe_pr_probe(
        self, *, probe_command: str, token: str, fallback: dict[str, object]
    ) -> tuple[str, int]:
        """Execute the declared probe and return ``(probe_stdout, exit_code)``.

        Runs the real ``gh pr view --json`` probe so the receipt carries
        machine-observed output rather than a fabricated template (OMN-13990
        item 4 / OMN-14055). ``gh`` authenticates from ``GH_TOKEN``. When ``gh``
        is unavailable or errors, the probe falls back to the fields already
        observed from the GitHub REST API — still genuine GitHub facts, never a
        hardcoded template — and reports exit_code 0 because the PR was in fact
        observed. Output is re-serialised as a single compact JSON line so the
        receipt's YAML block scalar stays well-formed.
        """
        fallback_json = json.dumps(fallback, separators=(",", ":"), sort_keys=True)
        try:
            env = os.environ.copy()
            env["GH_TOKEN"] = token
            result = subprocess.run(
                shlex.split(probe_command),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return fallback_json, 0
        if result.returncode != 0 or not result.stdout.strip():
            return fallback_json, 0
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            return result.stdout.strip().replace("\n", " "), 0
        return json.dumps(parsed, separators=(",", ":"), sort_keys=True), 0

    def _clone_and_branch(
        self, clone_dir: Path, branch: str, tmpdir: str, token: str
    ) -> None:
        # HTTPS x-access-token clone/push (OMN-13990): the effects container has
        # no SSH identity. The token is redacted from any surfaced git error by
        # occ_git_transport.run_git.
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
        self._run_git(
            ["git", "config", "user.name", self._git_author_name],
            cwd=str(clone_dir),
        )
        self._run_git(
            ["git", "config", "user.email", self._git_author_email],
            cwd=str(clone_dir),
        )
        self._run_git(["git", "checkout", "-b", branch], cwd=str(clone_dir))

    def _rebind_all_receipts(
        self, clone_dir: Path, ticket: str, contract_path: Path
    ) -> None:
        """Rebind every matching receipt's contract hash binding(s).

        Legacy whole-file: sets ``contract_sha256`` to sha256(contract) on
        every receipt, mirroring the overnight-sweep manual recipe (friction
        #9): ``LC_ALL=C shasum -a 256 contracts/<ticket>.yaml`` then rewrite
        the hash in ``drift/dod_receipts/<ticket>/*/command.yaml``. Kept for
        backward compat — the hardening gate still falls back to it for
        receipts with no per-entry hash (self-bind receipts).

        Per-entry (OMN-13888 / OMN-14418 residual 3): for a receipt that
        declares ``contract_entry_sha256``, also rebind that field to
        ``compute_contract_entry_sha256(contract_data, receipt's own
        evidence_item_id)`` — the SAME canonical per-entry hasher the
        consumer-side gates recompute against, never a local
        re-implementation. This is per-receipt evidence_item_id-aware (each
        receipt's own declared id, extracted from its own text) rather than
        one blanket whole-file substitution, so a contract with multiple
        dod_evidence items (e.g. the downstream check plus a future sibling
        check) rebinds each receipt against its own entry, not a shared
        digest. A receipt whose evidence_item_id is not declared in the
        contract's dod_evidence (self-bind receipts, by design — see
        occ_evidence_stamp) is left with the field absent/unset; there is no
        entry for it to bind to, so ContractEntryNotFoundError is swallowed
        for that one receipt rather than aborting the whole rebind sweep.

        Rendering + hashing stay in the pure :mod:`occ_evidence_stamp` seam
        (plus the canonical omnibase_core per-entry hasher); this method only
        does the file I/O.
        """
        contract_bytes = contract_path.read_bytes()
        whole_file_digest = compute_contract_sha256(contract_bytes)
        contract_data = yaml.safe_load(contract_bytes)
        receipt_root = clone_dir / "drift" / "dod_receipts" / ticket
        for receipt in receipt_root.rglob("*.yaml"):
            text = receipt.read_text(encoding="utf-8")
            new_text = rebind_contract_sha256_in_text(text, whole_file_digest)

            evidence_item_id = extract_evidence_item_id(new_text)
            if evidence_item_id is not None:
                try:
                    entry_digest = compute_contract_entry_sha256(
                        contract_data, evidence_item_id
                    )
                except ContractEntryNotFoundError:
                    # Not every receipt binds to a declared dod_evidence item
                    # (self-bind receipts, by design) — nothing to rebind.
                    pass
                else:
                    new_text = rebind_contract_entry_sha256_in_text(
                        new_text, entry_digest
                    )

            if new_text != text:
                receipt.write_text(new_text, encoding="utf-8")

    def _run_git(self, argv: list[str], *, cwd: str) -> str:
        # Delegates to the shared transport, which redacts any embedded
        # x-access-token credential from a surfaced git error (OMN-13990).
        return run_git(argv, cwd=cwd, timeout=_GIT_TIMEOUT_SECONDS)

    def _head_sha(self, cwd: str) -> str:
        return self._run_git(["git", "rev-parse", "HEAD"], cwd=cwd)

    @staticmethod
    def _receipt_commit_sha(pr_data: dict[str, object], head_sha: str) -> str:
        """Resolve the commit SHA the downstream receipt should cite (OMN-14255).

        Returns the actual squash ``merge_commit_sha`` ONLY when the PR is truly
        merged; otherwise the reviewed ``head_sha``. GitHub's REST ``merge_commit_sha``
        on an OPEN PR is a throwaway *test-merge* SHA that does not exist on any
        branch, so it must never be cited — gate on the ``merged`` / ``merged_at``
        facts before trusting it. Pure function — no I/O.
        """
        merged = bool(pr_data.get("merged")) or bool(pr_data.get("merged_at"))
        merge_commit_sha = pr_data.get("merge_commit_sha")
        if (
            merged
            and isinstance(merge_commit_sha, str)
            and SHA_RE.match(merge_commit_sha)
        ):
            return merge_commit_sha
        return head_sha

    def _open_or_sync_occ_pr(
        self, *, branch: str, ticket: str, repo: str, pr_number: int
    ) -> int:
        """Open the OCC binding PR, or return the existing PR for this branch.

        ``synchronize`` re-fires the emitter for the same product PR; the OCC
        branch already exists and pushing updates it, so a fresh ``create`` 422s.
        Look up the open PR for the branch head first.
        """
        token = _resolve_github_token()
        owner, repo_name = split_repo(self._occ_repo)

        existing_number = self._first_open_pr_number(owner, repo_name, branch, token)
        if existing_number is not None:
            return existing_number

        # Human prose is authored here; the Evidence-Ticket line is rendered by
        # the Piece-2 core renderer over the typed stamp (no inline stamp text).
        prose = (
            f"Autobind OCC evidence for `{ticket}`.\n\n"
            f"Triggered by node_pr_lifecycle_fix_effect OCC companion emitter on "
            f"{repo}#{pr_number} (OMN-13317 F1 / OMN-14285).\n"
        )
        body = render_occ_companion_pr_body(prose, tickets=[ticket])
        # OMN-13990: target OCC's DEFAULT branch, not a hardcoded "main". The
        # branch is cut from the shallow clone of the default (OCC default is
        # `dev`); a PR based on "main" surfaces the entire dev<->main delta
        # (thousands of files) with the 3 companion files buried in it — an
        # unmergeable mega-PR. Basing on the default keeps the companion PR a
        # clean net-new-files diff.
        base = self._occ_default_branch(owner, repo_name, token)
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

    @staticmethod
    def _occ_default_branch(owner: str, repo_name: str, token: str) -> str:
        """Return the OCC repo's default branch (the correct companion PR base)."""
        info = rest_json("GET", f"/repos/{owner}/{repo_name}", token=token)
        default = info.get("default_branch")
        if not isinstance(default, str) or not default:
            raise RuntimeError(
                f"could not resolve default branch for {owner}/{repo_name}"
            )
        return default

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
        tickets: Sequence[str],
        existing_body: str,
    ) -> None:
        """Rebind the product PR body to ``Evidence-Source: OCC#<n>`` via REST PATCH.

        The new body is produced entirely by the Piece-2 core renderer over the
        typed stamp (human prose preserved verbatim, one canonical Evidence
        block) — no inline f-string authoring. Idempotent: when the rendered body
        equals the existing body there is nothing to write.

        ``gh pr edit`` and GraphQL silently no-op on Projects-classic repos
        (friction #7); REST PATCH of the body is the reliable path.
        """
        new_body = render_product_pr_body_with_occ_source(
            existing_body, occ_pr_number=occ_pr_number, tickets=tickets
        )
        if new_body == existing_body:
            return  # already canonical — no-op
        token = _resolve_github_token()
        owner, repo_name = split_repo(repo)
        rest_json(
            "PATCH",
            f"/repos/{owner}/{repo_name}/pulls/{pr_number}",
            token=token,
            body={"body": new_body},
        )


__all__ = ["OccCompanionEmitter"]
