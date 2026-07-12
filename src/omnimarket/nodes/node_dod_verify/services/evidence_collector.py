# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Evidence collector — loads contract YAML, runs dod_evidence checks, returns results.

Responsibilities:
  1. Locate and load a ticket's contract YAML (auto-detect or explicit path).
  2. Iterate over ``dod_evidence[]`` items.
  3. For each item's ``checks[]``, execute the check.
  4. Return a list of ModelEvidenceCheckResult for the handler to tally.

This module is the I/O boundary — it reads files and runs subprocesses.
The handler itself remains pure (no I/O) and continues to work when callers
pre-populate evidence_results (tests, event-bus consumers).
"""

from __future__ import annotations

import glob
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, cast

import yaml
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_evidence_github_effect import (
    HandlerDodEvidenceGithubEffect,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_evidence_github_lookup import (
    EnumDodEvidenceGithubOperation,
    ModelDodEvidenceGithubLookupCommand,
    ModelDodEvidenceGithubLookupResultEvent,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.durable_evidence_gate import (
    apply_supersessions,
    extract_receipt_merge_commits,
)

logger = logging.getLogger(__name__)

# Default contract search roots (first match wins)
_DEFAULT_CONTRACT_ROOTS: list[str] = [
    "${ONEX_CC_REPO_PATH}/contracts",
    "${OMNI_HOME}/onex_change_control/contracts",
]

# OMN-13888 (scope 6): OCC governance is dev-targeted — contracts and receipts
# land on the OCC ``dev`` branch first and are batched to ``main`` later. The
# canonical omni_home clones track ``main``, so a contract merged only to ``dev``
# is invisible to a working-tree search (the OMN-13899 "No contract found",
# correlation 93b4e964). Resolve from this ref instead. Overridable via
# ``OCC_GOVERNANCE_REF`` for tests / operators. Mirrors
# ``DurableEvidenceGate.DEFAULT_OCC_GOVERNANCE_REF``.
_DEFAULT_OCC_GOVERNANCE_REF = "origin/dev"

# Wall-clock ceiling for the OCC git worktree/fetch subprocesses. A shared OCC
# clone can hit lock contention under concurrent collect() calls, or a fetch can
# stall on the network; without a timeout a stuck git op would block the whole
# collect() with no recovery (CodeRabbit — Stability). Kept generous because a
# fetch of the OCC repo may transfer real objects.
_GIT_OP_TIMEOUT_S = 60

# OMN-14207: live GitHub PR-state verification.
#
# A dod_evidence item that BINDS to a GitHub PR is additionally verified against
# the LIVE PR state: the PR must be MERGED and all status checks green. This runs
# ALONGSIDE the contract's declared hash/receipt checks (it never replaces them).
#
# It closes a false-positive class: a static receipt can record ``status: PASS``
# while the product PR is actually unmerged / CI-red, so the grep-the-receipt
# check passes and dod_verify reports ``verified`` for work that is neither merged
# nor green. OMN-13996 is the discovery case — ``dod_verify`` said ``verified 3/3``
# while ``omnibase_infra#2216`` was OPEN with 7 failing required checks.
#
# The binding source is authoritative, not heuristic (evidence-item ``id`` slugs
# are unreliable repo names): an explicit ``pr`` field on the item, else the
# durable receipt's ``pr_number`` + probed ``--repo owner/repo`` — the SAME fields
# the DurableEvidenceGate binds against.
_LIVE_PR_CHECK_ENV = "DOD_VERIFY_LIVE_PR_CHECK"
_DEFAULT_GITHUB_ORG = "OmniNode-ai"
# NOTE: the gh-CLI timeout and check-green-state constants formerly declared
# here (``_GH_PR_TIMEOUT_S``, ``_GH_CHECK_GREEN_STATES``) moved to
# ``handler_dod_evidence_github_effect.py`` with the subprocess calls that used
# them (OMN-14400, RSD-1 of OMN-14398).


class EvidenceCollector:
    """Loads a ticket contract and runs dod_evidence checks.

    Usage::

        collector = EvidenceCollector()
        results = collector.collect("OMN-9414")
        # results: list[ModelEvidenceCheckResult]
    """

    def __init__(self, timeout_per_check: int = 30) -> None:
        self._timeout = timeout_per_check
        # When set (during a dev-resolved collect), an origin/dev worktree of the
        # OCC repo. Contract-load AND the shell greps run inside it so dev-only
        # contracts + receipts are visible (OMN-13888 scope 6).
        self._occ_dev_root: str | None = None
        self._occ_governance_ref = (
            os.environ.get("OCC_GOVERNANCE_REF", _DEFAULT_OCC_GOVERNANCE_REF).strip()
            or _DEFAULT_OCC_GOVERNANCE_REF
        )

    @staticmethod
    def _github_lookup_result(
        output: ModelHandlerOutput[None],
    ) -> ModelDodEvidenceGithubLookupResultEvent:
        """Type-narrow HandlerDodEvidenceGithubEffect's single emitted event.

        ``ModelHandlerOutput.events`` is ``tuple[Any, ...]`` (the generic
        dispatch-engine shape); this handler always emits exactly one
        ``ModelDodEvidenceGithubLookupResultEvent`` per call (OMN-14400).
        """
        return cast(ModelDodEvidenceGithubLookupResultEvent, output.events[0])

    def collect(
        self,
        ticket_id: str,
        contract_path: str | None = None,
    ) -> list[ModelEvidenceCheckResult]:
        """Load contract and run all dod_evidence checks.

        When no explicit ``contract_path`` is given, OCC governance is dev-first
        (contracts and receipts land on the OCC ``dev`` branch first and are
        batched to ``main`` later; the canonical clones track ``main``). Per the
        OMN-13888 scope-6 decision of record, an auto-detected OCC contract is
        ALWAYS resolved from an ``origin/dev`` worktree when that worktree can be
        materialised and carries the contract — even when a (possibly STALE) copy
        exists on the ``main``-tracking working tree. This closes the round-1
        residual edge where a stale ``main`` copy was used as-is because the
        contract was merely *present* on the working tree so the rider never
        fired. The working tree is used only as a fallback (dev worktree cannot be
        materialised, or the contract is absent on dev). The worktree is removed
        before returning.
        """
        if contract_path is not None:
            return self._collect_impl(ticket_id, contract_path)

        created_worktree: Path | None = None
        try:
            dev_root, created_worktree = self._materialize_occ_dev_worktree()
            if dev_root is not None:
                dev_candidate = Path(dev_root) / "contracts" / f"{ticket_id}.yaml"
                if dev_candidate.exists():
                    # dev is authoritative — prefer it over any working-tree copy.
                    self._occ_dev_root = dev_root
                    logger.info(
                        "Resolved OCC contract for %s from %s worktree at %s "
                        "(dev-first, overrides any main working-tree copy)",
                        ticket_id,
                        self._occ_governance_ref,
                        dev_root,
                    )
                elif self._find_contract(ticket_id) is None:
                    logger.info(
                        "Contract %s absent on %s and on the working tree; "
                        "collect will report it missing",
                        ticket_id,
                        self._occ_governance_ref,
                    )
            return self._collect_impl(ticket_id, contract_path)
        finally:
            self._occ_dev_root = None
            if created_worktree is not None:
                self._remove_occ_dev_worktree(created_worktree)

    def _resolve_occ_root(self) -> Path | None:
        """Return the OCC repo root from the environment, or None."""
        cc_repo_path = os.environ.get("ONEX_CC_REPO_PATH", "").strip()
        if cc_repo_path and Path(cc_repo_path).is_dir():
            return Path(cc_repo_path)
        omni_home = os.environ.get("OMNI_HOME", "").strip()
        if omni_home:
            occ = Path(omni_home) / "onex_change_control"
            if occ.is_dir():
                return occ
        return None

    def _materialize_occ_dev_worktree(self) -> tuple[str | None, Path | None]:
        """Add a detached ``origin/dev`` worktree of the OCC repo.

        Returns ``(worktree_path_str, worktree_path)`` on success, else
        ``(None, None)``. The worktree is placed under ``OMNI_HOME`` (when set) so
        relative ``file_exists`` checks stay inside the containment boundary.
        """
        occ = self._resolve_occ_root()
        if occ is None:
            return None, None
        # Refresh the remote-tracking ref first so a long-lived OMNI_HOME clone
        # does not materialise a STALE origin/dev and miss the very contract this
        # rider exists to pick up (CodeRabbit — Data Integrity). Best-effort: a
        # fetch failure (offline, no remote — e.g. the local-branch test ref)
        # falls through to whatever the local clone already has.
        self._refresh_occ_ref(occ)
        omni_home = os.environ.get("OMNI_HOME", "").strip()
        parent = Path(omni_home) if omni_home and Path(omni_home).is_dir() else None
        try:
            tmp = Path(tempfile.mkdtemp(prefix=".occ-dev-wt-", dir=parent))
        except OSError:
            return None, None
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(occ),
                    "worktree",
                    "add",
                    "--detach",
                    "--force",
                    str(tmp),
                    self._occ_governance_ref,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=_GIT_OP_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "Timed out materialising %s worktree of OCC after %ss",
                self._occ_governance_ref,
                _GIT_OP_TIMEOUT_S,
            )
            shutil.rmtree(tmp, ignore_errors=True)
            return None, None
        if proc.returncode != 0:
            logger.warning(
                "Could not materialise %s worktree of OCC: %s",
                self._occ_governance_ref,
                proc.stderr.strip(),
            )
            shutil.rmtree(tmp, ignore_errors=True)
            return None, None
        return str(tmp), tmp

    def _refresh_occ_ref(self, occ: Path) -> None:
        """Best-effort ``git fetch`` of the OCC governance ref's remote branch.

        Only fires for a ``<remote>/<branch>`` ref (e.g. ``origin/dev``); a bare
        local-branch ref (test override) is left untouched. Failures are logged
        and swallowed — the worktree add proceeds against the local clone.
        """
        ref = self._occ_governance_ref
        if "/" not in ref:
            return
        remote, branch = ref.split("/", 1)
        try:
            proc = subprocess.run(
                ["git", "-C", str(occ), "fetch", "--quiet", remote, branch],
                capture_output=True,
                text=True,
                check=False,
                timeout=_GIT_OP_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Timed out fetching %s for OCC worktree refresh", ref)
            return
        if proc.returncode != 0:
            logger.info(
                "OCC ref refresh (git fetch %s %s) failed; using local clone: %s",
                remote,
                branch,
                proc.stderr.strip(),
            )

    def _remove_occ_dev_worktree(self, worktree: Path) -> None:
        occ = self._resolve_occ_root()
        if occ is not None:
            try:
                proc = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(occ),
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=_GIT_OP_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Timed out removing OCC worktree %s after %ss",
                    worktree,
                    _GIT_OP_TIMEOUT_S,
                )
                proc = None
            # A failed/timed-out `worktree remove` leaves a stale registration
            # under .git/worktrees/ even after rmtree deletes the directory;
            # prune it so the shared OCC clone does not accumulate dead entries
            # (CodeRabbit — Stability).
            if proc is None or proc.returncode != 0:
                if proc is not None:
                    logger.warning(
                        "git worktree remove failed for %s: %s; pruning registration",
                        worktree,
                        proc.stderr.strip(),
                    )
                self._prune_occ_worktrees(occ)
        shutil.rmtree(worktree, ignore_errors=True)

    def _prune_occ_worktrees(self, occ: Path) -> None:
        """Best-effort ``git worktree prune`` to clear stale registrations."""
        try:
            subprocess.run(
                ["git", "-C", str(occ), "worktree", "prune"],
                capture_output=True,
                text=True,
                check=False,
                timeout=_GIT_OP_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Timed out pruning OCC worktrees under %s", occ)

    def _collect_impl(
        self,
        ticket_id: str,
        contract_path: str | None = None,
    ) -> list[ModelEvidenceCheckResult]:
        """Load contract and run all dod_evidence checks (worktree-agnostic core).

        Args:
            ticket_id: Linear ticket ID (e.g. OMN-1234).
            contract_path: Explicit path to contract YAML. If None, auto-detect.

        Returns:
            One ModelEvidenceCheckResult per dod_evidence item.
        """
        if contract_path is not None:
            path = Path(contract_path)
            if not path.exists():
                return [
                    ModelEvidenceCheckResult(
                        evidence_id="contract",
                        description=f"Contract file not found: {contract_path}",
                        status=EnumEvidenceCheckStatus.FAILED,
                        message=f"File does not exist: {contract_path}",
                    )
                ]
        else:
            found = self._find_contract(ticket_id)
            if found is None:
                return [
                    ModelEvidenceCheckResult(
                        evidence_id="contract",
                        description=f"No contract found for {ticket_id}",
                        status=EnumEvidenceCheckStatus.SKIPPED,
                        message=(
                            f"Searched: {_DEFAULT_CONTRACT_ROOTS}. "
                            "Provide --contract-path or generate a contract."
                        ),
                    )
                ]
            path = found

        raw = self._load_yaml(path)
        if raw is None:
            return [
                ModelEvidenceCheckResult(
                    evidence_id="contract",
                    description=f"Failed to parse contract: {path}",
                    status=EnumEvidenceCheckStatus.FAILED,
                    message=f"YAML parse error in {path}",
                )
            ]

        # Validate contract belongs to the requested ticket
        contract_ticket_id = raw.get("ticket_id")
        if contract_ticket_id != ticket_id:
            return [
                ModelEvidenceCheckResult(
                    evidence_id="contract",
                    description=f"Contract ticket mismatch: {path}",
                    status=EnumEvidenceCheckStatus.FAILED,
                    message=(
                        f"Expected ticket_id {ticket_id!r}, "
                        f"found {contract_ticket_id!r}."
                    ),
                )
            ]

        dod_items = raw.get("dod_evidence", [])
        if not isinstance(dod_items, list):
            return [
                ModelEvidenceCheckResult(
                    evidence_id="contract",
                    description=f"Invalid dod_evidence structure in contract: {path}",
                    status=EnumEvidenceCheckStatus.FAILED,
                    message="dod_evidence must be a list of mappings.",
                )
            ]
        if not dod_items:
            return [
                ModelEvidenceCheckResult(
                    evidence_id="contract",
                    description=f"No dod_evidence entries in contract: {path}",
                    status=EnumEvidenceCheckStatus.SKIPPED,
                    message="Contract has empty or missing dod_evidence[] section.",
                )
            ]

        results: list[ModelEvidenceCheckResult] = []
        for item in dod_items:
            result = self._check_evidence_item(item, ticket_id, path)
            results.append(result)
            # OMN-14207: verify the LIVE PR state for any PR-bound item. Emitted
            # as additional check result(s) ALONGSIDE the item's declared checks
            # so a static ``status: PASS`` receipt can no longer mask an unmerged
            # or CI-red product PR.
            if isinstance(item, dict):
                results.extend(self._live_pr_checks_for_item(item, ticket_id, path))

        return results

    def _find_contract(self, ticket_id: str) -> Path | None:
        """Search standard locations for a ticket contract."""
        # OMN-13888 (scope 6): a materialised origin/dev worktree wins so a
        # dev-only contract resolves instead of falling through to "No contract".
        if self._occ_dev_root:
            dev_candidate = Path(self._occ_dev_root) / "contracts" / f"{ticket_id}.yaml"
            if dev_candidate.exists():
                logger.info("Found contract at %s (dev worktree)", dev_candidate)
                return dev_candidate
        for root_template in _DEFAULT_CONTRACT_ROOTS:
            root = Path(os.path.expandvars(root_template))
            candidate = root / f"{ticket_id}.yaml"
            if candidate.exists():
                logger.info("Found contract at %s", candidate)
                return candidate

        # Fallback: resolve via OMNI_HOME env var
        omni_home = os.environ.get("OMNI_HOME", "")
        candidate = (
            Path(omni_home) / "onex_change_control" / "contracts" / f"{ticket_id}.yaml"
        )
        if candidate.exists():
            logger.info("Found contract at %s", candidate)
            return candidate

        return None

    def _load_yaml(self, path: Path) -> dict[str, Any] | None:
        """Load and return YAML content, or None on error."""
        try:
            content = path.read_text(encoding="utf-8")
            raw = yaml.safe_load(content)
            if not isinstance(raw, dict):
                logger.error("Contract %s root is not a mapping", path)
                return None
            return raw
        except Exception as exc:
            logger.error("Failed to parse %s: %s", path, exc)
            return None

    def _check_evidence_item(
        self,
        item: dict[str, Any],
        ticket_id: str,
        contract_path: Path | None = None,
    ) -> ModelEvidenceCheckResult:
        """Run checks for a single dod_evidence item."""
        evidence_id = item.get("id", "unknown")
        description = item.get("description", evidence_id)
        checks = item.get("checks", [])

        if not isinstance(checks, list):
            return ModelEvidenceCheckResult(
                evidence_id=evidence_id,
                description=description,
                status=EnumEvidenceCheckStatus.FAILED,
                message="checks must be a list of mappings.",
            )

        if not checks:
            return ModelEvidenceCheckResult(
                evidence_id=evidence_id,
                description=description,
                status=EnumEvidenceCheckStatus.SKIPPED,
                message="No checks defined for this evidence item.",
            )

        # Run each check; all must pass for the item to be VERIFIED
        messages: list[str] = []
        for check in checks:
            check_type = check.get("check_type") or ""
            if check_type in ("command", "test_passes"):
                # ``test_passes`` is a semantic alias for ``command`` that signals
                # the command is a test runner (typically ``uv run pytest ...``).
                # Both share the same execution path: run the shell command and
                # treat exit code 0 as VERIFIED. The alias exists so contracts
                # can declare intent (running tests) distinct from generic
                # commands without forcing every shell-based check into the same
                # bucket. Regression for OMN-10046.
                ok, msg = self._run_command_check(check, ticket_id, contract_path)
                if not ok:
                    return ModelEvidenceCheckResult(
                        evidence_id=evidence_id,
                        description=description,
                        status=EnumEvidenceCheckStatus.FAILED,
                        message=msg,
                    )
                messages.append(msg)
            elif check_type == "file_exists":
                ok, msg = self._run_file_exists_check(check, contract_path)
                if not ok:
                    return ModelEvidenceCheckResult(
                        evidence_id=evidence_id,
                        description=description,
                        status=EnumEvidenceCheckStatus.FAILED,
                        message=msg,
                    )
                messages.append(msg)
            else:
                # Unknown or missing check_type must FAIL, not SKIPPED.
                # Silently skipping unknown types is the OMN-9571 bug class:
                # a misspelled or unregistered check_type would let DoD evidence
                # pass trivially without running any real check.
                label = check_type if check_type else "<missing check_type key>"
                return ModelEvidenceCheckResult(
                    evidence_id=evidence_id,
                    description=description,
                    status=EnumEvidenceCheckStatus.FAILED,
                    message=(
                        f"Unknown check_type: {label!r}. "
                        "Supported: command, test_passes, file_exists."
                    ),
                )

        return ModelEvidenceCheckResult(
            evidence_id=evidence_id,
            description=description,
            status=EnumEvidenceCheckStatus.VERIFIED,
            message="; ".join(messages) if messages else None,
        )

    def _resolve_cwd(
        self,
        cwd_template: str,
        ticket_id: str,
    ) -> tuple[str | None, str | None]:
        """Resolve a ``cwd`` template string into an absolute, contained path.

        Supports the ``${OMNI_HOME}``, ``${PR_NUMBER}``, ``${REPO}``, and
        ``${TICKET_ID}`` template tokens introduced by OMN-10078 (mirroring
        the OMN-10086 substitution pattern from the contract-compliance
        runner). Returns ``(resolved_path, None)`` on success or
        ``(None, error_message)`` on failure.

        Containment rules (defence-in-depth — the model itself does not
        validate `cwd`):

        - ``..`` segments in the raw input are rejected up-front
        - the resolved path must be relative to ``OMNI_HOME`` (when set);
          paths that escape via symlinks are rejected after ``Path.resolve()``
        - the resolved path must exist and be a directory
        """
        if ".." in Path(cwd_template).parts:
            return None, f"cwd path traversal not allowed: {cwd_template}"

        # Build the substitution table. Missing tokens leave the literal
        # ``${TOKEN}`` in place — the existence/containment check below is
        # what flags a bad cwd.
        substitutions = {
            "OMNI_HOME": os.environ.get("OMNI_HOME", ""),
            "PR_NUMBER": os.environ.get("PR_NUMBER", ""),
            "REPO": os.environ.get("REPO", ""),
            "TICKET_ID": ticket_id,
        }
        rendered = cwd_template
        for token, value in substitutions.items():
            rendered = rendered.replace(f"${{{token}}}", value)
        # Also support bare $TOKEN form via os.path.expandvars for any
        # tokens we did not template explicitly (e.g. user-set vars).
        rendered = os.path.expandvars(rendered)

        if "${" in rendered or rendered == "":
            return None, (
                f"cwd contains unresolved template tokens or is empty after "
                f"substitution: {cwd_template!r} -> {rendered!r}"
            )

        candidate = Path(rendered).resolve()

        omni_home = os.environ.get("OMNI_HOME")
        if omni_home:
            base = Path(omni_home).resolve()
            if not candidate.is_relative_to(base):
                return None, (
                    f"cwd escapes OMNI_HOME containment: {cwd_template!r} "
                    f"resolved to {candidate}"
                )

        if not candidate.exists():
            return None, f"cwd does not exist: {cwd_template!r} -> {candidate}"
        if not candidate.is_dir():
            return None, f"cwd is not a directory: {candidate}"

        return str(candidate), None

    def _lookup_pr_for_ticket(self, ticket_id: str) -> str:
        """Return the merged PR number string for ticket_id, or empty string.

        Checks PR_NUMBER env var first (not gh I/O — stays here). Falls back
        to HandlerDodEvidenceGithubEffect's ``gh pr list`` search (OMN-14400,
        RSD-1 of OMN-14398 — behavior-identical carve-out of the gh-CLI I/O
        into a canonical EFFECT handler). Returns empty string when nothing
        can be resolved (caller must handle unresolved placeholders
        gracefully).
        """
        env_val = os.environ.get("PR_NUMBER", "").strip()
        if env_val:
            return env_val
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET,
            ticket_id=ticket_id,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        return self._github_lookup_result(output).text_value

    def _lookup_repo_for_ticket(self, ticket_id: str) -> str:
        """Return the ``owner/repo`` string for ticket_id, or empty string.

        Checks REPO env var first (not gh I/O — stays here). Falls back to
        HandlerDodEvidenceGithubEffect's ``gh pr list`` search to discover
        which repo contains a merged PR for this ticket (OMN-14400, RSD-1 of
        OMN-14398).
        """
        env_val = os.environ.get("REPO", "").strip()
        if env_val:
            return env_val
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.LOOKUP_REPO_FOR_TICKET,
            ticket_id=ticket_id,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        return self._github_lookup_result(output).text_value

    def _resolve_command_placeholders(
        self,
        cmd_str: str,
        ticket_id: str,
    ) -> tuple[str, str | None]:
        """Substitute all placeholder forms in a command string.

        Supported forms:
          - ``{ticket_id}``, ``{pr}``, ``{repo}``  (Python format-style)
          - ``${TICKET_ID}``, ``${PR_NUMBER}``, ``${REPO}``  (shell-style)

        Returns ``(resolved_cmd, None)`` on success.
        Returns ``(original_cmd, error_message)`` when a required placeholder
        cannot be resolved (e.g. no merged PR found for ticket).
        """
        needs_pr = "{pr}" in cmd_str or "${PR_NUMBER}" in cmd_str
        needs_repo = "{repo}" in cmd_str or "${REPO}" in cmd_str

        pr_num = self._lookup_pr_for_ticket(ticket_id) if needs_pr else ""
        repo = self._lookup_repo_for_ticket(ticket_id) if needs_repo else ""

        if needs_pr and not pr_num:
            return cmd_str, (
                f"Cannot resolve PR number for {ticket_id}: "
                "set PR_NUMBER env var or ensure a merged PR exists."
            )
        if needs_repo and not repo:
            return cmd_str, (
                f"Cannot resolve repo for {ticket_id}: "
                "set REPO env var or ensure a merged PR exists."
            )

        # Apply shell-style substitutions first (${...} → value)
        cmd_str = cmd_str.replace("${TICKET_ID}", shlex.quote(ticket_id))
        if pr_num:
            cmd_str = cmd_str.replace("${PR_NUMBER}", shlex.quote(pr_num))
        if repo:
            cmd_str = cmd_str.replace("${REPO}", shlex.quote(repo))

        # Apply Python-format-style substitutions ({...} → value)
        cmd_str = cmd_str.replace("{ticket_id}", shlex.quote(ticket_id))
        if pr_num:
            cmd_str = cmd_str.replace("{pr}", shlex.quote(pr_num))
        if repo:
            cmd_str = cmd_str.replace("{repo}", shlex.quote(repo))

        return cmd_str, None

    def _infer_occ_cwd(self, contract_path: Path | None) -> str | None:
        """Return the onex_change_control repo path when contract is from OCC.

        Detects OCC contracts by checking whether the contract path contains
        ``onex_change_control`` as a path component. Returns None for all
        other contracts (cwd stays inherited).
        """
        # OMN-13888 (scope 6): during a dev-resolved collect, greps must run
        # inside the origin/dev worktree so dev-only receipt files are visible.
        if self._occ_dev_root:
            return self._occ_dev_root
        if contract_path is None:
            return None
        if "onex_change_control" not in contract_path.parts:
            return None
        omni_home = os.environ.get("OMNI_HOME")
        if not omni_home:
            return None
        occ_path = Path(omni_home) / "onex_change_control"
        if occ_path.is_dir():
            return str(occ_path)
        return None

    def _resolve_contract_repo_dir(self, contract_path: Path | None) -> str | None:
        """Resolve the OCC repo root for the ``$CONTRACT_REPO_DIR`` check token.

        OMN-13857: contract check commands embed ``$CONTRACT_REPO_DIR`` (e.g.
        ``grep -q ... "$CONTRACT_REPO_DIR/drift/dod_receipts/<T>/.../command.yaml"``).
        Before this fix the token was only satisfied when the *caller* happened to
        export ``CONTRACT_REPO_DIR``; unset it expanded to the empty string and
        every receipt-backed check FAILED with a missing-prefix path — a
        false-negative verdict on genuinely-passing evidence.

        Resolution is deterministic and does not depend on the caller's shell:

        1. An explicit non-empty ``CONTRACT_REPO_DIR`` in the environment wins
           (honours a deliberate CI / operator override).
        2. Otherwise, if the contract lives under ``onex_change_control/``, the
           OCC root is derived from the contract path itself — correct even when
           ``OMNI_HOME`` is unset or points elsewhere.
        3. Otherwise ``$ONEX_CC_REPO_PATH`` when set.
        4. Otherwise ``$OMNI_HOME/onex_change_control`` when that directory exists.

        Returns the absolute OCC root, or ``None`` when it cannot be resolved
        (the check then runs with the token unset — the fail-closed pre-fix
        behaviour — rather than silently guessing a wrong root).
        """
        explicit = os.environ.get("CONTRACT_REPO_DIR", "").strip()
        if explicit:
            return explicit

        # OMN-13888 (scope 6): a materialised origin/dev worktree is the OCC root
        # for receipt-backed greps during a dev-resolved collect.
        if self._occ_dev_root:
            return self._occ_dev_root

        # Derive from the contract path when it lives inside the OCC clone.
        if contract_path is not None and "onex_change_control" in contract_path.parts:
            parts = contract_path.parts
            occ_index = parts.index("onex_change_control")
            occ_root = Path(*parts[: occ_index + 1])
            if occ_root.is_dir():
                return str(occ_root)

        cc_repo_path = os.environ.get("ONEX_CC_REPO_PATH", "").strip()
        if cc_repo_path:
            return cc_repo_path

        omni_home = os.environ.get("OMNI_HOME", "").strip()
        if omni_home:
            occ_path = Path(omni_home) / "onex_change_control"
            if occ_path.is_dir():
                return str(occ_path)

        return None

    # ------------------------------------------------------------------
    # OMN-14207: live GitHub PR-state verification for PR-bound evidence.
    # ------------------------------------------------------------------

    @staticmethod
    def _live_pr_check_enabled() -> bool:
        """Whether the live PR-state check runs (default: on).

        Disabled only when ``DOD_VERIFY_LIVE_PR_CHECK`` is explicitly set to a
        falsey value (``0``/``false``/``off``/``no``). This is a deliberate,
        logged operator opt-out for environments without an authenticated
        ``gh`` CLI — NOT a silent fallback. When disabled, the live check is
        emitted as SKIPPED (surfaced, not swallowed).
        """
        raw = os.environ.get(_LIVE_PR_CHECK_ENV, "1").strip().lower()
        return raw not in ("0", "false", "off", "no")

    @staticmethod
    def _normalize_repo(repo: str) -> str:
        """Return ``owner/repo``, defaulting a bare name to the OmniNode org."""
        repo = repo.strip()
        return repo if "/" in repo else f"{_DEFAULT_GITHUB_ORG}/{repo}"

    def _resolve_pr_bindings(
        self,
        item: dict[str, Any],
        ticket_id: str,
        contract_path: Path | None,
    ) -> list[tuple[str, int]]:
        """Resolve every ``(owner/repo, pr_number)`` this evidence item binds to.

        Two authoritative sources, in precedence order:

        1. An explicit ``pr`` mapping on the item (``{repo, number}``) or explicit
           ``repo`` + ``pr_number`` scalar fields — lets a contract declare the
           binding directly (future-proof).
        2. The durable receipt(s) for the item under
           ``<occ_root>/drift/dod_receipts/<ticket>/<item_id>/*.yaml``: the receipt
           records ``pr_number`` and the probed ``--repo owner/repo`` (the SAME
           fields the DurableEvidenceGate binds against). This is what catches a
           contract (e.g. OMN-13996) that never declared the binding explicitly.

        Returns a de-duplicated list; empty when the item does not bind to any PR
        (a non-PR evidence item is therefore unaffected by the live check). The
        evidence-item ``id`` slug is deliberately NOT parsed for a repo — those
        slugs are frequently descriptive labels (``product``, ``sea``,
        ``release-*``), not repo names, so guessing a repo from them would be a
        false-positive machine.
        """
        bindings: list[tuple[str, int]] = []
        seen: set[tuple[str, int]] = set()

        def _add(repo_val: object, number_val: object) -> None:
            if not isinstance(repo_val, str) or not repo_val.strip():
                return
            # bool is an int subclass — reject it as a PR number.
            if isinstance(number_val, bool):
                return
            if isinstance(number_val, int):
                number = number_val
            elif isinstance(number_val, str) and number_val.strip().isdigit():
                number = int(number_val.strip())
            else:
                return
            if number <= 0:
                return
            key = (self._normalize_repo(repo_val), number)
            if key not in seen:
                seen.add(key)
                bindings.append(key)

        # 1. Explicit fields on the item.
        explicit = item.get("pr")
        if isinstance(explicit, dict):
            _add(
                explicit.get("repo"),
                explicit.get("number", explicit.get("pr_number")),
            )
        _add(item.get("repo"), item.get("pr_number"))

        # 2. Receipt-derived bindings (only when nothing explicit was declared).
        if not bindings:
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id:
                receipts = apply_supersessions(
                    self._load_item_receipts(item_id, ticket_id, contract_path)
                )
                for citation in extract_receipt_merge_commits(receipts):
                    _add(citation.repo, citation.pr_number)

        return bindings

    def _load_item_receipts(
        self,
        item_id: str,
        ticket_id: str,
        contract_path: Path | None,
    ) -> list[dict[str, Any]]:
        """Load the durable receipt payloads for a single evidence item.

        Receipts live at the canonical platform layout
        ``<occ_root>/drift/dod_receipts/<ticket>/<item_id>/*.yaml``. Each payload
        is tagged with ``__source_name__`` so :func:`apply_supersessions` can
        order any supersession chain by the unforgeable filename ordinal (matching
        the DurableEvidenceGate). Returns ``[]`` when the OCC root or the receipt
        directory cannot be resolved.
        """
        occ_root = self._resolve_contract_repo_dir(contract_path)
        if occ_root is None:
            return []
        receipt_dir = Path(occ_root) / "drift" / "dod_receipts" / ticket_id / item_id
        if not receipt_dir.is_dir():
            return []
        payloads: list[dict[str, Any]] = []
        for receipt_file in sorted(receipt_dir.glob("*.yaml")):
            raw = self._load_yaml(receipt_file)
            if raw is None:
                continue
            raw.setdefault("__source_name__", receipt_file.name)
            payloads.append(raw)
        return payloads

    def _live_pr_checks_for_item(
        self,
        item: dict[str, Any],
        ticket_id: str,
        contract_path: Path | None,
    ) -> list[ModelEvidenceCheckResult]:
        """Emit the live PR-state check result(s) for a PR-bound evidence item.

        Returns ``[]`` for a non-PR item (leaving its declared-check result the
        sole authority). Otherwise one result per bound PR: VERIFIED when the PR
        is MERGED and all checks are green; FAILED otherwise, INCLUDING when the
        live state cannot be resolved (fail-closed — a Done-flip must not proceed
        on unverifiable PR state).
        """
        bindings = self._resolve_pr_bindings(item, ticket_id, contract_path)
        if not bindings:
            return []

        item_id = str(item.get("id", "unknown"))
        description = str(item.get("description", item_id))

        if not self._live_pr_check_enabled():
            logger.warning(
                "Live PR check disabled via %s; %d binding(s) NOT verified for %s",
                _LIVE_PR_CHECK_ENV,
                len(bindings),
                item_id,
            )
            return [
                ModelEvidenceCheckResult(
                    evidence_id=f"{item_id}::pr-live-state",
                    description=f"Live PR state for {description}",
                    status=EnumEvidenceCheckStatus.SKIPPED,
                    message=(
                        f"Live PR check disabled via {_LIVE_PR_CHECK_ENV}; "
                        f"bindings not verified: {bindings}"
                    ),
                )
            ]

        results: list[ModelEvidenceCheckResult] = []
        multi = len(bindings) > 1
        for repo, pr_number in bindings:
            evidence_id = (
                f"{item_id}::pr-{pr_number}-live-state"
                if multi
                else f"{item_id}::pr-live-state"
            )
            ok, message = self._verify_live_pr(repo, pr_number)
            results.append(
                ModelEvidenceCheckResult(
                    evidence_id=evidence_id,
                    description=f"Live GitHub state for {repo}#{pr_number} ({item_id})",
                    status=(
                        EnumEvidenceCheckStatus.VERIFIED
                        if ok
                        else EnumEvidenceCheckStatus.FAILED
                    ),
                    message=message,
                )
            )
        return results

    def _verify_live_pr(self, repo: str, pr_number: int) -> tuple[bool, str]:
        """Return ``(ok, message)`` for the live state of ``repo#pr_number``.

        ``ok`` is True only when the PR is MERGED AND every REQUIRED status check
        is green (OMN-14390) — a red non-required/informational check does not
        block. A failure to resolve the merge state (gh missing/auth/network/not-found)
        fails closed.
        """
        merge = self._fetch_pr_merge_state(repo, pr_number)
        if merge is None:
            return False, (
                f"{repo}#{pr_number}: could not resolve live PR state via gh "
                "(missing/auth/network/not-found). Failing closed — a Done-flip "
                "must not proceed on unverifiable PR state."
            )
        merged, state = merge
        reasons: list[str] = []
        if not merged:
            reasons.append(f"PR not merged (state={state})")
        checks_green, checks_detail = self._fetch_pr_checks_green(repo, pr_number)
        if not checks_green:
            reasons.append(f"required checks not green ({checks_detail})")
        if reasons:
            return False, f"{repo}#{pr_number}: " + "; ".join(reasons)
        return True, f"{repo}#{pr_number}: MERGED (state={state}); {checks_detail}"

    def _fetch_pr_merge_state(
        self,
        repo: str,
        pr_number: int,
    ) -> tuple[bool, str] | None:
        """Return ``(merged, state)`` for ``repo#pr_number`` via the GitHub
        effect handler's ``gh pr view`` lookup (OMN-14400, RSD-1 of
        OMN-14398 — behavior-identical carve-out of the gh-CLI I/O into a
        canonical EFFECT handler).

        Returns ``None`` on any inability to resolve the PR (timeout, missing gh,
        non-zero exit, unparseable output) so the caller can fail closed.
        """
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_MERGE_STATE,
            repo=repo,
            pr_number=pr_number,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        result = self._github_lookup_result(output)
        if not result.resolved:
            return None
        return bool(result.merged), result.state or "UNKNOWN"

    def _fetch_pr_checks_green(
        self,
        repo: str,
        pr_number: int,
    ) -> tuple[bool, str]:
        """Return ``(all_green, detail)`` for ``repo#pr_number`` REQUIRED status
        checks via the GitHub effect handler (OMN-14400, RSD-1 of OMN-14398).

        Scoped to required checks only via ``gh pr checks --required`` (OMN-14390)
        — a non-green *non-required* check (e.g. an informational/advisory job)
        must never fail a Done-flip; only branch-protection-required contexts are
        load-bearing here. Fails closed: any non-green required check
        (FAILURE/CANCELLED/PENDING/...), an empty required-check set, or an
        inability to enumerate checks yields ``False``.
        """
        command = ModelDodEvidenceGithubLookupCommand(
            operation=EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN,
            repo=repo,
            pr_number=pr_number,
        )
        output = HandlerDodEvidenceGithubEffect().handle(command)
        result = self._github_lookup_result(output)
        return bool(result.checks_green), result.detail or ""

    def _run_command_check(
        self,
        check: dict[str, Any],
        ticket_id: str,
        contract_path: Path | None = None,
    ) -> tuple[bool, str]:
        """Execute a command-type check. Returns (success, message).

        OMN-10078: when ``check["cwd"]`` is set, the runner expands its
        ``${OMNI_HOME}/${PR_NUMBER}/${REPO}/${TICKET_ID}`` template tokens,
        containment-checks the resolved path against ``OMNI_HOME``, and
        passes ``cwd=`` to ``subprocess.run``. When ``cwd`` is absent the
        runner inherits its caller's working directory (legacy behaviour).

        OMN-10476: placeholder substitution is applied to the command string
        for both ``{pr}/{repo}/{ticket_id}`` and ``${PR_NUMBER}/${REPO}/${TICKET_ID}``
        forms before execution. OCC contracts get automatic cwd injection.
        """
        # Prefer explicit `command` field; fall back to `check_value`
        cmd_str = check.get("command") or check.get("check_value", "")
        if not cmd_str:
            return False, "Empty command in check definition."

        # OMN-10476: resolve all placeholder forms before execution
        cmd_str, placeholder_err = self._resolve_command_placeholders(
            cmd_str, ticket_id
        )
        if placeholder_err is not None:
            return False, placeholder_err

        # OMN-10078: resolve optional cwd via template-substitution +
        # containment-check pipeline. None => inherit caller cwd.
        run_cwd: str | None = None
        cwd_template = check.get("cwd")
        if cwd_template is not None:
            if not isinstance(cwd_template, str):
                return False, f"cwd must be a string, got {type(cwd_template).__name__}"
            resolved, err = self._resolve_cwd(cwd_template, ticket_id)
            if err is not None:
                return False, err
            run_cwd = resolved
        else:
            # OMN-10476: auto-inject OCC cwd when no explicit cwd is declared
            run_cwd = self._infer_occ_cwd(contract_path)

        # OMN-13857: deterministically satisfy the ``$CONTRACT_REPO_DIR`` token
        # used by receipt-backed check commands, so the verdict does not depend
        # on the caller having exported it. Inherit the caller env and overlay
        # the resolved OCC root (never mutate os.environ in place).
        run_env: dict[str, str] | None = None
        contract_repo_dir = self._resolve_contract_repo_dir(contract_path)
        if contract_repo_dir is not None:
            run_env = dict(os.environ)
            run_env["CONTRACT_REPO_DIR"] = contract_repo_dir

        logger.info(
            "Running command check (cwd=%s, CONTRACT_REPO_DIR=%s): %s",
            run_cwd or "<inherit>",
            contract_repo_dir or "<unset>",
            cmd_str,
        )

        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd_str,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=run_cwd,
                env=run_env,
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)
        except subprocess.TimeoutExpired:
            return False, f"Timed out after {self._timeout}s: {cmd_str}"
        except Exception as exc:
            return False, f"Execution error: {exc}"

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode != 0:
            detail = stderr or stdout or f"exit code {result.returncode}"
            return False, f"FAILED ({elapsed_ms}ms): {detail}"

        return True, f"OK ({elapsed_ms}ms): {stdout[:200]}"

    def _run_file_exists_check(
        self,
        check: dict[str, Any],
        contract_path: Path | None = None,
    ) -> tuple[bool, str]:
        """Verify a path exists within the repo-root containment boundary.

        Accepts ``path`` or ``check_value`` as the target. Resolution rules:

        - Relative paths resolve against the inferred OCC repo root when the
          contract lives under ``onex_change_control/`` (matches the cwd
          inference used by ``_run_command_check``); otherwise they resolve
          against ``OMNI_HOME`` (or ``Path.cwd()`` fallback). Regression for
          OMN-10542.
        - Absolute paths are permitted only if they resolve inside ``OMNI_HOME``
          (when set).
        - ``..`` segments in the raw input are rejected up-front.
        - Every candidate (and every glob match) is canonicalised via
          ``Path.resolve()`` — which follows symlinks — and checked against
          the containment base with ``is_relative_to``. Symlink escapes are
          therefore blocked.
        - Glob metacharacters (``*``, ``?``, ``[``) are expanded; at least one
          match must remain after containment filtering.
        """
        raw_path = check.get("path") or check.get("check_value", "")
        if not raw_path:
            return False, "Empty path in file_exists check definition."

        raw_path_obj = Path(raw_path)
        if ".." in raw_path_obj.parts:
            return False, f"Path traversal not allowed: {raw_path}"

        omni_home = os.environ.get("OMNI_HOME")
        # Containment base: OMNI_HOME (or cwd fallback). All resolved paths must
        # land inside this boundary regardless of where they were resolved from.
        base = Path(omni_home).resolve() if omni_home else Path.cwd().resolve()

        # Relative-path resolution root: prefer the OCC repo root when this
        # contract lives under onex_change_control/, mirroring the cwd
        # inference _run_command_check uses (OMN-10542). The OCC root stays
        # inside OMNI_HOME so the containment check below is unaffected.
        occ_cwd = self._infer_occ_cwd(contract_path)
        resolve_root = Path(occ_cwd).resolve() if occ_cwd else base

        candidate = (
            raw_path_obj if raw_path_obj.is_absolute() else resolve_root / raw_path_obj
        )
        has_glob = any(ch in raw_path for ch in ("*", "?", "["))

        if has_glob:
            safe_matches: list[Path] = []
            for match in glob.glob(str(candidate)):
                resolved_match = Path(match).resolve()
                if resolved_match.is_relative_to(base):
                    safe_matches.append(resolved_match)
            if not safe_matches:
                return False, f"No matches for glob: {raw_path}"
            return True, f"OK: {len(safe_matches)} match(es) for {raw_path}"

        resolved_target = candidate.resolve()
        if not resolved_target.is_relative_to(base):
            return False, f"Path traversal not allowed: {raw_path}"
        if not resolved_target.exists():
            return False, f"Path does not exist: {raw_path}"
        return True, f"OK: exists {raw_path}"


__all__ = ["EvidenceCollector"]
