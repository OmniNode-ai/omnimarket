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
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    ModelEvidenceCheckResult,
)

logger = logging.getLogger(__name__)

# Default contract search roots (first match wins)
_DEFAULT_CONTRACT_ROOTS: list[str] = [
    "${ONEX_CC_REPO_PATH}/contracts",
    "${OMNI_HOME}/onex_change_control/contracts",
]


class EvidenceCollector:
    """Loads a ticket contract and runs dod_evidence checks.

    Usage::

        collector = EvidenceCollector()
        results = collector.collect("OMN-9414")
        # results: list[ModelEvidenceCheckResult]
    """

    def __init__(self, timeout_per_check: int = 30) -> None:
        self._timeout = timeout_per_check

    def collect(
        self,
        ticket_id: str,
        contract_path: str | None = None,
    ) -> list[ModelEvidenceCheckResult]:
        """Load contract and run all dod_evidence checks.

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

        return results

    def _find_contract(self, ticket_id: str) -> Path | None:
        """Search standard locations for a ticket contract."""
        for root_template in _DEFAULT_CONTRACT_ROOTS:
            root = Path(os.path.expandvars(root_template))
            candidate = root / f"{ticket_id}.yaml"
            if candidate.exists():
                logger.info("Found contract at %s", candidate)
                return candidate

        # Fallback: resolve via OMNI_HOME env var
        omni_home = os.environ.get("OMNI_HOME", str(Path.home() / "Code" / "omni_home"))
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
                ok, msg = self._run_file_exists_check(check)
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

        Checks PR_NUMBER env var first. Falls back to ``gh pr list`` search.
        Returns empty string when nothing can be resolved (caller must handle
        unresolved placeholders gracefully).
        """
        env_val = os.environ.get("PR_NUMBER", "").strip()
        if env_val:
            return env_val
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--search",
                    ticket_id,
                    "--state",
                    "merged",
                    "--json",
                    "number",
                    "--jq",
                    ".[0].number",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                num = result.stdout.strip()
                if num and num != "null":
                    return num
        except Exception:
            pass
        return ""

    def _lookup_repo_for_ticket(self, ticket_id: str) -> str:
        """Return the ``owner/repo`` string for ticket_id, or empty string.

        Checks REPO env var first. Falls back to ``gh pr list`` search
        to discover which repo contains a merged PR for this ticket.
        """
        env_val = os.environ.get("REPO", "").strip()
        if env_val:
            return env_val
        try:
            # Search across all repos known to the gh CLI for the ticket in the PR title/body.
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--search",
                    ticket_id,
                    "--state",
                    "merged",
                    "--json",
                    "number,headRepository",
                    "--jq",
                    '.[0] | .headRepository.nameWithOwner // ""',
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                repo = result.stdout.strip()
                if repo and repo != "null":
                    return repo
        except Exception:
            pass
        return ""

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

        logger.info(
            "Running command check (cwd=%s): %s", run_cwd or "<inherit>", cmd_str
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
    ) -> tuple[bool, str]:
        """Verify a path exists within the repo-root containment boundary.

        Accepts ``path`` or ``check_value`` as the target. Resolution rules:

        - Relative paths resolve against ``OMNI_HOME`` (or ``Path.cwd()`` fallback).
        - Absolute paths are permitted only if they resolve inside the base.
        - ``..`` segments in the raw input are rejected up-front.
        - Every candidate (and every glob match) is canonicalised via
          ``Path.resolve()`` — which follows symlinks — and checked against
          ``base`` with ``is_relative_to``. Symlink escapes are therefore blocked.
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
        base = Path(omni_home).resolve() if omni_home else Path.cwd().resolve()
        candidate = raw_path_obj if raw_path_obj.is_absolute() else base / raw_path_obj
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
