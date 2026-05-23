# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for KB ADR publisher — EFFECT node.

Reads extracted_decisions.json from the canary run directory, filters by
model_key, renders ADRs via kb_adr_renderer adapter, clones the KB repo,
creates a branch, writes files, and opens a PR for human review.

[OMN-11808]
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path

from omnibase_core.models.adr.model_adr_draft import ModelADRDraft

from omnimarket.adapters.adr.kb_adr_renderer import render_adr_to_kb
from omnimarket.nodes.node_kb_adr_publisher.models.model_publish_request import (
    ModelKBADRPublishRequest,
)
from omnimarket.nodes.node_kb_adr_publisher.models.model_publish_result import (
    ModelKBADRPublishResult,
)

logger = logging.getLogger(__name__)


def _load_decisions(decisions_file: Path, model_key: str) -> list[dict[str, object]]:
    decisions: list[dict[str, object]] = json.loads(
        decisions_file.read_text(encoding="utf-8")
    )
    result = []
    for d in decisions:
        if not isinstance(d, dict):
            continue
        metadata = d.get("extraction_metadata")
        if isinstance(metadata, dict) and metadata.get("model_id") == model_key:
            result.append(d)
    return result


class HandlerKBADRPublisher:
    """EFFECT handler — renders canary ADRs and opens a KB PR."""

    async def handle(
        self, *, request: ModelKBADRPublishRequest
    ) -> ModelKBADRPublishResult:
        canary_run_dir = Path(request.canary_run_dir)
        decisions_file = canary_run_dir / "extracted_decisions.json"

        if not decisions_file.exists():
            logger.error("extracted_decisions.json not found: %s", decisions_file)
            return ModelKBADRPublishResult(
                success=False,
                error=f"extracted_decisions.json not found: {decisions_file}",
            )

        model_decisions = _load_decisions(decisions_file, request.model_key)

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

        logger.info(
            "Found %d decisions from model key %r",
            len(model_decisions),
            request.model_key,
        )

        if request.dry_run:
            for d in model_decisions:
                logger.info("  Would create ADR: %s", d.get("title", "untitled"))
            logger.info("Dry-run complete — no files written, no PR created.")
            return ModelKBADRPublishResult(
                success=True,
                adr_count=len(model_decisions),
            )

        run_id = canary_run_dir.name
        branch = f"canary/{run_id}/{request.model_key}"

        with tempfile.TemporaryDirectory() as tmpdir:
            kb_dir = Path(tmpdir) / "knowledge-base"

            logger.info("Cloning %s ...", request.kb_repo)
            subprocess.run(
                ["gh", "repo", "clone", request.kb_repo, str(kb_dir)],
                check=True,
            )

            proposed_dir = kb_dir / "adrs" / "proposed"
            proposed_dir.mkdir(parents=True, exist_ok=True)

            subprocess.run(
                ["git", "-C", str(kb_dir), "checkout", "-b", branch],
                check=True,
            )

            rendered_count = 0
            for i, decision in enumerate(model_decisions, start=1):
                adr_id = f"ADR-PROPOSED-{i:04d}"
                draft = ModelADRDraft.model_validate(decision)
                result = render_adr_to_kb(
                    draft=draft, adr_id=adr_id, output_dir=proposed_dir
                )
                logger.info("  Rendered: %s", result.adr_path.name)
                rendered_count += 1

            logger.info("Committing %d rendered ADRs ...", rendered_count)
            subprocess.run(["git", "-C", str(kb_dir), "add", "-A"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(kb_dir),
                    "commit",
                    "-m",
                    f"feat: propose {rendered_count} ADRs from canary run {run_id}",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(kb_dir), "push", "-u", "origin", branch],
                check=True,
            )

            pr_body = (
                "## Canary Extraction Results\n\n"
                f"- **Run ID**: `{run_id}`\n"
                f"- **Model**: `{request.model_key}`\n"
                f"- **Decisions extracted**: {rendered_count}\n\n"
                "All ADRs placed in `adrs/proposed/` for human review.\n"
                "Move to `adrs/` and change status to `accepted` after review.\n\n"
                "## Test plan\n"
                "- [ ] Each proposed ADR has valid frontmatter\n"
                "- [ ] No internal references in content\n"
                "- [ ] Evidence JSON files present in `adrs/proposed/`\n"
            )

            pr_result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    request.kb_repo,
                    "--title",
                    f"feat: {rendered_count} proposed ADRs from canary run {run_id}",
                    "--body",
                    pr_body,
                ],
                capture_output=True,
                text=True,
                check=True,
                cwd=str(kb_dir),
            )
            pr_url = pr_result.stdout.strip()
            logger.info("PR created: %s", pr_url)

        return ModelKBADRPublishResult(
            success=True,
            adr_count=rendered_count,
            pr_url=pr_url,
            branch=branch,
        )
