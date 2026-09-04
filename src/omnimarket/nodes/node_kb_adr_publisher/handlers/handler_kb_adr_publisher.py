# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for KB ADR publisher — EFFECT node.

Reads extracted_decisions.json from the canary run directory, filters by
model_key, renders ADRs via kb_adr_renderer adapter, clones the KB repo,
creates a branch, writes flat adrs/ADR-NNNN-<slug>.md files, and opens a
PR for human review.

[OMN-11808]
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Final, Protocol

from pydantic import ValidationError

from omnimarket.adapters.adr.kb_adr_renderer import render_adr_to_kb
from omnimarket.models.adr import (
    EnumAdrKBDestination,
    EnumAdrPublicationClassification,
    EnumAdrSourceVisibility,
    ModelAdrPublicationCandidate,
)
from omnimarket.nodes.node_kb_adr_publisher.models.model_publish_request import (
    ModelKBADRPublishRequest,
)
from omnimarket.nodes.node_kb_adr_publisher.models.model_publish_result import (
    ModelKBADRPublishResult,
)
from omnimarket.nodes.node_kb_adr_publisher.sanitization import (
    validate_candidate_preflight,
)

logger = logging.getLogger(__name__)

_KB_REPOSITORIES: Final[dict[EnumAdrKBDestination, str]] = {
    EnumAdrKBDestination.public: "OmniNode-ai/knowledge-base",
    EnumAdrKBDestination.private: "OmniNode-ai/knowledge-base-internal",
}
_SUBPROCESS_TIMEOUT_SECONDS: Final = 120


class ProtocolCommandRunner(Protocol):
    """Subprocess seam for git / gh CLI invocation.

    Production callers use the default ``subprocess.run``; tests inject a
    ``_Mock`` runner (the canonical ``_Mock*`` constructor-injection pattern),
    so the effect's I/O boundary is exercised at every outcome without ever
    running real subprocess and without monkeypatching ``subprocess``.
    """

    def __call__(
        self, cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]: ...


def _default_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Production ``ProtocolCommandRunner`` default — a thin ``subprocess.run``."""
    return subprocess.run(cmd, **kwargs)


def _load_decisions(
    decisions_file: Path, model_key: str
) -> list[ModelAdrPublicationCandidate]:
    """Load typed candidate evidence for exactly one served model identifier."""
    raw = json.loads(decisions_file.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("extracted_decisions.json must contain a JSON array")

    result: list[ModelAdrPublicationCandidate] = []
    for item in raw:
        candidate = ModelAdrPublicationCandidate.model_validate(item)
        if candidate.draft.extraction_metadata.model_id == model_key:
            result.append(candidate)
    return result


def _publication_policy_error(
    request: ModelKBADRPublishRequest,
    candidates: list[ModelAdrPublicationCandidate],
) -> str | None:
    """Return a fail-closed policy error before the publishing subprocess seam."""
    destination = request.kb_destination
    request_source = request.source_provenance
    if destination is None:
        return "kb_destination is required and must be a contract-owned destination"
    if request_source is None:
        return "source_provenance is required for publication"
    if destination not in _KB_REPOSITORIES:
        return "kb_destination is not an approved knowledge-base destination"

    for candidate in candidates:
        evidence_source = candidate.source_provenance
        if evidence_source is None:
            return "candidate evidence is missing source_provenance"
        if evidence_source != request_source:
            return "candidate evidence source_provenance conflicts with publish request"
        if candidate.kb_destination is None:
            return "candidate evidence is missing kb_destination"
        if candidate.kb_destination is not destination:
            return "candidate evidence kb_destination conflicts with publish request"
        if not candidate.source_documents:
            return "candidate evidence is missing hash-pinned source_documents"

    classification = request_source.publication_classification
    if request_source.source_visibility is EnumAdrSourceVisibility.private:
        if destination is not EnumAdrKBDestination.private:
            return "private source provenance may publish only to the private KB"
        return None

    if (
        classification
        in {
            EnumAdrPublicationClassification.private,
            EnumAdrPublicationClassification.restricted,
            EnumAdrPublicationClassification.needs_review,
        }
        and destination is EnumAdrKBDestination.public
    ):
        return (
            f"{classification.value} publication classification may not publish to "
            "the public KB"
        )
    return None


def _next_adr_number(adrs_dir: Path) -> int:
    """Return the next sequential ADR number by scanning existing adrs/ADR-NNNN-*.md.

    The knowledge-base convention numbers ADRs monotonically (ADR-0001, ADR-0002,
    ...). New proposals continue from the highest existing number; an empty or
    freshly cloned adrs/ starts at 1.
    """
    highest = 0
    for adr_file in adrs_dir.glob("ADR-*.md"):
        match = re.match(r"ADR-(\d+)", adr_file.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


class HandlerKBADRPublisher:
    """EFFECT handler — renders canary ADRs and opens a KB PR."""

    def __init__(self, *, run: ProtocolCommandRunner | None = None) -> None:
        """Store the subprocess seam.

        ``run`` defaults to ``subprocess.run`` (unchanged production behaviour);
        tests inject a ``_Mock`` runner to drive each I/O-boundary outcome.
        """
        self._run: ProtocolCommandRunner = run or _default_run

    async def handle(
        self, request: ModelKBADRPublishRequest
    ) -> ModelKBADRPublishResult:
        canary_run_dir = Path(request.canary_run_dir)
        decisions_file = canary_run_dir / "extracted_decisions.json"

        if not decisions_file.exists():
            logger.error("extracted_decisions.json not found: %s", decisions_file)
            return ModelKBADRPublishResult(
                success=False,
                error=f"extracted_decisions.json not found: {decisions_file}",
            )

        try:
            model_decisions = _load_decisions(decisions_file, request.model_key)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            logger.error("Invalid candidate evidence in %s: %s", decisions_file, exc)
            return ModelKBADRPublishResult(
                success=False,
                error_code="INVALID_CANDIDATE_EVIDENCE",
                error=f"Invalid candidate evidence: {exc}",
            )

        if not model_decisions:
            logger.error(
                "No decisions found with extraction_metadata.model_id == %r in %s",
                request.model_key,
                decisions_file,
            )
            return ModelKBADRPublishResult(
                success=False,
                error=f"No decisions for model_key={request.model_key!r}",
            )

        policy_error = _publication_policy_error(request, model_decisions)
        if policy_error is not None:
            logger.warning("KB ADR publication rejected: %s", policy_error)
            return ModelKBADRPublishResult(
                success=False,
                error_code="PUBLICATION_POLICY_REJECTED",
                error=policy_error,
                kb_destination=request.kb_destination,
            )

        assert request.kb_destination is not None
        sanitization_findings = tuple(
            finding
            for candidate in model_decisions
            for finding in validate_candidate_preflight(
                candidate, request.kb_destination
            )
        )
        if sanitization_findings:
            findings = ", ".join(sorted(set(sanitization_findings)))
            logger.warning("KB ADR publication rejected by sanitization: %s", findings)
            return ModelKBADRPublishResult(
                success=False,
                error_code="SANITIZATION_REJECTED",
                error=f"Publication sanitization rejected candidate: {findings}",
                kb_destination=request.kb_destination,
            )

        destination = request.kb_destination
        source_provenance = request.source_provenance
        assert destination is not None
        assert source_provenance is not None
        kb_repository = _KB_REPOSITORIES[destination]

        logger.info(
            "Found %d decisions from model key %r for %s KB publication",
            len(model_decisions),
            request.model_key,
            destination.value,
        )

        if request.dry_run:
            for candidate in model_decisions:
                logger.info("  Would create ADR: %s", candidate.draft.title)
            logger.info("Dry-run complete — no files written, no PR created.")
            return ModelKBADRPublishResult(
                success=True,
                adr_count=len(model_decisions),
                kb_destination=destination,
                kb_repository=kb_repository,
            )

        run_id = canary_run_dir.name
        branch = f"canary/{run_id}/{request.model_key}"

        with tempfile.TemporaryDirectory() as tmpdir:
            kb_dir = Path(tmpdir) / "knowledge-base"

            logger.info("Cloning approved %s KB repository ...", destination.value)
            self._run(
                ["gh", "repo", "clone", kb_repository, str(kb_dir)],
                check=True,
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            )

            # Knowledge-base convention: flat adrs/ADR-NNNN-<slug>.md with
            # status: proposed frontmatter — no adrs/proposed/ subdirectory.
            adrs_dir = kb_dir / "adrs"
            adrs_dir.mkdir(parents=True, exist_ok=True)
            next_num = _next_adr_number(adrs_dir)

            self._run(
                ["git", "-C", str(kb_dir), "checkout", "-b", branch],
                check=True,
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            )

            rendered_count = 0
            for offset, candidate in enumerate(model_decisions):
                adr_id = f"ADR-{next_num + offset:04d}"
                draft = candidate.draft
                result = render_adr_to_kb(
                    draft=draft, adr_id=adr_id, output_dir=adrs_dir
                )
                logger.info("  Rendered: %s", result.adr_path.name)
                rendered_count += 1

            if destination is EnumAdrKBDestination.public:
                # The public KB owns its canonical artifact sanitizer. Run it
                # over the rendered files before staging, committing, pushing,
                # or opening a PR; this is intentionally not a copied regex set.
                self._run(
                    ["uv", "run", "python", "scripts/validate.py"],
                    check=True,
                    cwd=str(kb_dir),
                    timeout=_SUBPROCESS_TIMEOUT_SECONDS,
                )

            logger.info("Committing %d rendered ADRs ...", rendered_count)
            self._run(
                ["git", "-C", str(kb_dir), "add", "-A"],
                check=True,
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            )
            self._run(
                [
                    "git",
                    "-C",
                    str(kb_dir),
                    "commit",
                    "-m",
                    f"feat: propose {rendered_count} ADRs from canary run {run_id}",
                ],
                check=True,
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            )
            self._run(
                ["git", "-C", str(kb_dir), "push", "-u", "origin", branch],
                check=True,
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            )

            pr_body = (
                "## Canary Extraction Results\n\n"
                f"- **Run ID**: `{run_id}`\n"
                f"- **Model**: `{request.model_key}`\n"
                f"- **Source repository**: `{source_provenance.source_repository}`\n"
                f"- **Source classification**: `{source_provenance.publication_classification.value}`\n"
                f"- **KB destination**: `{destination.value}`\n"
                f"- **Decisions extracted**: {rendered_count}\n\n"
                "All ADRs written to `adrs/` as `ADR-NNNN-<slug>.md` with "
                "`status: proposed` frontmatter, pending human review.\n"
                "Change status to `accepted` after review.\n\n"
                "## Test plan\n"
                "- [ ] Each proposed ADR has valid frontmatter "
                "(schemas/frontmatter.schema.json)\n"
                "- [ ] No internal references in content\n"
                "- [ ] Companion evidence JSON present for each ADR\n"
            )

            pr_result = self._run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    kb_repository,
                    "--title",
                    f"feat: {rendered_count} proposed ADRs from canary run {run_id}",
                    "--body",
                    pr_body,
                ],
                capture_output=True,
                text=True,
                check=True,
                cwd=str(kb_dir),
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            )
            pr_url = pr_result.stdout.strip()
            logger.info("PR created: %s", pr_url)

        return ModelKBADRPublishResult(
            success=True,
            adr_count=rendered_count,
            pr_url=pr_url,
            branch=branch,
            kb_destination=destination,
            kb_repository=kb_repository,
        )
